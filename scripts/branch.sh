#!/bin/zsh
# Vira parallel-branch workflow. One feature = one branch = one worktree.
# The live instance (launchd, port 8377) only ever changes at a merge.
# See CLAUDE.md, section "Parallel feature branches".
#
#   branch.sh start <slug>     new branch claude/<slug> + worktree .worktrees/<slug>
#   branch.sh adopt [slug]     provision a worktree this script didn't create
#   branch.sh serve <slug>     test instance: cloned data, passive, local + tailnet
#   branch.sh serve <slug> --fresh   re-clone data before serving
#   branch.sh serve <slug> --fixture synthetic-data preview (safe to share)
#   branch.sh stop <slug>      stop the test instance
#   branch.sh list             all branch worktrees, their state, running ports
#   branch.sh merge <slug>     fast, clean merge into live main (aborts on conflict)
#   branch.sh discard <slug>   remove worktree + branch (refuses if dirty)

set -eu

# Resolve the live (main) checkout from wherever this script runs — the
# common git dir belongs to the primary worktree.
GIT_COMMON=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || {
  echo "error: run from inside a vira checkout" >&2; exit 1; }
LIVE=${GIT_COMMON:h}
# Worktrees live INSIDE the live checkout, gitignored. They used to be siblings
# of it (../vira-<slug>), which put a throwaway tree for "fix a visual bug" at
# the same level in ~/workspace as the projects themselves — vira, crm, qocha.
# One dispatch per folder, never cleaned up, and by 2026-07-29 six of them had
# piled up in an afternoon. A worktree is an implementation detail of a branch,
# not a project, so it belongs under the project it branches from.
#
# Safe because the guard already expects it: worktree.violates() tests the
# worktree BEFORE the live root precisely so a nested one is not mistaken for
# a write into live (that is where the app's own worktree toggle has always
# put them, .claude/worktrees/<slug>). .worktrees/ must stay in .gitignore or
# every merge preflight would read the live tree as dirty.
WT_HOME=$LIVE/.worktrees
PORT_MIN=8378
PORT_MAX=8399
PIDFILE=.test-instance.json

# $0 inside a zsh function is the FUNCTION name, not the script.
usage() { sed -n '2,15p' "${(%):-%x}"; exit 1; }

slug_check() {
  [[ "$1" =~ ^[a-z0-9][a-z0-9-]*$ ]] || {
    echo "error: slug must be kebab-case ([a-z0-9-])" >&2; exit 1; }
}

# The worktree holding claude/<slug>, WHEREVER it lives — asked of git rather
# than assumed. `start` puts worktrees at $WT_HOME/<slug>, but a worktree made
# by something else is just as real: the app's worktree toggle creates them
# under .claude/worktrees/<slug>, and every worktree made before 2026-07-29
# sits at ../vira-<slug>. serve/stop/merge/discard used to fail on those with
# "no worktree at ...". Because git is the authority here, moving where NEW
# worktrees go needed no change to any other command, and the ones already on
# disk keep working exactly as they did. Falls back to the canonical path,
# which is what `start` creates and what `merge`/`discard` accept for a branch
# whose worktree is already gone.
wt_dir() {
  local d
  d=$(git -C "$LIVE" worktree list --porcelain |
      awk -v b="branch refs/heads/claude/$1" \
          '/^worktree /{wt=substr($0,10)} $0==b{print wt; exit}')
  if [[ -n "$d" ]]; then echo "$d"; else echo "$WT_HOME/$1"; fi
}

