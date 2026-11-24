"""Check exactly the files tracked by Git before publishing or in CI.

This is a focused release guard, not a complete security or licensing audit.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_FOLDERS = {"data", ".venv", ".git", "__pycache__", "node_modules", "archiv"}
MEDIA_SUFFIXES = {".db", ".sqlite", ".mp4", ".mov", ".mkv", ".wav", ".aiff", ".mp3", ".srt", ".vtt", ".safetensors"}
PATTERNS = {
    "provider key": re.compile(r"(?:sk-or-v1-|sk-proj-|AIza)[A-Za-z0-9_-]{16,}"),
    "GitHub token": re.compile(r"(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{20,}"),
    "AWS access key": re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "personal absolute path": re.compile(r"/(?:Users|home)/[^/\s\"']+/"),
}


def main() -> int:
    listing = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    names = [name.decode() for name in listing.split(b"\0") if name]
    if not names:
        raise SystemExit("No tracked files. Stage the explicit public file set first.")
    problems = []
    total = 0
    for name in names:
        path = ROOT / name
        if (FORBIDDEN_FOLDERS.intersection(path.relative_to(ROOT).parts)
                or path.suffix in MEDIA_SUFFIXES or path.name.startswith(".env")
                or path.name in {"config.toml", "caption_runtime.json"}
                or name.startswith("tvserver/static/experiments/") or path.is_symlink()):
            problems.append(f"Private/runtime file: {name}")
            continue
        data = path.read_bytes()
        total += len(data)
        if len(data) > 2_000_000:
            problems.append(f"Unexpectedly large file: {name}")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"Unexpected binary file: {name}")
            continue
        for reason, pattern in PATTERNS.items():
            if pattern.search(text):
                # Never print matching values, even when this check fails.
                problems.append(f"Potential {reason}: {name}")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"Public tree checked: {len(names)} text files, {total:,} bytes; no matched secrets or runtime data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
