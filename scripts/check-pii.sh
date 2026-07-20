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
# audits and CI, where only the generic built-ins apply since the
# instance patterns file is git-ignored).
if [ "${1:-}" = "--tree" ]; then
    DIFF="$(git grep -I -n -E '.' -- . ':(exclude)scripts/check-pii.sh' \
            || true)"
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

fail=0
check() {
    hits="$(printf '%s\n' "$DIFF" | grep -E -- "$1" || true)"
    if [ -n "$hits" ]; then
        echo "PII guard: staged lines match /$1/:" >&2
        printf '%s\n' "$hits" | head -5 >&2
        fail=1
    fi
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
    echo "Commit blocked. Scrub the lines above, or bypass a false positive" >&2
    echo "with: git commit --no-verify" >&2
    exit 1
fi
exit 0