# Provision the gitignored pieces a session needs, whoever made the worktree:
# - the FDA-granted venv (never rebuild; symlink the live one)
# - CLAUDE.md + .claude/launch.json (COPIES — edits are ported back by hand at
#   merge time because these files never ride git)
# CLAUDE.md is the load-bearing one: it carries this workflow, so a session
# that never receives it does not know the branch discipline exists. Idempotent.
provision() {
  local dir=$1
  [[ -e "$dir/.venv" ]] || ln -s "$LIVE/.venv" "$dir/.venv"
  [[ -e "$dir/CLAUDE.md" ]] || cp "$LIVE/CLAUDE.md" "$dir/CLAUDE.md" 2>/dev/null || true
  mkdir -p "$dir/.claude"
  [[ -e "$dir/.claude/launch.json" ]] ||
    cp "$LIVE/.claude/launch.json" "$dir/.claude/launch.json" 2>/dev/null || true
}

instance_pid() {  # prints pid if the worktree's instance is alive, else nothing
  local dir=$1 pid label
  [[ -f "$dir/$PIDFILE" ]] || return 0
  label=$(python3 -c "import json;print(json.load(open('$dir/$PIDFILE')).get('label',''))" 2>/dev/null || true)
  if [[ -n "$label" && "$(uname -s)" == "Darwin" ]]; then
    pid=$(launchctl print "gui/$(id -u)/$label" 2>/dev/null |
          awk '/^[[:space:]]*pid = /{print $3; exit}')
    [[ -n "$pid" ]] && { echo "$pid"; return 0; }
  fi
  pid=$(python3 -c "import json;print(json.load(open('$dir/$PIDFILE'))['pid'])" 2>/dev/null) || return 0
  kill -0 "$pid" 2>/dev/null && echo "$pid" || true
}

instance_port() {
  local dir=$1
  [[ -f "$dir/$PIDFILE" ]] || return 0
  python3 -c "import json;print(json.load(open('$dir/$PIDFILE'))['port'])" 2>/dev/null || true
}

# Resolve the installed CLI once. The App Store build does not always put its
# shim on PATH, while the standalone build does.
tailscale_binary() {
  local binary=""
  if binary=$(command -v tailscale 2>/dev/null); then
    :
  elif [[ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]]; then
    binary=/Applications/Tailscale.app/Contents/MacOS/Tailscale
  else
    return 0
  fi
  echo "$binary"
}

# Test uvicorn remains loopback-only. Tailscale Serve is the sole bridge out:
# encrypted, authenticated, and reachable only by this tailnet. Binding the
# cloned personal data to 0.0.0.0 would also expose it to the local LAN.
tailnet_host() {
  local binary
  binary=$(tailscale_binary)
  [[ -n "$binary" ]] || return 0
  "$binary" status --json 2>/dev/null | python3 -c '
import json, sys
try:
    node = (json.load(sys.stdin).get("Self") or {}).get("DNSName") or ""
    print(node.rstrip("."))
except (OSError, ValueError):
    pass
' 2>/dev/null || true
}

tailnet_serve() {
  local port=$1 binary host
  binary=$(tailscale_binary)
  [[ -n "$binary" ]] || return 0
  host=$(tailnet_host)
  [[ -n "$host" ]] || return 0
  "$binary" serve --bg --yes --http="$port" \
    "http://127.0.0.1:$port" >/dev/null
}

tailnet_unserve() {
  local port=$1 binary
  binary=$(tailscale_binary)
  [[ -n "$binary" && -n "$port" ]] || return 0
  "$binary" serve --yes --http="$port" off >/dev/null 2>&1 || true
}

test_label() {
  echo "nyc.durham.vira.test.$1"
}

launchd_pid() {
  local label=$1
  launchctl print "gui/$(id -u)/$label" 2>/dev/null |
    awk '/^[[:space:]]*pid = /{print $3; exit}'
}

