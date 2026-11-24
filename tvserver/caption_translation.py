"""Optional local translation to Czech. Dependencies load only in the worker."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


PROMPT = ("Translate the following text into Czech. Note that you should only "
          "output the translated result without any additional explanation:\n\n")


class TranslationRejected(ValueError):
    """A bad individual caption, rather than an unavailable translation model."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def translation_available(runtime: dict) -> bool:
    try:
        config = runtime["translator"]
        model = Path(config["model_directory"])
        quantization = json.loads((model / "config.json").read_text())["quantization"]
        paths = config["pythonpath"]
        return (quantization["bits"] == 8 and (model / "model.safetensors").is_file()
                and (model / "tokenizer.json").is_file()
                and bool(paths) and all(Path(path).is_dir() for path in paths)
                and any((Path(path) / "mlx_lm" / "__init__.py").is_file() for path in paths))
    except (OSError, ValueError, KeyError, TypeError):
        return False


class LocalTranslator:
    def __init__(self, runtime: dict):
        self.config = runtime["translator"]
        self.model = self.tokenizer = None

    def _load(self):
        for directory in reversed(self.config["pythonpath"]):
            if directory not in sys.path:
                sys.path.insert(0, directory)
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        from mlx_lm import load
        import mlx.core as mx

        self.model, self.tokenizer = load(self.config["model_directory"], trust_remote_code=False)
        mx.synchronize()

    def translate(self, text: str) -> str:
        if not text.strip():
            return ""
        if len(text) > 1600:
            raise TranslationRejected("input_too_long")
        if self.model is None:
            self._load()
        import mlx.core as mx
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_logits_processors, make_sampler

        prompt = self.tokenizer.apply_chat_template(
            [{"role": "user", "content": PROMPT + text}],
            tokenize=True, add_generation_prompt=True,
        )
        pieces, final = [], None
        for response in stream_generate(
            self.model, self.tokenizer, prompt=prompt, max_tokens=128,
            sampler=make_sampler(temp=0),
            logits_processors=make_logits_processors(
                repetition_penalty=1.05, repetition_context_size=8192),
        ):
            pieces.append(response.text)
            final = response
        mx.synchronize()
        translated = "".join(pieces).strip()
        if final is not None and final.finish_reason == "length":
            raise TranslationRejected("output_limit")
        if final is None or final.finish_reason != "stop" or not translated:
            raise TranslationRejected("empty_or_unfinished_output")
        return translated
