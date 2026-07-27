"""Evidence Ledger — Vira's own build provenance, mined into interview-ready
case studies (problem -> how the owner directed the agent -> what shipped).

Two rungs, the find.py split:

  - mine() is RUNG 1: pure, deterministic, no model call. It parses session
    retros (~/TC-IL/Sessions/* vira.md and <repo>/retros/*.md), reads this
    checkout's git log, and reads the durable job ledger
    (data/jobs-log.json), then joins everything sharing a session's date
    into an EPISODE — raw material, not an audit. A missing retro source is
    dormant, with the reason named in status() (the frontdoor.py honesty
    rule), never an exception.

  - compose_episode() is RUNG 2: exactly ONE suggest.complete() call per
    episode, turning its raw material into a case study. The model's
    output is never trusted as-is — _clean_case validates it against the
    episode's own material before anything is saved: every citation must
    exactly match a retro filename, a commit sha, or a job id the episode
    actually carries, or it is DROPPED (never invented). This is the same
    grounded-or-held discipline as journal._pid_checker and
    resolver._grounded. Composing never auto-approves; a case starts as a
    draft the owner reviews.

Case studies are curated in data/evidence.json (draft -> approved ->
archived), editable in the UI, and exportable as clean plain text for
pasting into interview prep — a labeled block per case, ASCII punctuation
only (no markdown, no em-dashes).

No passive guard: mining is read-only (git log, file reads), and the store
write lands harmlessly in a test instance's cloned data/. compose_episode
spends model tokens the same way a reply draft does, and passive instances
already allow that class of call — so there is nothing here for
VIRA_PASSIVE to refuse.
"""
import itertools
import re
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from . import jsonstore, settings
from .changelog import _clean
from .gitutil import git

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "evidence.json"
REPO_RETROS_DIR = ROOT / "retros"
DEFAULT_RETRO_DIR = Path.home() / "TC-IL" / "Sessions"

CASE_STATUSES = ("draft", "approved", "archived")
UPDATE_FIELDS = ("title", "problem", "direction", "outcome", "skills", "status")
REQUIRED_CASE_FIELDS = ("title", "problem", "direction", "outcome")

MAX_TITLE = 120
MAX_PROSE = 2000
MAX_SKILLS = 8
MAX_SKILL_CHARS = 80
MAX_CITATIONS = 10
JOB_PROMPT_HEAD = 400
BUILDLOG_HEAD_LINES = 30
COMPOSE_LIMIT_DEFAULT = 5

