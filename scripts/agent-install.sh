#!/bin/bash
# One-shot install + launch for Vira, written for whoever is doing a first
# install — an AI agent handed this repo, or a human who wants one command.
# Idempotent: re-running reuses whatever already exists and never starts a
# second server. The full installer contract lives in AGENTS.md.
#
# What it does: pick a python -> venv --copies -> pip install -> serve
# http://localhost:8377 -> open it. Nothing else. Persistence (launchd /
# Task Scheduler), data imports, and AI sign-in are the app's own Setup.
set -u
cd "$(dirname "$0")/.." || exit 1

say()  { printf '%s\n' "$*"; }
fail() { printf 'agent-install: %s\n' "$*" >&2; exit 1; }

URL="http://localhost:8377"

# --- 1. git: requirements.txt installs the qocha engine from GitHub -------
command -v git >/dev/null 2>&1 || fail \
  "git is required (one dependency installs from GitHub). macOS: run 'xcode-select --install' and rerun this."

# --- 2. python: prefer 3.12/3.13 ------------------------------------------
# The optional media-index extras (torch, insightface, mlx-whisper) have no
# 3.14 wheels yet; building the venv on 3.12/3.13 keeps that path open.
# Any 3.10+ runs the base app, so 3.14 is a warning, not a refusal.
if [ -x .venv/bin/python ] && .venv/bin/python -c \
     'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' \
     2>/dev/null; then
  say "venv: reusing existing .venv ($(.venv/bin/python -V 2>&1))"
else
  PY=""
  for c in python3.13 python3.12 \
           /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
           python3.11 python3.10 python3; do
    p="$(command -v "$c" 2>/dev/null || true)"
    [ -n "$p" ] && [ -x "$p" ] || continue
    "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' \
      2>/dev/null || continue
    PY="$p"
    break
  done
  [ -n "${PY}" ] || fail \
    "no python 3.10+ found. macOS: 'brew install python@3.12', then rerun."
  if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info < (3,14) else 1)' 2>/dev/null; then
    say "note: $PY is 3.14+ — fine for the base app; the optional media extras have no wheels there yet"
  fi
  say "venv: creating .venv with $PY ($($PY -V 2>&1))"
  # --copies, not symlinks: the macOS Full Disk Access grant attaches to
  # the real binary, so the venv must own one.
  "$PY" -m venv --copies .venv || fail "venv creation failed"
fi

# --- 3. dependencies into the venv, never a global pip --------------------
say "deps: installing requirements.txt (a first run takes a minute)…"
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt \
  || fail "pip install failed — rerun by hand to see why: .venv/bin/pip install -r requirements.txt"

# --- 4. serve, unless a Vira already answers ------------------------------
mkdir -p data
if curl -s -m 2 -o /dev/null "$URL/api/config" 2>/dev/null; then
  say "serve: a server already answers at $URL — not starting a second one"
else
  nohup .venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8377 \
    >> data/server.log 2>&1 &
  echo $! > data/agent-install.pid
  up=""
  for _ in $(seq 1 30); do
    sleep 1
    if curl -s -m 2 -o /dev/null "$URL/api/config" 2>/dev/null; then
      up=1
      break
    fi
  done
  [ -n "$up" ] || fail "server did not come up in 30s — read data/server.log"
  say "serve: $URL (pid $(cat data/agent-install.pid), log data/server.log)"
fi

# --- 5. open it for the human ---------------------------------------------
case "$(uname -s)" in
  Darwin) open "$URL" >/dev/null 2>&1 || true ;;
  *)      command -v xdg-open >/dev/null 2>&1 && xdg-open "$URL" >/dev/null 2>&1 || true ;;
esac

say ""
say "Vira is up: $URL"
say "A fresh install boots into fixture mode — the demo contact's thread is the tour."
say "Next step: connect an AI. The app's first screen walks it; if you are"
say "an AI agent, AGENTS.md says how to connect yourself."
