#!/bin/zsh
# Run the WhatsApp sidecar by hand against a given Vira checkout's data/.
# Normal live operation does NOT use this — server/whatsapp.py spawns the
# sidecar itself. This is for passive test instances (branch.sh serve),
# where the server never starts workers and the owner starts the sidecar
# deliberately, and for debugging.
#
#   scripts/whatsapp-sidecar.sh            # this checkout's data/, default port
#   scripts/whatsapp-sidecar.sh 18391      # explicit port
set -eu

HERE=${0:A:h:h}                       # repo root (script lives in scripts/)
BRIDGE="$HERE/bridge/whatsapp"
DATA="$HERE/data/whatsapp"
PORT=${1:-$(python3 -c "
import json,sys
try: print(json.load(open('$HERE/data/config.json')).get('whatsapp_bridge_port') or 18377)
except Exception: print(18377)")}

[[ -d "$BRIDGE/node_modules" ]] || {
  echo "installing sidecar dependencies (one time)…"
  (cd "$BRIDGE" && npm install --no-fund --no-audit)
}
mkdir -p "$DATA"
echo "sidecar on 127.0.0.1:$PORT  (session + inbox under $DATA)"
echo "stop: curl -s -X POST http://127.0.0.1:$PORT/stop"
exec node "$BRIDGE/sidecar.js" --port "$PORT" \
  --session-dir "$DATA/session" \
  --inbox "$DATA/inbox.ndjson" \
  --pidfile "$DATA/sidecar.pid" \
  --log "$DATA/sidecar.log"
