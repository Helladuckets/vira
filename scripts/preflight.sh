#!/usr/bin/env bash
# Preflight — the executable form of what CLAUDE.md used to only ask for.
#
# WHY THIS EXISTS. Every process failure this repo has had was the same
# shape: a check existed but was weaker than it looked, and its failure
# reached nobody. The response each time was another paragraph of prose,
# which a session may or may not read and which nothing enforces. This file
# is the alternative: a lesson is encoded as a CHECK — a function that runs,
# names the incident that earned it, and prints the exact fix.
#
# ADDING A LESSON IS ADDING A ROW. Append the id to CHECKS, then write
# desc_<id>, incident_<id>, fix_<id>, and check_<id>. Nothing else. That is
# the whole point: the mechanism for encoding a lesson is cheap and uniform,
# so the process gets stronger instead of accumulating patches.
#
# A check must be CHEAP, DETERMINISTIC, and print a FIX. A check that cannot
# say what to do about a failure is a complaint, not a check.
#
# RATCHETS. Some debt is pre-existing and too large to fix in one go. Those
# checks compare against a baseline count in preflight-baseline.txt and fail
# only when the count RISES. Existing debt is tolerated; new debt is not, and
# the baseline is lowered as it gets paid down. A ratchet never has to be
# argued about in review.
#
# Usage:
#   scripts/preflight.sh --list              what is checked, and why
#   scripts/preflight.sh --all               everything (CI, publication audits)
#   scripts/preflight.sh --pre-merge [slug]  the gate branch.sh merge runs
#   scripts/preflight.sh <id> [<id>...]      one or more by name
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASELINE="$ROOT/scripts/preflight-baseline.txt"
cd "$ROOT" || exit 2

CHECKS=(base deps encoding pii ci)
PRE_MERGE=(base deps encoding pii ci)

SLUG="${PREFLIGHT_SLUG:-}"
fails=0
warns=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  ok    %-9s %s\n' "$1" "$2"; }
warn() { printf '  WARN  %-9s %s\n' "$1" "$2"; warns=$((warns+1)); }
bad()  { printf '  FAIL  %-9s %s\n' "$1" "$2"; fails=$((fails+1)); }

baseline_for() {   # baseline_for <id> -> integer (0 if unrecorded)
  local v; v=$(grep -E "^$1[[:space:]]" "$BASELINE" 2>/dev/null | awk '{print $2}')
  printf '%s' "${v:-0}"
}

# ---------------------------------------------------------------- base ----
desc_base="a branch's base still exists in main's history"
incident_base="2026-07-25: the public history was rewritten; three worktrees kept
      a dead base. A plain merge from one contributed 146 commits instead of 1
      and stopped mid-rebase. 'git rebase main' — what the protocol said to do —
      made it worse, because the lineages still share an ancestor so git does
      not refuse."
fix_base="git rebase --onto main <recorded-base>   # NOT 'git rebase main'"
check_base() {
  local slug="$SLUG" branch base n
  [[ -z "$slug" ]] && { ok base "no branch in context — skipped"; return 0; }
  branch="claude/$slug"
  git show-ref --verify --quiet "refs/heads/$branch" || {
    ok base "no branch $branch — skipped"; return 0; }

  base="$(cat "$ROOT/.git/vira-bases/$slug" 2>/dev/null || true)"
  if [[ -n "$base" ]] && git cat-file -e "$base" 2>/dev/null; then
    if git merge-base --is-ancestor "$base" main; then
      ok base "base ${base:0:9} is still in main"
    else
      bad base "recorded base ${base:0:9} is NOT in main — history was rewritten"
      say "        fix: cd <worktree> && git rebase --onto main $base"
    fi
    return 0
  fi

  # Legacy branch with no recorded base: fall back to the count tell. A real
  # feature branch contributes a handful of commits; a dead base contributes
  # its whole lineage.
  n=$(git rev-list --count "main..$branch" 2>/dev/null || echo 0)
  if [[ "$n" -gt 25 ]]; then
    bad base "$branch would contribute $n commits — its base is almost certainly dead"
    say "        fix: cd <worktree> && git rebase --onto main \$(git merge-base main $branch)"
  else
    ok base "$branch contributes $n commits (no recorded base; count looks sane)"
  fi
}