SECTION_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*\n(.*?)(?=^##[ \t]|\Z)", re.S | re.M)
DATE_RE = re.compile(r"^date:\s*(.+)$", re.M)
TIME_RE = re.compile(r'^time:\s*"?([0-9:]+)"?', re.M)
FRONTMATTER_TYPE_RE = re.compile(r"^type:\s*(build-log|feature)\s*$", re.M)
H1_RE = re.compile(r"^#\s+(.+)$", re.M)

_ASCII_MAP = str.maketrans({
    "—": "-", "–": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "…": "...",
})


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _retro_dir():
    """The default TC-IL Sessions dir, or an owner-configured override.
    Dormant (empty episode list) rather than raising when it is missing —
    every other install simply has no session retros yet."""
    raw = str(settings.get("evidence_retro_dir") or "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_RETRO_DIR


# ---------------------------------------------------------------- rung 1

def _parse_retro_full(path):
    """Frontmatter date/time plus EVERY `## <heading>` section body, keyed
    by heading text. Parsed generically (not against a fixed heading list)
    because the session-retro skill's headings drift over time — losing a
    section to a rename would be worse than keeping one under an unusual
    name."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    m = DATE_RE.search(text)
    date = m.group(1).strip() if m else ""
    m = TIME_RE.search(text)
    time_ = m.group(1).strip() if m else ""
    sections = {}
    for sm in SECTION_RE.finditer(text):
        heading = _clean(sm.group(1))
        body = _clean(sm.group(2))
        if heading and body:
            sections[heading] = body
    return {"date": date, "time": time_, "sections": sections,
            "goal": sections.get("Goal", "")}


def _retro_files():
    """Every session retro across both sources, newest first: the primary
    `* vira.md` glob in the retro dir, and this repo's own git-ignored
    retros/*.md."""
    out = []
    d = _retro_dir()
    if d.exists():
        out += sorted(d.glob("* vira.md"))
    if REPO_RETROS_DIR.is_dir():
        out += sorted(REPO_RETROS_DIR.glob("*.md"))
    return out


def _git_log():
    """[{sha, date, subject}], newest first on this checkout's main branch.
    Empty list on any failure (bad returncode, timeout, missing git) —
    mining never raises over a git problem. Tests patch this function
    directly rather than the subprocess call underneath it."""
    try:
        res = git(ROOT, "log", "--first-parent", "main",
                  "--pretty=%H%x09%ad%x09%s", "--date=format:%Y-%m-%d",
                  timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if res.returncode != 0:
        return []
    out = []
    for line in res.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        sha, date, subject = parts
        out.append({"sha": sha, "date": date, "subject": subject})
    return out


def _is_project_cwd(cwd):
    # Duplicated from changelog._is_project_cwd (three lines) rather than
    # imported — that function is private to changelog.py, which this
    # module must never alter or depend on structurally.
    if not cwd:
        return False
    try:
        return Path(cwd).expanduser().resolve() == ROOT.resolve()
    except OSError:
        return False


def _jobs():
    """Jobs launched in THIS checkout, from the durable ledger — the
    prompt head is how the owner directed the agent that day."""
    from . import joblog
    out = []
    for r in joblog.list_records():
        if not _is_project_cwd(r.get("cwd")):
            continue
        out.append({
            "id": r.get("id"),
            "title": joblog.name(r),
            "status": r.get("status") or "",
            "prompt": (r.get("prompt") or "")[:JOB_PROMPT_HEAD],
            "started": r.get("started") or "",
        })
    return out


def _buildlogs():
    """Vault notes typed `build-log` or `feature` — read-only, heads only
    (30 lines is enough for frontmatter + the H1 title), so this stays
    cheap even at thousands of notes. Empty when no vault is connected."""
    raw = str(settings.get("vault_root") or "").strip()
    if not raw:
        return []
    root = Path(raw).expanduser()
    if not root.is_dir():
        return []
    out = []
    for p in root.rglob("*.md"):
        try:
            with p.open(encoding="utf-8", errors="ignore") as fh:
                head = "".join(itertools.islice(fh, BUILDLOG_HEAD_LINES))
        except OSError:
            continue
        if not FRONTMATTER_TYPE_RE.search(head):
            continue
        tm = H1_RE.search(head)
        dm = DATE_RE.search(head)
        out.append({
            "path": str(p),
            "title": (tm.group(1).strip() if tm else p.stem),
            "date": (dm.group(1).strip() if dm else ""),
        })
    return out


def _composed_keys():
    """Episode keys carrying a non-archived case — an archived case frees
    its episode to be recomposed without a force flag."""
    return {c["episode_key"] for c in list_cases()
            if c.get("status") != "archived"}


def mine():
    """Every episode, newest first: one per session retro, with the day's
    commits, jobs, and build-log notes attached by date. Unmatched commits
    or jobs are simply not shown — this is raw material, not an audit."""
    parsed = []
    for f in _retro_files():
        info = _parse_retro_full(f)
        if not info["sections"]:
            continue
        parsed.append({"key": f.stem, "retro_file": f.name, **info})
    parsed.sort(key=lambda e: (e["date"], e["time"]), reverse=True)

    commits = _git_log()
    jobs = _jobs()
    buildlogs = _buildlogs()
    composed = _composed_keys()

    out = []
    for ep in parsed:
        d = ep["date"]
        out.append({
            "key": ep["key"],
            "retro_file": ep["retro_file"],
            "date": d,
            "time": ep["time"],
            "goal": ep["goal"],
            "sections": ep["sections"],
            "commits": [c for c in commits if c["date"] == d] if d else [],
            "jobs": [j for j in jobs if (j["started"] or "")[:10] == d] if d else [],
            "buildlogs": [b for b in buildlogs if b["date"] == d] if d else [],
            "composed": ep["key"] in composed,
        })
    return out


def _episode(key):
    ep = next((e for e in mine() if e["key"] == key), None)
    if ep is None:
        raise KeyError(key)
    return ep


# ---------------------------------------------------------------- store

def _load():
    return jsonstore.read(STORE, {"cases": []})


def list_cases():
    return list(_load()["cases"])


def _get_case(cid, cases=None):
    cases = list_cases() if cases is None else cases
    return next((c for c in cases if c["id"] == cid), None)


def update_case(cid, fields):
    """Whitelisted edit (title/problem/direction/outcome/skills/status);
    stamps updated + edited. Raises KeyError on an unknown id."""
    result = {}

    def fn(s):
        c = _get_case(cid, s["cases"])
        if c is None:
            raise KeyError(cid)
        if fields.get("title") is not None:
            c["title"] = str(fields["title"]).strip()[:MAX_TITLE]
        for k in ("problem", "direction", "outcome"):
            if fields.get(k) is not None:
                c[k] = str(fields[k]).strip()[:MAX_PROSE]
        if fields.get("skills") is not None and isinstance(fields["skills"], list):
            c["skills"] = [str(x).strip()[:MAX_SKILL_CHARS] for x in fields["skills"]
                           if str(x).strip()][:MAX_SKILLS]
        if fields.get("status") is not None and fields["status"] in CASE_STATUSES:
            c["status"] = fields["status"]
        c["updated"] = _now()
        c["edited"] = True
        result["case"] = c
        return s

    jsonstore.mutate(STORE, fn, {"cases": []}, indent=1, ensure_ascii=False)
    return result["case"]


def delete_case(cid):
    def fn(s):
        if _get_case(cid, s["cases"]) is None:
            raise KeyError(cid)
        s["cases"] = [c for c in s["cases"] if c["id"] != cid]
        return s

    jsonstore.mutate(STORE, fn, {"cases": []}, indent=1, ensure_ascii=False)
    return {"removed": cid}


def _add_case(case):
    def fn(s):
        s["cases"].insert(0, case)
        return s
    jsonstore.mutate(STORE, fn, {"cases": []}, indent=1, ensure_ascii=False)


# ---------------------------------------------------------------- rung 2

COMPOSE_PROMPT = """You are helping {owner} build an "Evidence Ledger" -- \
interview-ready case studies that show how {owner} directs an AI coding \
agent to ship real software. This material is {owner}'s own private build \
log for one working session on {date}. The case study you write may be \
exported as plain text for interview prep and could leave this machine, so \
NEVER mention a third party, client, family member, or anyone besides \
{owner} and the AI agent by name.

Session goal:
{goal}

Session retro sections:
{sections}

Git commits from that day (repo: {repo_name}):
{commits}

Agent jobs launched that day:
{jobs}

Turn this into ONE case study in the shape: problem, how {owner} directed \
the agent, what shipped. Return STRICT JSON only, no prose around it:
{{
 "title": "<short case title, under 12 words>",
 "problem": "<the problem or gap being solved, grounded in the material above>",
 "direction": "<HOW {owner} steered the agent: specific instructions, \
corrections, or decisions he made, using only what the material above shows>",
 "outcome": "<what shipped -- concrete, grounded in the commits/sections above>",
 "skills": ["<3-8 short agent-management skills this episode demonstrates, \
e.g. 'scoping an ambiguous ask', 'catching a regression from a diff', \
'directing multi-step verification'>"],
 "citations": ["<copied EXACTLY from the retro filename, a commit sha (7+ \
chars), or a job id listed above -- only ones that actually appear above>"]
}}

Rules:
- Never invent metrics, dates, or outcomes not grounded in the material above.
- Never name anyone besides {owner} and the AI agent.
- direction must describe {owner}'s OWN steering, not what the agent decided
  on its own.
- Every citation must be copied exactly from the retro filename, the commit
  list, or the job list above -- an invented one will be discarded.
"""


def _format_sections(sections):
    return "\n\n".join(f"## {h}\n{b}" for h, b in sections.items()) or "(none)"


def _format_commits(commits):
    return "\n".join(f"- {c['sha'][:10]} {c['subject']}"
                     for c in commits) or "(none)"


def _format_jobs(jobs):
    lines = [f"- {j['id']} [{j['status']}] {j['title']}: {j['prompt'][:200]}"
             for j in jobs]
    return "\n".join(lines) or "(none)"


def _compose_prompt(episode):
    owner = settings.get("owner_name") or "the owner"
    return COMPOSE_PROMPT.format(
        owner=owner,
        date=episode["date"] or episode["key"],
        goal=episode["goal"] or "(not stated)",
        sections=_format_sections(episode["sections"]),
        repo_name=ROOT.name,
        commits=_format_commits(episode["commits"]),
        jobs=_format_jobs(episode["jobs"]))


def _grounded_citation(cite, episode):
    cite = (cite or "").strip()
    if not cite:
        return False
    if cite == episode.get("retro_file"):
        return True
    if len(cite) >= 7 and any(sha.startswith(cite) for sha in
                              (c["sha"] for c in episode["commits"])):
        return True
    if cite in {j["id"] for j in episode["jobs"]}:
        return True
    return False


def _clean_case(raw, episode):
    """The server validates what the model proposed (the
    journal._clean_unapplied / resolver._grounded discipline): required
    fields present and non-empty, length caps enforced, and every citation
    checked against the episode's own material. An ungrounded citation is
    dropped, never fabricated forward — if every citation drops the case
    still saves, flagged citations: []."""
    if not isinstance(raw, dict):
        raise ValueError("model output was not a JSON object")
    out = {}
    for k in REQUIRED_CASE_FIELDS:
        v = raw.get(k)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"missing required field: {k}")
        cap = MAX_TITLE if k == "title" else MAX_PROSE
        out[k] = v.strip()[:cap]
    skills = raw.get("skills")
    out["skills"] = ([str(s).strip()[:MAX_SKILL_CHARS] for s in skills if str(s).strip()]
                     [:MAX_SKILLS] if isinstance(skills, list) else [])
    cites = raw.get("citations")
    cites = [str(c).strip() for c in cites] if isinstance(cites, list) else []
    out["citations"] = [c for c in cites
                        if _grounded_citation(c, episode)][:MAX_CITATIONS]
    now = _now()
    out.update({
        "id": "ev_" + uuid.uuid4().hex[:10],
        "episode_key": episode["key"],
        "status": "draft",
        "created": now,
        "updated": now,
        "edited": False,
    })
    return out


def compose_episode(key, force=False):
    """One suggest.complete() call, validated, saved as a draft case.
    Raises KeyError on an unknown episode, ValueError("already composed")
    when a non-archived case already carries this key and force is not
    set."""
    from . import suggest
    episode = _episode(key)
    if episode["composed"] and not force:
        raise ValueError("already composed")
    raw = suggest._extract_json(suggest.complete(_compose_prompt(episode)))
    case = _clean_case(raw, episode)
    _add_case(case)
    return case


def compose_new(limit=COMPOSE_LIMIT_DEFAULT):
    """Sweep helper: compose up to `limit` uncomposed episodes, newest
    first. Returns a per-key ok/error report — one bad episode never stops
    the sweep."""
    results = []
    episodes = [e for e in mine() if not e["composed"]]
    for ep in episodes[:max(1, int(limit))]:
        try:
            case = compose_episode(ep["key"])
            results.append({"key": ep["key"], "ok": True, "case_id": case["id"]})
        except Exception as e:  # noqa: BLE001 — a bad episode must not kill the sweep
            results.append({"key": ep["key"], "ok": False, "error": str(e)[:200]})
    return results


# ---------------------------------------------------------------- export

def _ascii_clean(s):
    return (s or "").translate(_ASCII_MAP)


def _export_block(case):
    lines = [
        "CASE: " + _ascii_clean(case.get("title", "")),
        "",
        "PROBLEM:",
        _ascii_clean(case.get("problem", "")),
        "",
        "HOW I DIRECTED THE AGENT:",
        _ascii_clean(case.get("direction", "")),
        "",
        "WHAT SHIPPED:",
        _ascii_clean(case.get("outcome", "")),
    ]
    if case.get("citations"):
        lines += ["", "EVIDENCE: " + ", ".join(case["citations"])]
    return "\n".join(lines)


def export_case(cid):
    c = _get_case(cid)
    if c is None:
        raise KeyError(cid)
    return _export_block(c)


def export_approved():
    cases = [c for c in list_cases() if c.get("status") == "approved"]
    cases.sort(key=lambda c: c.get("updated", ""), reverse=True)
    return "\n\n".join(_export_block(c) for c in cases)


# ---------------------------------------------------------------- status

def status():
    retro_dir = _retro_dir()
    tc_retros = sorted(retro_dir.glob("* vira.md")) if retro_dir.exists() else []
    repo_retro_files = (sorted(REPO_RETROS_DIR.glob("*.md"))
                        if REPO_RETROS_DIR.is_dir() else [])
    cases = list_cases()
    return {
        "retro_dir": str(retro_dir),
        "retro_dir_exists": retro_dir.exists(),
        "retro_count": len(tc_retros),
        "repo_retros": len(repo_retro_files),
        "buildlog_count": len(_buildlogs()),
        "git_ok": (ROOT / ".git").exists(),  # a worktree's .git is a FILE, not a dir
        "episodes": len(mine()),
        "cases": {
            "draft": sum(1 for c in cases if c.get("status") == "draft"),
            "approved": sum(1 for c in cases if c.get("status") == "approved"),
            "archived": sum(1 for c in cases if c.get("status") == "archived"),
        },
    }
