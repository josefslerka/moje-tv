"""Explicitly selected, paid text translation. Audio stays in the local worker."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import os
from pathlib import Path
import shlex

import httpx

from .caption_translation import TranslationRejected
from .caption_usage import CaptionUsage


MODEL = "google/gemini-2.5-flash-lite"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
PROMPT = ("Jsi překladatel do češtiny (jazyk cs). Přelož následující text výhradně do češtiny. "
          "Zachovej význam, zápory, vlastní jména, čísla a případnou neúplnost vět. "
          "Vrať pouze český překlad bez vysvětlení.\n\n")


def _key(config):
    if config.get("env_file"):
        key = ""
        for line in Path(config["env_file"]).read_text().splitlines():
            name, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and name.strip() == "OPENROUTER_API_KEY":
                parts = shlex.split(value, comments=True)
                key = parts[0] if len(parts) == 1 else ""
    else:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key.startswith("sk-or-") or len(key) < 24:
        raise ValueError("OpenRouter key is not configured")
    return key


def cloud_translation_available(runtime: dict) -> bool:
    try:
        config = runtime["openrouter"]
        return isinstance(config, dict) and config.get("model", MODEL) == MODEL and bool(_key(config))
    except (OSError, ValueError, TypeError, KeyError):
        return False


@dataclass(frozen=True)
class CloudTranslation:
    text: str
    cost_usd: float | None = None


class OpenRouterTranslator:
    def __init__(self, runtime: dict, *, client: httpx.AsyncClient | None = None,
                 recorder: CaptionUsage | None = None):
        self.config = runtime["openrouter"]
        if self.config.get("model", MODEL) != MODEL:
            raise ValueError("Unsupported paid translation model")
        self.client = client
        self.recorder = recorder

    async def translate(self, text: str, *, channel_id: int | None = None) -> CloudTranslation:
        if not text.strip():
            return CloudTranslation("")
        if len(text) > 1600:
            raise TranslationRejected("input_too_long")
        key = _key(self.config)
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=httpx.Timeout(3, connect=2), follow_redirects=False)
        # No retries, model fallback or paid tools. The server selects this fixed
        # model; browsers cannot supply an endpoint, key, model or price ceiling.
        request_id = self.recorder.start(MODEL, channel_id) if self.recorder else None
        status, cost, input_tokens, output_tokens, generation_id, http_status = "error", None, None, None, None, None
        try:
            async with asyncio.timeout(4):
                response = await self.client.post(
                    ENDPOINT,
                    headers={"Authorization": "Bearer " + key,
                             "X-OpenRouter-Title": "Moje TV subtitles"},
                    json={"model": MODEL, "messages": [{"role": "user", "content": PROMPT + text}],
                          "temperature": 0, "max_tokens": 128, "stream": False,
                          "reasoning": {"enabled": False},
                          "provider": {"sort": "latency", "max_price": {"prompt": .10, "completion": .40}}},
                )
            http_status = response.status_code
            try:
                data = response.json()
            except ValueError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            usage = data.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            cost = usage.get("cost")
            if not isinstance(cost, (int, float)) or isinstance(cost, bool) or not math.isfinite(cost) or cost < 0:
                cost = None
            input_tokens, output_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
            generation_id = data.get("id")
            if http_status != 200:
                status = "http_error"
                # Provider error bodies, transcripts and credentials stay out of logs.
                raise RuntimeError("OpenRouter request failed")
            try:
                choice = data["choices"][0]
                translated = choice["message"]["content"]
                if choice.get("finish_reason") == "length":
                    raise TranslationRejected("output_limit")
                if choice.get("finish_reason") != "stop" or not isinstance(translated, str) or not translated.strip():
                    raise TranslationRejected("empty_or_unfinished_output")
            except (KeyError, IndexError, TypeError, ValueError) as error:
                if isinstance(error, TranslationRejected):
                    raise
                raise TranslationRejected("invalid_response") from None
            status = "success"
            return CloudTranslation(translated.strip(), cost)
        except TranslationRejected:
            status = "rejected"
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except (TimeoutError, httpx.TimeoutException):
            status = "timeout"
            raise
        finally:
            if self.recorder:
                self.recorder.finish(request_id, status=status, cost_usd=cost, input_tokens=input_tokens,
                                     output_tokens=output_tokens, generation_id=generation_id, http_status=http_status)

    async def close(self):
        if self.client is not None:
            await self.client.aclose()
            self.client = None
