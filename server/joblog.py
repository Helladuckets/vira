"""Durable job ledger — every claude job Vira launches, recorded on disk.

data/jobs-log.json gets a record at launch, the claude session id as soon
as the CLI's init event names it (which makes the on-disk transcript at
~/.claude/projects/<cwd-slug>/<session>.jsonl findable ever after), and the
outcome at finish. Since the durable-runner build, jobs run as DETACHED
processes (server/runner.py) that outlive the server — so this module is
written for cross-process use: no in-memory cache (every operation re-reads
the store), and mutations serialize through an fcntl file lock
(server/filelock.py). The server writes the launch record; the runner
writes the session id and the finish; the supervisor writes orphan marks.

Orphan sweeping is NO LONGER automatic at import time — a runner process
imports this module too, and an import-time sweep would have marked every
other live job orphaned. The server's session supervisor calls
sweep_orphans(alive) at boot with the set of job ids it actually
re-attached to; only running records outside that set are dead.

The change log (server/changelog.py) folds these records in per date, so
every job — initial prompt, session id, outcome — shows in the Change log
tab alongside shipped work and resolved ideas. The Jobs window's History
tab renders them directly (GET /api/jobs/history).

Every record is named at launch (the Job naming section below — formerly
server/jobtitle.py): `command` is the immutable human first-command line
the terminal echoes, `title` is the short, editable session name used
everywhere the job appears (title bar, Jobs list, change log, retro).
set_title() overwrites `title` on an owner edit; `command` never changes.
The two strings derive from the launch record alone (no model call — a
job is named the instant it starts): idea dispatches read as the idea,
routine runs as the routine, right-click asks quote the owner's question,
machine-composed agent prompts name themselves by their "You are …" role,
and the triple-quoted block Vira's idea/ask prompts wrap the human text
in is the primary signal.

Record shape:
  { "id": job id, "session_id": claude session id ("" until init arrives),
    "transcript": absolute path to the CLI's session jsonl ("" until known),
    "prompt": full initial prompt, "cwd": str, "model": str|None,
    "permission_mode": str|None, "publish_plan": bool, "idea_id": str|None,
    "mode": str|None, "status": running|done|error|orphaned,
    "command": human first-command line, "title": short editable name,
    "started": ISO, "finished": ISO|None,
    "result": first 4000 chars of the job's final text }
"""
import json
import re
import threading
from datetime import datetime
from pathlib import Path

from .filelock import locked

STORE = Path(__file__).resolve().parent.parent / "data" / "jobs-log.json"
RESULT_CAP = 4000

_lock = threading.Lock()


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read():
    """Fresh read every time — runners and the server share this store, so
    a process-lifetime cache would clobber the other side's writes."""
    try:
        s = json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        s = {"jobs": []}
    if not isinstance(s, dict) or "jobs" not in s:
        s = {"jobs": []}
    return s


def _write(s):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_name(STORE.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
    tmp.replace(STORE)


def _mutate(fn):
    """Run fn(store) under both locks (threads in this process + other
    processes) against a fresh read; persist if fn returns truthy."""
    with _lock, locked(STORE):
        s = _read()
        if fn(s):
            _write(s)


def _transcript_path(cwd, session_id):
    # claude CLI writes session transcripts under a project dir whose name is
    # the cwd with path separators and dots flattened to dashes.
    slug = re.sub(r"[/._]", "-", cwd or str(Path.home()))
    return str(Path.home() / ".claude" / "projects" / slug
               / f"{session_id}.jsonl")


# ---------- Job naming (folded in from server/jobtitle.py, 2026-07-21) ----

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
        from . import routines  # lazy — routines imports joblog back
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
    """The human first-command line for a job record. Immutable — it is
    what was asked."""
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


# ---------- ledger operations ----------

def list_records():
    return list(_read()["jobs"])


def recent(limit=100):
    """Newest-first slice for the Jobs window's History tab."""
    return list(reversed(_read()["jobs"]))[:max(1, min(int(limit), 500))]


def get_record(jid):
    return next((r for r in _read()["jobs"] if r["id"] == jid), None)


def record_launch(job):
    row = {
        "id": job["id"], "session_id": "", "transcript": "",
        "prompt": job["prompt"], "cwd": job["cwd"],
        "model": job.get("model"),
        "permission_mode": job.get("permission_mode"),
        "publish_plan": bool(job.get("publish_plan")),
        "idea_id": job.get("idea_id"),
        "mode": job.get("mode"),
        "read_only": bool(job.get("read_only")),
        "meta": job.get("meta") or {},
        "status": "running", "started": _now(), "finished": None,
        "result": "",
    }
    # Name the job at launch — the first-command line (immutable) and the
    # default session title (editable via set_title). idea_text resolves
    # from the record's own quoted block, so no cross-store read is needed.
    row["command"] = command(row)
    row["title"] = default_title(row)

    def fn(s):
        s["jobs"].append(row)
        return True
    _mutate(fn)


def set_title(jid, title):
    """Rename a job (the owner's edit in the terminal title bar). Empty
    input clears the override so the derived default shows again. Returns
    the updated record, or None if the job is unknown."""
    title = (title or "").strip()[:120]
    out = {}

    def fn(s):
        r = next((r for r in s["jobs"] if r["id"] == jid), None)
        if not r:
            return False
        r["title"] = title
        out["rec"] = r
        return True
    _mutate(fn)
    return out.get("rec")


def record_session(jid, session_id):
    def fn(s):
        r = next((r for r in s["jobs"] if r["id"] == jid), None)
        if r and session_id and not r["session_id"]:
            r["session_id"] = session_id
            r["transcript"] = _transcript_path(r["cwd"], session_id)
            return True
        return False
    _mutate(fn)


def record_finish(jid, status, result_text=""):
    def fn(s):
        r = next((r for r in s["jobs"] if r["id"] == jid), None)
        if r:
            r["status"] = status
            r["finished"] = _now()
            r["result"] = (result_text or "")[:RESULT_CAP]
            return True
        return False
    _mutate(fn)


def record_judge(jid, verdict):
    """Stamp a fresh-eyes verdict (server/judge.py contract: grade, score,
    summary, findings, recommendation, judge_job) onto a job's record."""
    def fn(s):
        r = next((r for r in s["jobs"] if r["id"] == jid), None)
        if r:
            r["judge"] = verdict
            return True
        return False
    _mutate(fn)


def mark_orphaned(jid):
    """One dead job — the supervisor found its runner gone mid-flight."""
    def fn(s):
        r = next((r for r in s["jobs"] if r["id"] == jid), None)
        if r and r["status"] == "running":
            r["status"] = "orphaned"
            r["finished"] = r["finished"] or _now()
            return True
        return False
    _mutate(fn)


def sweep_orphans(alive=()):
    """Mark still-"running" records orphaned, EXCEPT the ids in `alive` —
    the detached runners the supervisor just re-attached to at boot. Called
    once at server boot by the session supervisor, never at import."""
    alive = set(alive)

    def fn(s):
        stale = [r for r in s["jobs"]
                 if r["status"] == "running" and r["id"] not in alive]
        for r in stale:
            r["status"] = "orphaned"
            r["finished"] = r["finished"] or _now()
        return bool(stale)
    _mutate(fn)