# ---------------------------------------------------------------- deps ----
desc_deps="every third-party module the code imports is declared in requirements.txt"
incident_deps="2026-07-24: tests imported Pillow, which was never declared. It was
      present on the dev Mac transitively via the media-index extras, so the suite
      passed locally and errored in CI for two days — and a fresh install got a
      dead Genre Studio. A test-only dependency is still a dependency."
fix_deps="add the distribution to requirements.txt (or to the extras note if truly optional)"
check_deps() {
  local out
  out=$(python3 "$ROOT/scripts/preflight_deps.py" 2>&1)
  local rc=$?
  if [[ $rc -eq 0 ]]; then ok deps "${out:-all imports declared}"
  else bad deps "undeclared imports:"; printf '%s\n' "$out" | sed 's/^/        /'; fi
}

# ------------------------------------------------------------ encoding ----
desc_encoding="no NEW text IO without an explicit encoding (ratchet)"
incident_encoding="2026-07-25: server/skins.py read style.css with the platform
      default. On Windows that is cp1252, so applying a skin would read a tracked
      stylesheet as cp1252 and write the mangled text back. A round trip HIDES
      this — both ends agree until one is fixed — so it stayed green for weeks."
fix_encoding="pass encoding=\"utf-8\" on BOTH the read and the write, in the same commit"
check_encoding() {
  # AST, not grep: encoding= on a continuation line is invisible to a
  # line-based scan, and a check that cries wolf gets ignored.
  local n base
  n=$(python3 "$ROOT/scripts/preflight_encoding.py" --count 2>/dev/null)
  [[ -z "$n" ]] && { bad encoding "scanner failed to run"; return 0; }
  base=$(baseline_for encoding)
  if [[ "$n" -gt "$base" ]]; then
    bad encoding "$n unencoded text-IO calls, baseline $base — this change adds $((n-base))"
    python3 "$ROOT/scripts/preflight_encoding.py" 2>/dev/null | head -6 | sed 's/^/        /'
    say "        fix: $fix_encoding"
  elif [[ "$n" -lt "$base" ]]; then
    warn encoding "$n calls, below the $base baseline — lower it to $n to lock the gain in"
  else
    ok encoding "$n unencoded calls, at baseline (no new debt)"
  fi
}

# ----------------------------------------------------------------- pii ----
desc_pii="no personal data in the tracked tree — and the scan says how strong it was"
incident_pii="2026-07-24: a docstring naming a contact and two employers shipped to
      the PUBLIC repo. data/pii-patterns.txt is gitignored, so CI ran with only the
      generic patterns and passed — a green CI was NOT evidence the tree was clean.
      The false comfort was the real bug, so this check now REFUSES to be silent
      about which mode it ran in."
fix_pii="scrub the line; if this is CI, note that a full scan needs the patterns file"
check_pii() {
  local mode out rc
  if [[ -f "$ROOT/data/pii-patterns.txt" ]]; then mode="FULL (generic + instance identifiers)"
  else mode="REDUCED (generic patterns only — names and companies are NOT checked)"; fi
  out=$(sh "$ROOT/scripts/check-pii.sh" --tree 2>&1); rc=$?
  if [[ $rc -ne 0 ]]; then
    bad pii "tracked tree has personal data [$mode]"
    printf '%s\n' "$out" | head -8 | sed 's/^/        /'
  elif [[ "$mode" == REDUCED* ]]; then
    warn pii "clean, but scan was $mode"
    say "        a pass here does NOT mean the tree is clean of names or companies."
    say "        Full strength needs data/pii-patterns.txt (gitignored by design)."
  else
    ok pii "clean [$mode]"
  fi
}

# ------------------------------------------------------------------ ci ----
desc_ci="the branch's own CI run is not red (the Windows job is the only Windows machine)"
incident_ci="2026-07-28: two branches merged hours apart, each shipping a POSIX
      assumption that only Windows could catch — an executable #! test helper
      (WinError 193) and a strict '>' against a clock whose resolution is 15.6ms
      there. Both suites were green on the Mac, both CI runs were red, and the
      merge gate never looked: it checked base/deps/encoding/pii and nothing
      else. Red CI blocked nothing, so red CI changed nothing."