start_test_process() {
  local slug=$1 dir=$2 port=$3 pid="" label plist
  if [[ "$(uname -s)" == "Darwin" ]] && command -v launchctl >/dev/null; then
    label=$(test_label "$slug")
    plist="$HOME/Library/LaunchAgents/$label.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    python3 - "$plist" "$label" "$LIVE/.venv/bin/python" "$dir" \
      "$port" "$dir/.test-instance.log" "$PATH" <<'PY'
import plistlib
import sys

path, label, python, workdir, port, log, path_env = sys.argv[1:]
payload = {
    "Label": label,
    # caffeinate must not receive a utility: macOS ignores -t when it does.
    # The shell starts a genuinely bounded assertion, then execs Vira. Both
    # stay in launchd's process group, so bootout drops the assertion early.
    "ProgramArguments": [
        "/bin/sh", "-c",
        '"$1" -i -s -t 43200 & '
        'exec "$2" -m uvicorn server.main:app --host 127.0.0.1 --port "$3"',
        label, "/usr/bin/caffeinate", python, port,
    ],
    "WorkingDirectory": workdir,
    "EnvironmentVariables": {"VIRA_PASSIVE": "1", "PATH": path_env},
    "RunAtLoad": True,
    "KeepAlive": True,
    "AbandonProcessGroup": False,
    "ThrottleInterval": 3,
    "StandardOutPath": log,
    "StandardErrorPath": log,
}
with open(path, "wb") as handle:
    plistlib.dump(payload, handle)
PY
    launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
    for _ in $(seq 1 20); do
      pid=$(launchd_pid "$label")
      [[ -n "$pid" ]] && break
      sleep 0.1
    done
    [[ -n "$pid" ]] || {
      echo "error: launchd did not start $label" >&2
      return 1
    }
    print -r -- "{\"pid\": $pid, \"port\": $port, \"label\": \"$label\"}" > "$dir/$PIDFILE"
  else
    cd "$dir"
    VIRA_PASSIVE=1 nohup "$LIVE/.venv/bin/uvicorn" server.main:app \
      --host 127.0.0.1 --port "$port" >> "$dir/.test-instance.log" 2>&1 &
    pid=$!
    print -r -- "{\"pid\": $pid, \"port\": $port}" > "$dir/$PIDFILE"
  fi
  echo "$pid"
}

stop_test_process() {
  local dir=$1 pid label port
  [[ -f "$dir/$PIDFILE" ]] || return 0
  port=$(instance_port "$dir")
  label=$(python3 -c "import json;print(json.load(open('$dir/$PIDFILE')).get('label',''))" 2>/dev/null || true)
  pid=$(instance_pid "$dir")
  if [[ -n "$label" && "$(uname -s)" == "Darwin" ]]; then
    launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
  elif [[ -n "$pid" ]]; then
    kill "$pid" 2>/dev/null || true
  fi
  tailnet_unserve "$port"
  rm -f "$dir/$PIDFILE" "$dir/.test-instance.plist"
  if [[ -n "$label" ]]; then
    rm -f "$HOME/Library/LaunchAgents/$label.plist"
  fi
  [[ -n "$pid" ]] && echo "stopped (pid $pid)" || echo "not running"
}

print_instance_urls() {
  local port=$1 host
  echo "test instance up:  http://localhost:$port  (passive, cloned data)"
  echo "local stage:       http://localhost:$port/stage.html"
  host=$(tailnet_host)
  if [[ -n "$host" ]]; then
    echo "tailnet stage:     http://$host:$port/stage.html   <- desktop review"
    echo "tailnet mobile:    http://$host:$port/              <- phone / tablet"
  fi
  echo "                   (stage: 1280 desktop canvas + 402x874 mobile side)"
}

# Record what main was when this branch forked. Cheap now, decisive later: if
# main's history is ever REWRITTEN, this is the only way to tell "main moved"
# (rebase onto it) from "your base no longer exists" (rebase --onto it). Lives
# in the live .git dir, so it is never inside a worktree and never tracked.
record_base() {
  local d="$LIVE/.git/vira-bases"
  mkdir -p "$d" && git -C "$LIVE" rev-parse main > "$d/$1" 2>/dev/null || true
}

cmd_start() {
  slug_check "$1"
  local dir; dir=$(wt_dir "$1")
  [[ -e "$dir" ]] && { echo "error: $dir already exists" >&2; exit 1; }
  mkdir -p "${dir:h}"
  git -C "$LIVE" worktree add -b "claude/$1" "$dir" main
  provision "$dir"
  record_base "$1"
  echo ""
  echo "branch  claude/$1"
  echo "worktree $dir"
  echo "next: work in the worktree. Test-drive with: scripts/branch.sh serve $1"
}

