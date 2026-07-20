#!/bin/zsh
# Vira server. Binds all interfaces so Tailscale/LAN devices can reach it.
# Dependencies (incl. claude-agent-sdk for live sessions) install into the
# EXISTING venv: .venv/bin/pip install -r requirements.txt
# Never rebuild the venv — the Full Disk Access grant is tied to its python
# binary (see README).
cd "$(dirname "$0")"
exec .venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8377 "$@"
