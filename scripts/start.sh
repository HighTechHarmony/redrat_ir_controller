#!/usr/bin/env bash
set -euo pipefail
# Start script for local testing — activates project venv if present
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT_DIR/.venv"
if [ -d "$VENV" ]; then
  echo "Using venv at $VENV"
  exec "$VENV/bin/python" "$ROOT_DIR/main.py" "$@"
else
  echo "No virtualenv found at $VENV — running system python"
  exec python3 "$ROOT_DIR/main.py" "$@"
fi