# Bring a worktree this script didn't create under the same discipline: give it
# the venv symlink and the CLAUDE.md/launch.json copies `start` would have.
# With no slug, adopts the worktree the caller is standing in.
cmd_adopt() {
  local dir slug=""
  if [[ $# -ge 1 ]]; then
    slug_check "$1"; slug=$1; dir=$(wt_dir "$1")
    [[ -d "$dir" ]] || {
      echo "error: no worktree checked out on claude/$1" >&2; exit 1; }
  else
    dir=$(git rev-parse --show-toplevel 2>/dev/null) || {
      echo "error: not inside a checkout — pass a slug" >&2; exit 1; }
    slug=$(git -C "$dir" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    slug=${slug#claude/}
  fi
  [[ "$dir" == "$LIVE" ]] &&
    { echo "error: refusing to adopt the live tree" >&2; exit 1; }
  provision "$dir"
  echo "provisioned $dir"
  if [[ -n "$slug" ]]; then
    echo "next: scripts/branch.sh serve $slug"
  fi
}

# clone_data <src-data-dir> <dst-data-dir>
#
# An instant APFS clone of live data. Disposable; never shared. The source is
# a RUNNING server, so it churns while the copy walks it — three rules keep
# that from killing the clone:
#
#   - sqlite sidecars (-shm/-wal) are never copied. They appear and vanish as
#     the server checkpoints (a vanished media-index.sqlite-wal used to abort
#     the whole script under `set -e`), they rebuild themselves on open, and
#     pairing a mid-transaction WAL with a separately-copied database would
#     make the snapshot less consistent, not more.
#   - every other top-level entry is copied on its own, so one entry's churn
#     can't truncate the walk. A copy error is fatal only if the source still
#     exists; an entry that disappeared mid-clone was transient and simply
#     isn't part of this point-in-time snapshot.
#   - the snapshot is built in a staging directory and moved into place in a
#     single rename, so a failure can never leave a half-copied data/ behind
#     for the next serve to trip over.
#
# The .test-snapshot marker is written last, inside the stage: it distinguishes
# a real snapshot from a stray data/ created by module imports (e.g. running
# the test suite), and it only ever appears on a complete clone.
clone_data() {
  local src=$1 dst=$2 stage="${2:h}/.data-snapshot.tmp" name churn=0
  local -a entries
  rm -rf "$dst" "$stage"
  mkdir -p "$stage"
  entries=("$src"/*(DN:t))
  for name in $entries; do
    [[ "$name" == *-shm || "$name" == *-wal ]] && continue
    cp -Rc "$src/$name" "$stage/$name" 2>/dev/null || churn=1
  done
  for name in $entries; do
    [[ "$name" == *-shm || "$name" == *-wal ]] && continue
    [[ -e "$stage/$name" ]] && continue
    [[ -e "$src/$name" ]] || continue           # vanished mid-clone; not ours
    echo "error: data clone incomplete — could not copy $name from $src" >&2
    rm -rf "$stage"
    return 1
  done
  (( churn )) && echo "  (source changed mid-clone; affected entries skipped or partial)"
  find "$stage" \( -name '*-shm' -o -name '*-wal' \) -delete
  rm -f "$stage/launchd.log"
  date > "$stage/.test-snapshot"
  rm -rf "$dst"
  mv "$stage" "$dst"
}

# A neutral preview for cases where publishing a personal data clone has not
# been explicitly approved. It exercises fixture CRM plus a tiny synthetic
# vault, so Find chat and its companion windows are testable without exposing
# any owner data to another device or network transport.
fixture_data() {
  local dir=$1 dst="$1/data" stage="$1/.data-snapshot.tmp"
  rm -rf "$dst" "$stage"
  mkdir -p "$stage/test-vault/wiki" "$stage/test-vault/Sessions"
  python3 - "$stage/config.json" "$dst/test-vault" <<'PY'
import json
import sys
from pathlib import Path

config_path, vault_path = sys.argv[1:]
Path(config_path).write_text(json.dumps({
    "fixture_mode": True,
    "vault_root": vault_path,
    "vault_dirs": ["wiki", "Sessions"],
}, indent=2), encoding="utf-8")
PY
  print -r -- '# Durable previews

The preview server stays loopback-only. Tailscale Serve makes it available
only to authenticated devices in the same tailnet.

The test environment uses synthetic notes unless the owner explicitly approves
a cloned personal-data snapshot.' > "$stage/test-vault/wiki/Durable previews.md"
  print -r -- '# Find integration session

Find keeps deterministic search and one-shot Ask. Chat with my vault starts a
persistent conversation. Concept Cloud and Related are session-linked companion
windows on desktop and internal tabs on mobile.' > "$stage/test-vault/Sessions/Find integration.md"
  date > "$stage/.test-snapshot"
  mv "$stage" "$dst"
  (cd "$dir" && VIRA_PASSIVE=1 "$LIVE/.venv/bin/python" -c \
    'from server import vault; print(vault.scan_once())') >/dev/null
}

cmd_serve() {
  slug_check "$1"
  local fresh=${2:-} dir port pid
  dir=$(wt_dir "$1")
  [[ -d "$dir" ]] || { echo "error: no worktree at $dir (run start first)" >&2; exit 1; }
  [[ "$dir" == "$LIVE" ]] && { echo "error: refusing to serve the live tree" >&2; exit 1; }
  provision "$dir"          # a worktree from elsewhere may still lack the venv
  pid=$(instance_pid "$dir")
  [[ -n "$pid" ]] && { echo "already running (pid $pid, port $(instance_port "$dir"))"; exit 0; }

  if [[ "$fresh" == "--fixture" ]]; then
    echo "building synthetic fixture snapshot..."
    fixture_data "$dir" || exit 1
  elif [[ "$fresh" == "--fresh" || ! -f "$dir/data/.test-snapshot" ]]; then
    echo "cloning data snapshot (APFS copy-on-write)..."
    clone_data "$LIVE/data" "$dir/data" || exit 1
  fi

  # First free port in the test range.
  port=""
  for p in $(seq $PORT_MIN $PORT_MAX); do
    lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1 || { port=$p; break; }
  done
  [[ -n "$port" ]] || { echo "error: no free port in $PORT_MIN-$PORT_MAX" >&2; exit 1; }

  # Passive: no background workers, no outbound sends (server-side gate).
  # Uvicorn stays loopback-only; Tailscale Serve is the authenticated bridge.
  # launchd keeps Mac previews alive after the agent terminal goes away and
  # restarts them on a crash. Other platforms retain the nohup fallback.
  pid=$(start_test_process "$1" "$dir" "$port")

  for i in $(seq 1 40); do
    curl -sf -o /dev/null "http://127.0.0.1:$port/" && break
    kill -0 "$pid" 2>/dev/null || { echo "error: instance died — see $dir/.test-instance.log" >&2; exit 1; }
    sleep 0.5
  done
  curl -sf -o /dev/null "http://127.0.0.1:$port/" || {
    echo "error: no response on :$port — see $dir/.test-instance.log" >&2; exit 1; }
  tailnet_serve "$port" || {
    stop_test_process "$dir" >/dev/null
    echo "error: Tailscale could not expose :$port to the tailnet" >&2
    exit 1
  }
  echo ""
  # Health checks stay numeric and local. Human links use localhost on this
  # Mac (Browser allows it) and MagicDNS everywhere else.
  print_instance_urls "$port"
  echo "log: $dir/.test-instance.log    stop: scripts/branch.sh stop $1"
}

cmd_stop() {
  slug_check "$1"
  local dir; dir=$(wt_dir "$1")
  stop_test_process "$dir"
}

cmd_list() {
  local br dir pid port ab tail
  tail=$(tailnet_host)
  echo "live: $LIVE (port 8377, launchd)"
  git -C "$LIVE" worktree list --porcelain | awk '/^worktree /{wt=$2} /^branch /{print wt, $2}' |
  while read -r dir br; do
    [[ "$dir" == "$LIVE" ]] && continue
    br=${br#refs/heads/}
    ab=$(git -C "$LIVE" rev-list --left-right --count "main...$br" 2>/dev/null | awk '{print "behind "$1" / ahead "$2}')
    pid=$(instance_pid "$dir"); port=$(instance_port "$dir")
    if [[ -n "$pid" ]]; then
      if [[ -n "$tail" ]]; then
        echo "  $br  ->  $dir  [$ab]  RUNNING :$port  http://$tail:$port/"
      else
        echo "  $br  ->  $dir  [$ab]  RUNNING :$port"
      fi
    else
      echo "  $br  ->  $dir  [$ab]"
    fi
  done
}

cmd_merge() {
  slug_check "$1"
  local dir branch="claude/$1"; dir=$(wt_dir "$1")
  git -C "$LIVE" show-ref --verify --quiet "refs/heads/$branch" || {
    echo "error: no branch $branch" >&2; exit 1; }

  # Preflight: both trees clean, instance down.
  [[ -n "$(git -C "$LIVE" status --porcelain)" ]] && {
    echo "error: live tree has uncommitted changes — resolve first" >&2; exit 1; }
  if [[ -d "$dir" && -n "$(git -C "$dir" status --porcelain)" ]]; then
    echo "error: worktree $dir has uncommitted changes — commit or stash first" >&2; exit 1; fi
  local pid; pid=$(instance_pid "$dir" 2>/dev/null)
  [[ -f "$dir/$PIDFILE" ]] && {
    echo "stopping test instance${pid:+ (pid $pid)}"
    stop_test_process "$dir"
  }

  # The checks that CLAUDE.md used to only ask for. Each one is a bug that
  # already shipped once; see scripts/preflight.sh --list. A MISSING preflight
  # is announced rather than silently skipped — an absent check that reads as a
  # pass is the exact failure mode this whole gate exists to end.
  if [[ ! -f "$LIVE/scripts/preflight.sh" ]]; then
    echo "NOTE: scripts/preflight.sh not present — merging WITHOUT preflight checks."
  elif ! PREFLIGHT_SLUG="$1" bash "$LIVE/scripts/preflight.sh" --pre-merge "$1"; then
    echo ""
    if [[ "${VIRA_SKIP_PREFLIGHT:-}" == "1" ]]; then
      echo "VIRA_SKIP_PREFLIGHT=1 — proceeding over the failures above."
    else
      echo "preflight failed — merge refused. Fix the above, or override with:"
      echo "  VIRA_SKIP_PREFLIGHT=1 scripts/branch.sh merge $1"
      exit 1
    fi
  fi

  echo "merging $branch into main..."
  if ! git -C "$LIVE" merge --no-ff "$branch" -m "Merge branch '$branch'"; then
    git -C "$LIVE" merge --abort
    echo ""
    echo "CONFLICT — merge aborted, live tree restored. Resolve in-session:"
    local base n
    base="$(cat "$LIVE/.git/vira-bases/$1" 2>/dev/null || true)"
    n=$(git -C "$LIVE" rev-list --count "main..$branch" 2>/dev/null || echo 0)
    # If the branch would contribute far more commits than anyone wrote, its
    # base is gone (main was rewritten) and a plain rebase replays the whole
    # dead lineage — measured 2026-07-25: 146 commits, stopped mid-rebase.
    if [[ -n "$base" ]] && ! git -C "$LIVE" merge-base --is-ancestor "$base" main 2>/dev/null; then
      echo "  NOTE: main's history was REWRITTEN since this branch forked."
      echo "  cd $dir && git rebase --onto main $base   # NOT 'git rebase main'"
    elif [[ "$n" -gt 25 ]]; then
      echo "  NOTE: $branch would contribute $n commits — its base looks dead."
      echo "  cd $dir && git rebase --onto main \$(git merge-base main $branch)"
    else
      echo "  cd $dir && git rebase main   # fix conflicts, re-verify, then merge again"
    fi
    exit 1
  fi

  echo ""
  echo "merged. Post-merge checklist:"
  # CLAUDE.md is gitignored (the repo is public), so a spec line NEVER rides a
  # merge. Two ways that goes wrong, and the silent one used to be invisible:
  # an unprovisioned worktree has no copy at all, which means the session
  # worked without the spec and any line it proposed lives only in its report.
  if [[ -d "$dir" && ! -f "$dir/CLAUDE.md" ]]; then
    echo "  [ ] this worktree had NO CLAUDE.md — the session never read the"
    echo "      spec. Check its report for proposed spec lines and apply them"
    echo "      to $LIVE/CLAUDE.md by hand; run 'branch.sh adopt' next time."
  elif [[ -f "$dir/CLAUDE.md" ]] && ! diff -q "$LIVE/CLAUDE.md" "$dir/CLAUDE.md" >/dev/null 2>&1; then
    echo "  [ ] CLAUDE.md differs (gitignored — git did NOT carry it). Port by hand:"
    echo "      diff $LIVE/CLAUDE.md $dir/CLAUDE.md"
  fi
  if git -C "$LIVE" diff --name-only ORIG_HEAD..HEAD | grep -q "^server/"; then
    echo "  [ ] server code changed — restart live:"
    echo "      launchctl kickstart -k gui/501/nyc.durham.vira"
  fi
  echo "  [ ] push:     git -C $LIVE push"
  echo "  [ ] teardown: scripts/branch.sh discard $1"
}

cmd_discard() {
  slug_check "$1"
  local force=${2:-} dir branch="claude/$1"; dir=$(wt_dir "$1")
  local pid; pid=$(instance_pid "$dir" 2>/dev/null)
  [[ -f "$dir/$PIDFILE" ]] && stop_test_process "$dir"
  if [[ -d "$dir" ]]; then
    # data/ and .venv are gitignored, so remove needs --force even when the
    # tracked tree is clean — but refuse if there are uncommitted TRACKED changes
    # unless the caller passed --force.
    if [[ -n "$(git -C "$dir" status --porcelain)" && "$force" != "--force" ]]; then
      echo "error: $dir has uncommitted changes. Re-run with --force to discard them." >&2
      exit 1
    fi
    rm -rf "$dir/data"
    git -C "$LIVE" worktree remove --force "$dir"
  fi
  if git -C "$LIVE" show-ref --verify --quiet "refs/heads/$branch"; then
    git -C "$LIVE" branch -d "$branch" 2>/dev/null ||
      { echo "branch $branch is unmerged; deleting anyway (recoverable from reflog)";
        git -C "$LIVE" branch -D "$branch"; }
  fi
  echo "discarded $branch"
}

# Sourced rather than run (tests/test_branch_clone.py drives clone_data against
# a synthetic source tree): define the functions, dispatch nothing.
[[ "$ZSH_EVAL_CONTEXT" == *file* ]] && return 0

[[ $# -lt 1 ]] && usage
cmd=$1; shift
case "$cmd" in
  start)   [[ $# -ge 1 ]] || usage; cmd_start "$@";;
  adopt)   cmd_adopt "$@";;
  serve)   [[ $# -ge 1 ]] || usage; cmd_serve "$@";;
  stop)    [[ $# -ge 1 ]] || usage; cmd_stop "$@";;
  list)    cmd_list;;
  merge)   [[ $# -ge 1 ]] || usage; cmd_merge "$@";;
  discard) [[ $# -ge 1 ]] || usage; cmd_discard "$@";;
  *) usage;;
esac
