#!/bin/zsh
# Vira parallel-branch workflow. One feature = one branch = one worktree.
# The live instance (launchd, port 8377) only ever changes at a merge.
# See CLAUDE.md, section "Parallel feature branches".
#
#   branch.sh start <slug>     new branch claude/<slug> + worktree ../vira-<slug>
#   branch.sh adopt [slug]     provision a worktree this script didn't create
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

# $0 inside a zsh function is the FUNCTION name, not the script.
usage() { sed -n '2,15p' "${(%):-%x}"; exit 1; }

slug_check() {
  [[ "$1" =~ ^[a-z0-9][a-z0-9-]*$ ]] || {
    echo "error: slug must be kebab-case ([a-z0-9-])" >&2; exit 1; }
}

# The worktree holding claude/<slug>, WHEREVER it lives — asked of git rather
# than assumed. `start` puts worktrees at ../vira-<slug>, but a worktree made
# by something else (the app's worktree toggle creates them under
# .claude/worktrees/<slug>) is just as real, and serve/stop/discard used to
# fail on it with "no worktree at ../vira-<slug>". Falls back to the canonical
# path, which is what `start` creates and what `merge`/`discard` accept for a
# branch whose worktree is already gone.
wt_dir() {
  local d
  d=$(git -C "$LIVE" worktree list --porcelain |
      awk -v b="branch refs/heads/claude/$1" \
          '/^worktree /{wt=substr($0,10)} $0==b{print wt; exit}')
  if [[ -n "$d" ]]; then echo "$d"; else echo "$WORKSPACE/vira-$1"; fi
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
  provision "$dir"
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

cmd_serve() {
  slug_check "$1"
  local fresh=${2:-} dir port pid
  dir=$(wt_dir "$1")
  [[ -d "$dir" ]] || { echo "error: no worktree at $dir (run start first)" >&2; exit 1; }
  [[ "$dir" == "$LIVE" ]] && { echo "error: refusing to serve the live tree" >&2; exit 1; }
  provision "$dir"          # a worktree from elsewhere may still lack the venv
  pid=$(instance_pid "$dir")
  [[ -n "$pid" ]] && { echo "already running (pid $pid, port $(instance_port "$dir"))"; exit 0; }

  if [[ "$fresh" == "--fresh" || ! -f "$dir/data/.test-snapshot" ]]; then
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
