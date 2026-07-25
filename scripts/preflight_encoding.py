"""Text IO without an explicit encoding — found by parsing, not grepping.

The bug this guards (2026-07-25): server/skins.py read a tracked stylesheet with
the platform default. On Windows that is cp1252, so applying a skin read the CSS
as cp1252 and wrote the mangled text back. A ROUND TRIP HIDES THIS — both ends
agree until one is fixed — which is why it survived every green CI run until the
reads were pinned and six tests promptly failed.

Why AST and not grep: a call whose `encoding=` sits on a continuation line is
invisible to a line-based scan, so grep reports it as a violation. A check that
cries wolf gets ignored, and an ignored check is worse than none — so this one
parses the code instead of reading lines of it.

Prints `path:line  expr` per violation and the total; exits 0 always. The
ratchet in preflight.sh owns the pass/fail decision, since the count is what
matters, not any individual site.
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCAN = ("server", "tests")
TEXT_METHODS = {"read_text", "write_text"}


def has_kw(node, name):
    return any(k.arg == name for k in node.keywords)


def binary_open(node):
    """open(..., 'rb') and friends take no encoding — not a violation."""
    mode = None
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        mode = node.args[1].value
    for k in node.keywords:
        if k.arg == "mode" and isinstance(k.value, ast.Constant):
            mode = k.value.value
    return isinstance(mode, str) and "b" in mode


def violations():
    out = []
    for pkg in SCAN:
        base = ROOT / pkg
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if isinstance(fn, ast.Attribute) and fn.attr in TEXT_METHODS:
                    if not has_kw(node, "encoding"):
                        out.append((path, node.lineno, f".{fn.attr}()"))
                elif isinstance(fn, ast.Name) and fn.id == "open":
                    if not binary_open(node) and not has_kw(node, "encoding"):
                        out.append((path, node.lineno, "open()"))
    return out


def main():
    v = violations()
    if "--count" in sys.argv:
        print(len(v))
        return 0
    for path, line, what in v:
        print(f"{path.relative_to(ROOT)}:{line}  {what}")
    print(f"total: {len(v)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
