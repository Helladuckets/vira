"""Change log — every shipped change per session, all the way back to the
first Vira session.

The per-session record already exists as the Vira session retros in
`~/TC-IL/Sessions/* vira.md` (each retro's `## Shipped` section is that
session's changes), so the change log is DERIVED from them at read time —
no parallel store to keep in sync, and it always reaches back to the
beginning. Resolved backlog items from the ideas store (status done /
dropped) are folded in too, so marking an idea done/dropped in the Ideas
tab shows up here immediately (under its session date, or a "recent /
unfiled" bucket until close-session writes that session's retro).

Claude jobs launched through Vira (Plan / Implement / cockpit runs) fold in
the same way from the durable job ledger (server/joblog.py) — prompt head,
target repo, outcome, and the claude session id that names the on-disk
transcript — so every agent-driven change is on the record even after the
in-memory jobs list is gone.

PROJECT-SCOPED (2026-07-12): this change log is Vira's only. The ideas
store and the job ledger both carry cross-project entries (ideas have a
`project` field; jobs run against any target repo), so both are filtered
before folding in — ideas to `project == "Vira"`, jobs to those that ran
in the Vira checkout or were dispatched from a Vira-project idea. Other
projects get their own changelogs in their own homes; nothing foreign
lands here.

Read-only: `GET /api/changelog` → { groups: [ {date, time, goal, entries:
[{text, kind}]} ] }, newest first. kind ∈ {ship, done, dropped, job}.
"""
import re
from pathlib import Path

from . import ideas as ideasstore
from . import joblog

SESSIONS = Path.home() / "TC-IL" / "Sessions"
PROJECT = ideasstore.DEFAULT_PROJECT          # "Vira"
REPO = Path(__file__).resolve().parent.parent  # this checkout


def _is_project_idea(it):
    return (it.get("project") or PROJECT).strip().lower() == PROJECT.lower()


def _is_project_cwd(cwd):
    if not cwd:
        return False
    try:
        return Path(cwd).expanduser().resolve() == REPO.resolve()
    except OSError:
        return False


def _clean(s):
    s = re.sub(r"`([^`]*)`", r"\1", s)      # drop code ticks
    s = s.replace("**", "")                  # drop bold markers
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _parse_retro(path):
    text = path.read_text(errors="ignore")
    m = re.search(r"^date:\s*(.+)$", text, re.M)
    date = m.group(1).strip() if m else ""
    m = re.search(r'^time:\s*"?([0-9:]+)"?', text, re.M)
    time = m.group(1).strip() if m else ""

    goal = ""
    gm = re.search(r"^##\s+Goal\s*\n+(.+?)(?=\n\n|\n##|\Z)", text, re.S | re.M)
    if gm:
        goal = _clean(gm.group(1))

    entries = []
    sm = re.search(r"^##\s+Shipped\s*\n(.*?)(?=^##\s|\Z)", text, re.S | re.M)
    if sm:
        cur = None
        for line in sm.group(1).splitlines():
            if re.match(r"^- ", line):
                if cur is not None:
                    entries.append(cur)
                cur = line[2:]
            elif re.match(r"^\s+-\s", line) and cur is not None:
                cur += " · " + line.strip()[2:]
            elif line.strip() and cur is not None and not line.startswith("#"):
                cur += " " + line.strip()
        if cur is not None:
            entries.append(cur)
    entries = [{"text": _clean(e), "kind": "ship"} for e in entries if _clean(e)]
    return {"date": date, "time": time, "goal": goal, "entries": entries}


def _job_entry(r, idea_texts):
    # The job's canonical name (an owner edit wins) heads the entry — the
    # same name the terminal title bar and Jobs list show, so a rename in
    # one place is the name everywhere. joblog.name reads the stored
    # title, falling back to the derived default.
    label = joblog.name(r, idea_texts.get(r.get("idea_id")))
    # "orphaned" used to mean "killed by server restart"; since the durable
    # runner, jobs survive restarts — orphaned now means the runner died.
    status = {"done": "done", "error": "failed", "running": "running",
              "orphaned": "runner died (orphaned)"}.get(
        r.get("status"), r.get("status", ""))
    bits = [label + " — " + status,
            Path(r.get("cwd") or "").name or "~"]
    if r.get("session_id"):
        bits.append("session " + r["session_id"][:8])
    bits.append("job " + r["id"][:8])
    if r.get("model"):
        bits.append(r["model"])
    return {"text": " · ".join(bits), "kind": "job"}


def groups():
    retros = []
    if SESSIONS.exists():
        for f in sorted(SESSIONS.glob("* vira.md")):
            g = _parse_retro(f)
            if g["entries"]:
                retros.append(g)
    retros.sort(key=lambda g: (g["date"], g["time"]), reverse=True)

    # fold resolved backlog items into the session whose date matches their
    # updated date; anything with no matching session lands in an "unfiled"
    # bucket that sorts to the very top. Vira-project ideas only.
    unfiled = []
    project_ideas = {}      # id -> text, for job labels + membership
    for it in ideasstore.list_items():
        if not _is_project_idea(it):
            continue
        project_ideas[it["id"]] = it["text"]
        if it["status"] not in ("done", "dropped"):
            continue
        d = (it.get("updated") or "")[:10]
        entry = {"text": it["text"], "kind": it["status"]}
        match = next((g for g in retros if g["date"] == d), None)
        if match:
            match["entries"].insert(0, entry)
        else:
            unfiled.append(entry)

    # fold claude jobs in from the durable ledger, by launch date — only
    # jobs that ran in this checkout or came off a Vira-project idea
    for r in joblog.list_records():
        if not (_is_project_cwd(r.get("cwd"))
                or (r.get("idea_id") and r["idea_id"] in project_ideas)):
            continue
        d = (r.get("started") or "")[:10]
        entry = _job_entry(r, project_ideas)
        match = next((g for g in retros if g["date"] == d), None)
        if match:
            match["entries"].insert(0, entry)
        else:
            unfiled.append(entry)

    out = []
    if unfiled:
        out.append({"date": "", "time": "",
                    "goal": "Recent — not yet in a session retro",
                    "entries": unfiled})
    out.extend(retros)
    return out
