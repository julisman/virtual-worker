#!/usr/bin/env bash
# Virtual Worker service control: ./vw start | stop | restart | status | logs [-f]
# Runs the backend detached with auto-restart and serves the built UI on one port
# (default http://127.0.0.1:8888). Bootstraps on first run via setup.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x "$ROOT/backend/.venv/bin/python" ]]; then
  printf '\033[36m[vw]\033[0m first run — installing prerequisites…\n'
  "$ROOT/setup.sh"
fi
exec "$ROOT/backend/.venv/bin/python" "$ROOT/backend/vw_cli.py" "$@"
