#!/bin/sh
set -eu
umask 077

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/.python-packages:$ROOT"
export PYTHONUNBUFFERED=1
exec /usr/bin/python3 "$ROOT/log_runner.py"
