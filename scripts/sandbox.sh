#!/bin/zsh
# Vira sandbox: a stranger's first ten minutes, on your own Mac.
#
# Clones the PUBLIC repo into ~/vira-sandbox/app, builds its own venv from
# requirements.txt, and runs it against a FAKE HOME (~/vira-sandbox/home) so
# the app sees an empty machine: no contacts, no messages, no calendar, no
# skills library, no backups of yours. Reset wipes it back to virgin.
#
#   sandbox.sh new [--force]      clone + venv + empty home (a virgin install)
#   sandbox.sh serve              run it on :8400 (a real first boot)
#   sandbox.sh stop
#   sandbox.sh status
#   sandbox.sh expose <what>      lend the sandbox a real store, read-only-ish:
#                                   contacts | messages | calendar | photos | all
#   sandbox.sh unexpose           take them all back
#   sandbox.sh reset [--force]    wipe and re-provision from scratch
#
# WHY THE FAKE HOME: Path.home() follows $HOME, and every machine-level
# store Vira reads hangs off it — ~/Library/Messages/chat.db, AddressBook,
# Calendar, Photos, ~/.claude (the skills library), ~/.vira-backups. One
# env var isolates all of them. What it CANNOT isolate is the login
# Keychain, which is machine-wide: that is why the sandbox launches with
# VIRA_KEYCHAIN_PREFIX, so it can never read the live instance's Mercury
# token or overwrite its Graph refresh token (see settings.keychain_service).
#
# Not to be confused with scripts/branch.sh — that serves YOUR data from a
# feature branch to test a change. This serves NOTHING of yours, to test
# the install.

set -eu

REPO_URL=${VIRA_SANDBOX_REPO:-https://github.com/Helladuckets/vira.git}
ROOT=${VIRA_SANDBOX_ROOT:-$HOME/vira-sandbox}
APP=$ROOT/app
FAKE_HOME=$ROOT/home
PORT=${VIRA_SANDBOX_PORT:-8400}
PIDFILE=$ROOT/.instance.json
LOG=$ROOT/serve.log
REAL_HOME=$HOME

usage() { sed -n '2,19p' "$0"; exit 1; }
die() { print -u2 -- "error: $*"; exit 1; }

instance_pid() {
  [[ -f $PIDFILE ]] || return 0
  local pid; pid=$(python3 -c "import json;print(json.load(open('$PIDFILE'))['pid'])" 2>/dev/null) || return 0
  kill -0 "$pid" 2>/dev/null && echo "$pid" || true
}

# A logged-in claude CLI with no history: the OAuth credential lives in the
# Keychain (machine-wide, so it follows), but ~/.claude.json carries the
# onboarding state the CLI needs to run non-interactively. Copy only the
# flags — never projects, mcpServers, or repo paths.
seed_claude_state() {
  [[ -f $REAL_HOME/.claude.json ]] || return 0
  REAL_HOME=$REAL_HOME FAKE_HOME=$FAKE_HOME python3 - <<'PY'
import json, os, pathlib
KEEP = ("hasCompletedOnboarding", "lastOnboardingVersion", "installMethod",
        "autoUpdates", "userID", "firstStartTime", "theme")
src = pathlib.Path(os.environ["REAL_HOME"]) / ".claude.json"
try:
    d = json.loads(src.read_text())
except Exception:
    raise SystemExit(0)
out = {k: d[k] for k in KEEP if k in d}
(pathlib.Path(os.environ["FAKE_HOME"]) / ".claude.json").write_text(json.dumps(out, indent=2))
PY
}

cmd_new() {
  local force=${1:-}
  if [[ -e $ROOT ]]; then
    [[ $force == "--force" ]] || die "$ROOT already exists (sandbox.sh reset, or pass --force)"
    cmd_stop >/dev/null 2>&1 || true
    rm -rf "$ROOT"
  fi
  mkdir -p "$FAKE_HOME"

  echo "cloning $REPO_URL ..."
  git clone --depth 1 "$REPO_URL" "$APP"

  echo "building venv (--copies, so a Full Disk Access grant scopes to it alone) ..."
  python3 -m venv --copies "$APP/.venv"
  "$APP/.venv/bin/pip" install --quiet --upgrade pip
  "$APP/.venv/bin/pip" install -r "$APP/requirements.txt"

  seed_claude_state
  # No data/config.json on purpose: a virgin install boots into fixture mode
  # and opens the Setup window, which is the thing under test.
  echo ""
  echo "sandbox ready:  $ROOT"
  echo "  app     $APP  ($(git -C "$APP" rev-parse --short HEAD))"
  echo "  home    $FAKE_HOME  (empty — no contacts, messages, calendar, or skills)"
  echo "next:   scripts/sandbox.sh serve"
}

cmd_serve() {
  [[ -d $APP ]] || die "no sandbox at $APP (run: sandbox.sh new)"
  local pid; pid=$(instance_pid)
  [[ -n $pid ]] && { echo "already running (pid $pid) — http://127.0.0.1:$PORT"; exit 0; }
  lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 && die "port $PORT is busy"

  # Real first boot: NOT passive. Background workers run, which is the point —
  # they are what a new user's install actually does. The fake HOME is what
  # keeps them harmless, and VIRA_KEYCHAIN_PREFIX keeps live secrets unreachable.
  cd "$APP"
  HOME="$FAKE_HOME" VIRA_KEYCHAIN_PREFIX="sandbox-" \
    nohup "$APP/.venv/bin/uvicorn" server.main:app \
    --host 127.0.0.1 --port "$PORT" >> "$LOG" 2>&1 &
  pid=$!
  print -r -- "{\"pid\": $pid, \"port\": $PORT}" > "$PIDFILE"

  for i in $(seq 1 60); do
    curl -sf -o /dev/null "http://127.0.0.1:$PORT/" && break
    kill -0 "$pid" 2>/dev/null || die "instance died — see $LOG"
    sleep 0.5
  done
  curl -sf -o /dev/null "http://127.0.0.1:$PORT/" || die "no response on :$PORT — see $LOG"
  echo ""
  echo "sandbox up:   http://127.0.0.1:$PORT        <- a stranger's Vira"
  echo "stage view:   http://127.0.0.1:$PORT/stage.html"
  echo "log: $LOG    stop: scripts/sandbox.sh stop"
}

cmd_stop() {
  local pid; pid=$(instance_pid)
  if [[ -n $pid ]]; then kill "$pid" && echo "stopped (pid $pid)"; else echo "not running"; fi
  rm -f "$PIDFILE"
}

cmd_status() {
  [[ -d $APP ]] || { echo "no sandbox provisioned (sandbox.sh new)"; exit 0; }
  local pid; pid=$(instance_pid)
  echo "root      $ROOT"
  echo "app       $APP  ($(git -C "$APP" rev-parse --short HEAD 2>/dev/null || echo '?'))"
  echo "home      $FAKE_HOME"
  echo "state     ${pid:+RUNNING pid $pid on :$PORT}${pid:-stopped}"
  echo "crm       $([[ -f $FAKE_HOME/.vira/crm/people.json ]] && echo 'imported (real mode)' || echo 'none (fixture mode)')"
  echo -n "exposed   "
  local any=""
  for name in contacts messages calendar photos; do
    _exposed "$name" && { print -n -- "$name "; any=1; }
  done
  [[ -n $any ]] || print -n -- "nothing (empty machine)"
  echo ""
}

# ---- expose: lend the sandbox one real store, by symlink ----
# Paths are (label, path-relative-to-home) pairs.
_target_for() {
  case "$1" in
    contacts) echo "Library/Application Support/AddressBook";;
    messages) echo "Library/Messages";;
    calendar) echo "Library/Group Containers/group.com.apple.calendar";;
    photos)   echo "Pictures/Photos Library.photoslibrary";;
    *) return 1;;
  esac
}

