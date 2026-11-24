"""Render a launchd plist for this checkout; never install or start it automatically."""
from __future__ import annotations

import argparse
from pathlib import Path
import plistlib
import sys

ROOT = Path(__file__).resolve().parents[1]
LABEL = "io.github.moje-tv"


def configuration(root: Path) -> dict:
    root = root.resolve()
    return {
        "Label": LABEL,
        "ProgramArguments": [str(root / ".venv/bin/tvserver"), "--config", str(root / "config.toml"), "serve"],
        "WorkingDirectory": str(root), "RunAtLoad": True, "KeepAlive": True,
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": str(root / "data/server.log"),
        "StandardErrorPath": str(root / "data/server.log"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    body = plistlib.dumps(configuration(args.root))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        (args.root / "data").mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(body)
        print(f"Written {args.output}; no launchd service was installed.")
    else:
        sys.stdout.buffer.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