fix_ci="gh run view --log-failed <run-id>, fix on the branch, push, re-run this"
check_ci() {
  # Deliberately NOT a blocker in three cases, because a check that cries
  # wolf gets ignored and then the real signal is ignored with it: CI cannot
  # grade itself, an unpushed commit is a legitimate local state, and a run
  # still in flight has no verdict yet. Only a real FAILING conclusion stops
  # a merge.
  local sha target to json st cc rid
  if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
    ok ci "running inside CI — skipped (a run cannot grade itself)"; return 0
  fi
  command -v gh >/dev/null 2>&1 || {
    warn ci "gh is not installed — CI status unknown"; return 0; }

  if [[ -n "$SLUG" ]] && git show-ref --verify --quiet "refs/heads/claude/$SLUG"; then
    target="claude/$SLUG"
  else
    target="HEAD"
  fi
  sha="$(git rev-parse "$target" 2>/dev/null)" || {
    warn ci "cannot resolve $target — CI status unknown"; return 0; }

  # A network call is the one thing here that can hang, and a wedged merge
  # gate is worse than an unchecked one. macOS has no timeout(1) by default.
  to=""
  if   command -v timeout  >/dev/null 2>&1; then to="timeout 20"
  elif command -v gtimeout >/dev/null 2>&1; then to="gtimeout 20"; fi

  json=$($to gh run list --commit "$sha" --limit 1 \
           --json status,conclusion,databaseId 2>/dev/null)
  if [[ -z "$json" || "$json" == "[]" ]]; then
    warn ci "CI has not run on ${sha:0:9} (${target#refs/heads/}) — push it to get a verdict"
    return 0
  fi
  st=$(printf '%s' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0].get("status") or "")' 2>/dev/null)
  cc=$(printf '%s' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0].get("conclusion") or "")' 2>/dev/null)
  rid=$(printf '%s' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0].get("databaseId") or "")' 2>/dev/null)

  if [[ "$st" != "completed" ]]; then
    warn ci "CI is still ${st:-pending} on ${sha:0:9} — no verdict yet, merging on your judgement"
    say  "        watch: gh run watch $rid"
  elif [[ "$cc" == "success" ]]; then
    ok ci "CI green on ${sha:0:9} (run $rid)"
  elif [[ "$cc" == "skipped" || "$cc" == "neutral" ]]; then
    ok ci "CI $cc on ${sha:0:9} — nothing to grade"
  else
    bad ci "CI is $cc on ${sha:0:9} (run $rid)"
    $to gh run view "$rid" --json jobs \
      --jq '.jobs[] | select(.conclusion != "success") | "        \(.conclusion)\t\(.name)"' \
      2>/dev/null | head -6
    say "        fix: gh run view --log-failed $rid"
    say "        the Windows job is the ONLY Windows machine — a green Mac suite is not evidence."
  fi
}

# --------------------------------------------------------------- driver ---
list_checks() {
  say "preflight checks — each one is a lesson an incident paid for"
  say ""
  for id in "${CHECKS[@]}"; do
    local d i; d="desc_$id"; i="incident_$id"
    printf '  %-9s %s\n' "$id" "${!d}"
    printf '      why: %s\n\n' "${!i}"
  done
  say "add a lesson: append an id to CHECKS, then desc_/incident_/fix_/check_."
}

run() {
  local ids=("$@")
  say "preflight: ${ids[*]}"
  for id in "${ids[@]}"; do
    if ! declare -F "check_$id" >/dev/null; then bad "$id" "no such check"; continue; fi
    "check_$id"
  done
  say ""
  if [[ $fails -gt 0 ]]; then
    say "$fails check(s) FAILED, $warns warning(s)."
    for id in "${ids[@]}"; do :; done
    say "Each failure above prints its fix. These are not style rules — every one"
    say "of them is a bug that already shipped once."
    return 1
  fi
  [[ $warns -gt 0 ]] && say "passed with $warns warning(s)." || say "all clear."
  return 0
}

case "${1:---all}" in
  --list)      list_checks ;;
  --all)       run "${CHECKS[@]}" ;;
  --pre-merge) SLUG="${2:-$SLUG}"; run "${PRE_MERGE[@]}" ;;
  -h|--help)   sed -n '2,30p' "${BASH_SOURCE[0]}" ;;
  *)           run "$@" ;;
esac
