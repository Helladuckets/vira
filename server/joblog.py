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

Every record is named at launch (server/jobtitle.py): `command` is the
immutable human first-command line the terminal echoes, `title` is the
short, editable session name used everywhere the job appears (title bar,
Jobs list, change log, retro). set_title() overwrites `title` on an
owner edit; `command` never changes.

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


def list_records():
    return list(_read()["jobs"])


def recent(limit=100):
    """Newest-first slice for the Jobs window's History tab."""
    return list(reversed(_read()["jobs"]))[:max(1, min(int(limit), 500))]


def get_record(jid):
    return next((r for r in _read()["jobs"] if r["id"] == jid), None)


def record_launch(job):
    from . import jobtitle
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
    row["command"] = jobtitle.command(row)
    row["title"] = jobtitle.default_title(row)

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
