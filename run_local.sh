#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ ! -d .venv ]]; then
  python3.12 -m venv .venv
fi

source .venv/bin/activate
python -m pip install -U pip
pip install -e .

printf '\nStart the WhatsApp bridge in another terminal first:\n'
printf '  cd %q && go run main.go\n\n' "$ROOT/upstream/whatsapp-mcp/whatsapp-bridge"

python -m app.server
