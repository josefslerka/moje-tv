#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -f config.toml ]; then
  cp config.example.toml config.toml
fi
exec .venv/bin/tvserver --config config.toml serve
