"""Prepare optional MLX models and a machine-local captions runtime.

Run with ``uv run python scripts/prepare_models.py`` after installing the ai extra.
Only public model files are downloaded; no model-provided Python is executed.
"""
from __future__ import annotations

import argparse
from importlib.util import find_spec
import json
from pathlib import Path
import platform
import shutil
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]
WHISPER_REPO = "mlx-community/whisper-large-v3-turbo"
WHISPER_REVISION = "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb"
TRANSLATOR_REPO = "mlx-community/Hy-MT2-1.8B-8bit"
TRANSLATOR_REVISION = "f54bb3b8885363fd8b83d63d50b50a12a138321f"


def validate_model(path: Path, kind: str) -> None:
    required = ("config.json", "weights.safetensors") if kind == "whisper" else (
        "config.json", "model.safetensors", "tokenizer.json")
    missing = [name for name in required if not (path / name).is_file()]
    if missing:
        raise ValueError(f"{kind}: missing {', '.join(missing)} in {path}")
    config = json.loads((path / "config.json").read_text())
    if kind == "translator" and config.get("quantization", {}).get("bits") != 8:
        raise ValueError("Local translation requires the 8-bit Hy-MT2 checkpoint.")


def build_runtime(whisper: Path, translator: Path | None, *, python: str,
                  paths: list[str], ffmpeg: str, openrouter: bool = False) -> dict:
    runtime = {
        "backend": "mlx", "model": "large-v3-turbo",
        "model_directory": str(whisper.resolve()), "python": python, "pythonpath": paths,
        "language": "auto", "translation": False, "chunk_seconds": 2,
        "overlap_seconds": 1, "ffmpeg": ffmpeg,
    }
    if translator is not None:
        runtime["translator"] = {"model": "Hy-MT2-1.8B-8bit",
                                 "model_directory": str(translator.resolve()), "pythonpath": paths}
    if openrouter:
        # Read the key from the server environment at use time; never copy it here.
        runtime["openrouter"] = {"model": "google/gemini-2.5-flash-lite"}
    return runtime


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def download(repo: str, revision: str, dest: Path) -> dict:
    from huggingface_hub import HfApi, snapshot_download

    actual_revision = HfApi(token=False).model_info(repo, revision=revision, token=False).sha
    snapshot_download(
        repo, revision=actual_revision, local_dir=dest, token=False, max_workers=2,
        allow_patterns=["*.json", "*.safetensors", "*.txt", "*.md", "*.jinja", "*.model"],
    )
    return {"repo": repo, "revision": actual_revision, "directory": str(dest.resolve())}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runtime", type=Path, default=ROOT / "data/caption_runtime.json")
    p.add_argument("--model-dir", type=Path, default=ROOT / "data/models")
    p.add_argument("--whisper-path", type=Path)
    p.add_argument("--translator-path", type=Path)
    p.add_argument("--whisper-revision", default=WHISPER_REVISION)
    p.add_argument("--translator-revision", default=TRANSLATOR_REVISION)
    p.add_argument("--asr-only", action="store_true")
    p.add_argument("--enable-openrouter", action="store_true")
    p.add_argument("--no-download", action="store_true")
    p.add_argument("--check", action="store_true")
    p.add_argument("--force", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.check:
        from tvserver.captions import CaptionService
        status = CaptionService(args.runtime).status()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if status["available"] else 1
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise ValueError("Local MLX captions require macOS on Apple Silicon.")
    if args.runtime.exists() and not args.force:
        raise ValueError("Runtime already exists. Use --check, a different --runtime, or --force.")
    required = ["mlx_whisper"] + ([] if args.asr_only else ["mlx_lm"])
    if any(find_spec(name) is None for name in required):
        raise ValueError("Install the AI environment first: uv sync --extra dev --extra ai")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        raise ValueError("FFmpeg and ffprobe must be installed and available on PATH.")
    specs = [("whisper", WHISPER_REPO, args.whisper_revision,
              args.whisper_path, args.model_dir / "whisper-large-v3-turbo")]
    if not args.asr_only:
        specs.append(("translator", TRANSLATOR_REPO, args.translator_revision,
                      args.translator_path, args.model_dir / "Hy-MT2-1.8B-8bit"))
    models, provenance = {}, []
    for kind, repo, revision, provided, default in specs:
        path = (provided or default).resolve()
        if provided is not None or args.no_download:
            info = {"repo": repo, "revision": None, "directory": str(path), "source": "existing-local-copy"}
        else:
            print(f"Downloading {repo}…", flush=True)
            info = download(repo, revision, path)
        validate_model(path, kind)
        models[kind] = path
        provenance.append(info)
    paths = list(dict.fromkeys(str(Path(sysconfig.get_paths()[key]).resolve()) for key in ("purelib", "platlib")))
    runtime = build_runtime(models["whisper"], models.get("translator"),
                            python=str(Path(sys.executable).absolute()), paths=paths,
                            ffmpeg=ffmpeg, openrouter=args.enable_openrouter)
    from importlib import metadata
    versions = {}
    for name in ["mlx", "mlx-whisper", "mlx-lm", "huggingface-hub"]:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    atomic_json(args.runtime.parent / "model-provenance.json", {"models": provenance, "versions": versions})
    atomic_json(args.runtime, runtime)
    print(f"Prepared {args.runtime}. Restart Moje TV to load the configuration.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
