#!/bin/sh
# Pre-commit PII guard: blocks a commit whose staged additions contain
# personal data. Two pattern sources:
#   1. Generic built-ins below (phones, home paths, personal-email domains).
#   2. data/pii-patterns.txt — one extended-regex per line, # comments —
#      holding THIS instance's real identifiers (names, tenant, numbers).
#      It is git-ignored, so the guard itself never leaks what it guards.
# Install via scripts/install-hooks.sh (or: ln -s ../../scripts/check-pii.sh
# .git/hooks/pre-commit). Bypass for a false positive: git commit --no-verify.
set -u

ROOT="$(git rev-parse --show-toplevel)"
PATTERNS_FILE="$ROOT/data/pii-patterns.txt"

# Two modes: default scans STAGED additions (the pre-commit hook);
# `--tree` scans every line of every tracked text file (publication
# audits, local sweeps, and CI — in CI only the generic built-ins apply
# since the instance patterns file is git-ignored). LICENSE is excluded
# from the tree scan: its attribution line is deliberate (public-release
# decision D8, 2026-07-20).
TREE=0
if [ "${1:-}" = "--tree" ]; then
    TREE=1
    DIFF="$(git grep -I -n -E '.' -- . ':(exclude)scripts/check-pii.sh' \
            ':(exclude)LICENSE' || true)"
else
    DIFF="$(git diff --cached -U0 --diff-filter=ACM -- . \
            ':(exclude)scripts/check-pii.sh' \
            | grep '^+' | grep -v '^+++' || true)"
fi
[ -z "$DIFF" ] && exit 0

# Documented placeholders are allowed (config.example.json, README samples),
# as is the NANP reserved-fiction phone block (555-0100..0199 in any area
# code) that test fixtures use.
DIFF="$(printf '%s\n' "$DIFF" | grep -v -E 'you@|example\.com|yourdomain|yourtenant|\+12125551234|[0-9]{3}[-.]?555[-.]?01[0-9]{2}' || true)"
[ -z "$DIFF" ] && exit 0

# Tree-mode lines carry a file:LINE: prefix from git grep -n, which a
# digit-bearing pattern would match (hitting the line number of a clean
# line, never its content). So patterns run against a prefix-stripped
# copy, and the located original line is used only for reporting.
# Staged lines carry no such prefix.
if [ "$TREE" -eq 1 ]; then
    CONTENT="$(printf '%s\n' "$DIFF" | sed 's/^[^:]*:[0-9][0-9]*://')"
    LABEL="tracked"
else
    CONTENT="$DIFF"
    LABEL="staged"
fi

fail=0
check() {
    nums="$(printf '%s\n' "$CONTENT" | grep -n -E -- "$1" | cut -d: -f1 || true)"
    [ -z "$nums" ] && return 0
    echo "PII guard: $LABEL lines match /$1/:" >&2
    printf '%s\n' "$DIFF" \
        | sed -n "$(printf '%sp;' $(printf '%s\n' "$nums" | head -5))" >&2
    fail=1
}

# Generic built-ins.
check '\+1[0-9]{10}'
check '[0-9]{3}[-.][0-9]{3}[-.][0-9]{4}'
check '/Users/[a-z][a-z0-9_-]*'
check '[a-z0-9._%+-]+@(gmail|icloud|yahoo|hotmail|outlook|me)\.com'

# Instance-specific patterns (never committed).
if [ -f "$PATTERNS_FILE" ]; then
    while IFS= read -r pat; do
        case "$pat" in ''|'#'*) continue;; esac
        check "$pat"
    done < "$PATTERNS_FILE"
fi

if [ "$fail" -ne 0 ]; then
    echo "" >&2
    if [ "$TREE" -eq 1 ]; then
        echo "Tree scan flagged the lines above. Scrub them before publishing." >&2
    else
        echo "Commit blocked. Scrub the lines above, or bypass a false positive" >&2
        echo "with: git commit --no-verify" >&2
    fi
    exit 1
fi
exit 0
