#!/bin/zsh
# Vira parallel-branch workflow. One feature = one branch = one worktree.
# The live instance (launchd, port 8377) only ever changes at a merge.
# See CLAUDE.md, section "Parallel feature branches".
#
#   branch.sh start <slug>     new branch claude/<slug> + worktree ../vira-<slug>
#   branch.sh serve <slug>     test instance: cloned data, passive, port 8378+
#   branch.sh serve <slug> --fresh   re-clone data before serving
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
WORKSPACE=${LIVE:h}
PORT_MIN=8378
PORT_MAX=8399
PIDFILE=.test-instance.json

usage() { sed -n '2,14p' "$0"; exit 1; }

slug_check() {
  [[ "$1" =~ ^[a-z0-9][a-z0-9-]*$ ]] || {
    echo "error: slug must be kebab-case ([a-z0-9-])" >&2; exit 1; }
}

wt_dir() { echo "$WORKSPACE/vira-$1"; }

instance_pid() {  # prints pid if the worktree's instance is alive, else nothing
  local dir=$1 pid
  [[ -f "$dir/$PIDFILE" ]] || return 0
  pid=$(python3 -c "import json;print(json.load(open('$dir/$PIDFILE'))['pid'])" 2>/dev/null) || return 0
  kill -0 "$pid" 2>/dev/null && echo "$pid" || true
}

instance_port() {
  local dir=$1
  [[ -f "$dir/$PIDFILE" ]] || return 0
  python3 -c "import json;print(json.load(open('$dir/$PIDFILE'))['port'])" 2>/dev/null || true
}

cmd_start() {
  slug_check "$1"
  local dir; dir=$(wt_dir "$1")
  [[ -e "$dir" ]] && { echo "error: $dir already exists" >&2; exit 1; }
  git -C "$LIVE" worktree add -b "claude/$1" "$dir" main
  # Provision the gitignored pieces a session needs:
  # - the FDA-granted venv (never rebuild; symlink the live one)
  # - CLAUDE.md + .claude/ (the operational spec; a COPY — edits are ported
  #   back by hand at merge time because these files never ride git)
  ln -s "$LIVE/.venv" "$dir/.venv"
  cp "$LIVE/CLAUDE.md" "$dir/CLAUDE.md" 2>/dev/null || true
  mkdir -p "$dir/.claude"
  cp "$LIVE/.claude/launch.json" "$dir/.claude/launch.json" 2>/dev/null || true
  echo ""
  echo "branch  claude/$1"
  echo "worktree $dir"
  echo "next: work in the worktree. Test-drive with: scripts/branch.sh serve $1"
}

