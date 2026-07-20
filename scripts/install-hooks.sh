#!/bin/sh
# Install the repo's git hooks (currently: the pre-commit PII guard).
set -eu
ROOT="$(git rev-parse --show-toplevel)"
HOOK="$ROOT/.git/hooks/pre-commit"
cat > "$HOOK" <<'EOF'
#!/bin/sh
exec "$(git rev-parse --show-toplevel)/scripts/check-pii.sh"
EOF
chmod +x "$HOOK" "$ROOT/scripts/check-pii.sh"
echo "pre-commit PII guard installed at $HOOK"
