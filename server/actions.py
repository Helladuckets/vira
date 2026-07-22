"""Claude Code cockpit: enumerate the central library's skills and commands as
buttons. Scanning is deterministic file reads.

Running a job is the live-session registry in server/session.py
(session.sessions) — the Claude Agent SDK path with steering + permission
gating when available, the legacy subprocess --print path when not.
main.py's `jobs` handle points at that registry directly; the old
actions.Jobs wrapper (verbatim delegation) was deleted 2026-07-21.
"""
import re
from pathlib import Path

LIB = Path.home() / ".claude"


def _frontmatter(text):
    m = re.match(r"\s*---\n(.*?)\n---", text, re.S)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith((" ", "\t", "-")):
                k, _, v = line.partition(":")
                fm[k.strip()] = v.strip().strip("\"'")
    return fm


def _arg_fields(hint):
    """Per-action form fields from an argument-hint string. The library
    convention is bracketed tokens — "[dir] [vault_root] [--force]",
    "<video-url-or-path> [question]" — where <angle> means required and
    [square] optional. No hint (or an empty one) -> no declared fields and
    the UI falls back to a single free-text input."""
    if not hint:
        return []
    fields = []
    for m in re.finditer(r"<([^>]+)>|\[([^\]]+)\]", hint):
        required = m.group(1) is not None
        token = (m.group(1) or m.group(2)).strip()
        if not token:
            continue
        fields.append({
            "name": token,
            "required": required,
            "flag": token.startswith("--"),
        })
    return fields


def scan_library():
    """Skills + commands from ~/.claude, each with name/kind/description and
    any declared arg fields (from argument-hint frontmatter)."""
    items = []
    skills_dir = LIB / "skills"
    if skills_dir.exists():
        for sk in sorted(skills_dir.iterdir()):
            f = sk / "SKILL.md"
            if not f.is_file():
                continue
            fm = _frontmatter(f.read_text(errors="replace"))
            desc = fm.get("description", "")
            hint = fm.get("argument-hint", "")
            items.append({"name": sk.name, "kind": "skill",
                          "invoke": f"/{sk.name}",
                          "description": desc.split(". ")[0][:180],
                          "arg_hint": hint,
                          "arg_fields": _arg_fields(hint)})
    cmds_dir = LIB / "commands"
    if cmds_dir.exists():
        for f in sorted(cmds_dir.glob("*.md")):
            fm = _frontmatter(f.read_text(errors="replace"))
            desc = fm.get("description", "")
            hint = fm.get("argument-hint", "")
            items.append({"name": f.stem, "kind": "command",
                          "invoke": f"/{f.stem}",
                          "description": desc[:180],
                          "arg_hint": hint,
                          "arg_fields": _arg_fields(hint)})
    return items