cmd_serve() {
  slug_check "$1"
  local fresh=${2:-} dir port pid
  dir=$(wt_dir "$1")
  [[ -d "$dir" ]] || { echo "error: no worktree at $dir (run start first)" >&2; exit 1; }
  [[ "$dir" == "$LIVE" ]] && { echo "error: refusing to serve the live tree" >&2; exit 1; }
  pid=$(instance_pid "$dir")
  [[ -n "$pid" ]] && { echo "already running (pid $pid, port $(instance_port "$dir"))"; exit 0; }

  # Data: an instant APFS clone of live data. Disposable; never shared.
  # The marker distinguishes a real snapshot from a stray data/ created by
  # module imports (e.g. running the test suite) — no marker means re-clone.
  if [[ "$fresh" == "--fresh" || ! -f "$dir/data/.test-snapshot" ]]; then
    rm -rf "$dir/data"
    echo "cloning data snapshot (APFS copy-on-write)..."
    cp -Rc "$LIVE/data" "$dir/data"
    rm -f "$dir/data/launchd.log"
    date > "$dir/data/.test-snapshot"
  fi

  # First free port in the test range.
  port=""
  for p in $(seq $PORT_MIN $PORT_MAX); do
    lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1 || { port=$p; break; }
  done
  [[ -n "$port" ]] || { echo "error: no free port in $PORT_MIN-$PORT_MAX" >&2; exit 1; }

  # Passive: no background workers, no outbound sends (server-side gate).
  # Local-only bind; reuses the live checkout's FDA-granted venv.
  cd "$dir"
  VIRA_PASSIVE=1 nohup "$LIVE/.venv/bin/uvicorn" server.main:app \
    --host 127.0.0.1 --port "$port" >> "$dir/.test-instance.log" 2>&1 &
  pid=$!
  print -r -- "{\"pid\": $pid, \"port\": $port}" > "$dir/$PIDFILE"

  for i in $(seq 1 40); do
    curl -sf -o /dev/null "http://127.0.0.1:$port/" && break
    kill -0 "$pid" 2>/dev/null || { echo "error: instance died — see $dir/.test-instance.log" >&2; exit 1; }
    sleep 0.5
  done
  curl -sf -o /dev/null "http://127.0.0.1:$port/" || {
    echo "error: no response on :$port — see $dir/.test-instance.log" >&2; exit 1; }
  echo ""
  echo "test instance up:  http://127.0.0.1:$port  (passive, cloned data)"
  echo "stage view:        http://127.0.0.1:$port/stage.html   <- open THIS one"
  echo "                   (Design Studio format: 1280 desktop canvas + 402x874 mobile side)"
  echo "log: $dir/.test-instance.log    stop: scripts/branch.sh stop $1"
}

cmd_stop() {
  slug_check "$1"
  local dir pid; dir=$(wt_dir "$1")
  pid=$(instance_pid "$dir")
  if [[ -n "$pid" ]]; then kill "$pid" && echo "stopped (pid $pid)"; else echo "not running"; fi
  rm -f "$dir/$PIDFILE"
}

cmd_list() {
  local br dir pid port ab
  echo "live: $LIVE (port 8377, launchd)"
  git -C "$LIVE" worktree list --porcelain | awk '/^worktree /{wt=$2} /^branch /{print wt, $2}' |
  while read -r dir br; do
    [[ "$dir" == "$LIVE" ]] && continue
    br=${br#refs/heads/}
    ab=$(git -C "$LIVE" rev-list --left-right --count "main...$br" 2>/dev/null | awk '{print "behind "$1" / ahead "$2}')
    pid=$(instance_pid "$dir"); port=$(instance_port "$dir")
    if [[ -n "$pid" ]]; then
      echo "  $br  ->  $dir  [$ab]  RUNNING :$port"
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
  [[ -n "$pid" ]] && { echo "stopping test instance (pid $pid)"; kill "$pid"; rm -f "$dir/$PIDFILE"; }

  echo "merging $branch into main..."
  if ! git -C "$LIVE" merge --no-ff "$branch" -m "Merge branch '$branch'"; then
    git -C "$LIVE" merge --abort
    echo ""
    echo "CONFLICT — merge aborted, live tree restored. Resolve in-session:"
    echo "  cd $dir && git rebase main   # fix conflicts, re-verify, then merge again"
    exit 1
  fi

  echo ""
  echo "merged. Post-merge checklist:"
  if [[ -f "$dir/CLAUDE.md" ]] && ! diff -q "$LIVE/CLAUDE.md" "$dir/CLAUDE.md" >/dev/null 2>&1; then
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
  [[ -n "$pid" ]] && { kill "$pid"; rm -f "$dir/$PIDFILE"; }
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

[[ $# -lt 1 ]] && usage
cmd=$1; shift
case "$cmd" in
  start)   [[ $# -ge 1 ]] || usage; cmd_start "$@";;
  serve)   [[ $# -ge 1 ]] || usage; cmd_serve "$@";;
  stop)    [[ $# -ge 1 ]] || usage; cmd_stop "$@";;
  list)    cmd_list;;
  merge)   [[ $# -ge 1 ]] || usage; cmd_merge "$@";;
  discard) [[ $# -ge 1 ]] || usage; cmd_discard "$@";;
  *) usage;;
esac