_exposed() {
  local rel; rel=$(_target_for "$1") || return 1
  [[ -L "$FAKE_HOME/$rel" ]]
}

cmd_expose() {
  [[ $# -ge 1 ]] || usage
  [[ -d $FAKE_HOME ]] || die "no sandbox (run: sandbox.sh new)"
  local names=("$@")
  [[ $1 == "all" ]] && names=(contacts messages calendar photos)
  for name in $names; do
    local rel; rel=$(_target_for "$name") || die "unknown store: $name"
    [[ -e "$REAL_HOME/$rel" ]] || { echo "skip $name (not present on this Mac)"; continue; }
    mkdir -p "$FAKE_HOME/${rel:h}"
    rm -f "$FAKE_HOME/$rel"
    ln -s "$REAL_HOME/$rel" "$FAKE_HOME/$rel"
    echo "exposed $name -> $REAL_HOME/$rel"
  done
  echo ""
  echo "NOTE: the sandbox venv has no Full Disk Access of its own — grant it to"
  echo "      $APP/.venv/bin/python"
  echo "      (System Settings > Privacy & Security > Full Disk Access), which is"
  echo "      the same step a new user takes. Restart the instance after granting."
}

cmd_unexpose() {
  for name in contacts messages calendar photos; do
    local rel; rel=$(_target_for "$name")
    [[ -L "$FAKE_HOME/$rel" ]] && { rm -f "$FAKE_HOME/$rel"; echo "took back $name"; }
  done
  echo "sandbox is an empty machine again"
}

cmd_reset() {
  cmd_stop >/dev/null 2>&1 || true
  cmd_new --force
}

[[ $# -lt 1 ]] && usage
cmd=$1; shift
case "$cmd" in
  new)      cmd_new "${1:-}";;
  serve)    cmd_serve;;
  stop)     cmd_stop;;
  status)   cmd_status;;
  expose)   cmd_expose "$@";;
  unexpose) cmd_unexpose;;
  reset)    cmd_reset;;
  *) usage;;
esac
