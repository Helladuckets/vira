"""Job naming — the session title and the first-command line.

Claude Code picks a short name for every session and echoes the first
command inline under its welcome box; Vira mirrors both. They are
DERIVED from the launch record (no model call — a job is named the
instant it starts), and the title is EDITABLE (joblog.set_title). The
title is the one canonical name for a job and is used everywhere the job
appears: the terminal title bar, the Jobs list and history, the change
log, the retro.

Two strings, from the same source material:

  command(record)  — the human intent behind the job, shown as the first
    `> …` line in the terminal (fuller). Idea dispatches read as the
    idea; routine runs as the routine; right-click asks quote the
    owner's question; free-form Actions runs as what was typed; only a
    truly anonymous job falls back to the prompt head. Immutable — it is
    what was asked.
  name(record)     — the short, editable session title. Defaults to a
    squeezed form of the command; a stored `title` (an owner edit) wins.

The `\"\"\"…\"\"\"` block that Vira's idea/ask prompts wrap the human text
in is the primary signal, so even a job whose idea was later deleted
still names itself from what was asked.
"""
import re

# Prompt scaffolding lines that are never the human intent — skipped when
# falling back to the prompt head for a free-form job.
_PREAMBLE = re.compile(
    r"^(you are |investigate|prefer read-only|finish with|click context|"
    r"the owner asks|this task comes from|research only|output only|"
    r"- component:|- person:|- text at|carry it out|end with|\"\"\")",
    re.I)

# "You are Vira's subs-visuals apply agent, running headless …" → the role
# ("subs-visuals apply agent"). Names machine-composed agent prompts by what
# the agent IS, since their role preamble spills across lines with no clean
# human ask to quote.
_ROLE = re.compile(
    r"^\s*you are\s+(?:vira'?s?\s+|an?\s+|the\s+)*(.+?)"
    r"(?:,|\s+running\b|\s+that\b|\s+which\b|\s+working\b|\s+spawned\b|"
    r"\s+inside\b|\.|\n|$)",
    re.I)


def _collapse(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _short(text, limit):
    """Truncate to `limit` chars on a word boundary, trailing '…' if cut."""
    text = _collapse(text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
    return (cut or text[:limit]).rstrip() + "…"


def _quoted(prompt):
    """The first triple-quoted block — where Vira wraps the human text."""
    m = re.search(r'"""\s*(.+?)\s*"""', prompt or "", re.S)
    return _collapse(m.group(1)) if m else None


def _routine_name(routine_id):
    try:
        from . import routines
        r = next((x for x in routines.list_routines()
                  if x.get("id") == routine_id), None)
        return r.get("name") if r else None
    except Exception:  # noqa: BLE001 — naming must never raise
        return None


def _prompt_head(prompt):
    """The human intent, or — for a machine-composed agent prompt — its
    role. Quoted block first, then the 'You are …' role, then the first
    substantive line past the scaffolding."""
    quoted = _quoted(prompt)
    if quoted:
        return quoted
    m = _ROLE.match(prompt or "")
    if m:
        role = _collapse(m.group(1))
        if role:
            return role[0].upper() + role[1:]
    for line in (prompt or "").splitlines():
        line = line.strip()
        if line and not _PREAMBLE.match(line):
            return _collapse(line)
    return _collapse(prompt)


def command(record, idea_text=None):
    """The human first-command line for a job record."""
    meta = record.get("meta") or {}
    if meta.get("kind") == "map-refresh" or meta.get("routine_id") == "system-map":
        return "System map — refresh the registry from the change log"
    if meta.get("kind") == "judge" or meta.get("judge_of"):
        return "Judge — grade a finished job with fresh eyes"
    if meta.get("routine_id"):
        return "Routine — " + (_routine_name(meta["routine_id"])
                               or meta["routine_id"])
    if meta.get("stage"):
        return "Circuit step — " + str(meta["stage"])
    idea_text = idea_text or (_quoted(record.get("prompt", ""))
                              if record.get("idea_id") else None)
    if record.get("idea_id") and idea_text:
        verb = "Plan" if record.get("publish_plan") else "Implement"
        return f"{verb} — {_collapse(idea_text)}"
    head = _prompt_head(record.get("prompt", ""))
    quoted = _quoted(record.get("prompt", ""))
    if quoted and quoted == head and not record.get("idea_id"):
        return "Ask Vira — " + head
    return head or "(untitled job)"


def default_title(record, idea_text=None):
    """The short session name a job is auto-given (before any edit)."""
    return _short(command(record, idea_text), 64)


def name(record, idea_text=None):
    """The effective display name: an owner edit wins, else the default."""
    edited = (record.get("title") or "").strip()
    return edited or default_title(record, idea_text)
