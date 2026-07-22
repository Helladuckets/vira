"""Applications module — the job-application front door.

Turns the careers-teardown work (the lab explainers: fit-scored role lists
for Anthropic, OpenAI, and the full frontier boards) into a live backend: a
merged, deduplicated role catalog the owner can star, comment on, track
status against, and — the point — hit Apply on, which dispatches the
`application-package` skill as a live agent session that builds the full
package (tailored CV, cover letter, form answers, interview prep) in the
self-record's 15-applications/.

Design:
- **Roles are read, never owned.** The teardown data.js files stay the source
  of truth for what roles exist and how they score; this module re-parses them
  (mtime-cached) so a re-run of a teardown pipeline shows up on next load.
- **Owner state is keyed by stable uid** (board job id extracted from the
  posting URL), in data/applications.json — stars/comments/status survive
  re-ingests and teardown re-runs.
- **Dedupe prefers the richer record**: a role present in both a company
  teardown (fit-scored) and the frontier full-board corpus keeps the teardown
  record; frontier-only roles carry fit=None and sort below scored ones.
- **Connections**: the LinkedIn export's Connections.csv gives a per-company
  "who could refer me" count surfaced in the UI; the deep referral work
  happens in the skill at package time.
"""
import csv
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import jobshared, jsonstore, settings

STORE = Path(__file__).resolve().parent.parent / "data" / "applications.json"


def self_record() -> Path:
    """The owner's self-record (the CRM's record of its own owner)."""
    override = settings.raw().get("self_record")
    if override:
        return Path(str(override)).expanduser()
    return settings.crm_root() / "self"


def connections_csv() -> Path:
    override = settings.raw().get("applications_connections_csv")
    if override:
        return Path(str(override)).expanduser()
    return (self_record() / "00-current" /
            "linkedin-archive-2026-07-15-complete" / "Connections.csv")


def universe_dir() -> Path:
    """The curated candidate universe — the OWNER-ADJUDICATED starting point
    (v2 repass, 2026-07-14: 181 NYC/remote roles at both labs, 50 deep-read
    and scored/tiered/laned, adversarially verified). This, not the raw
    boards, is the module's default view; the raw corpora remain as the
    'all' view for discovering postings newer than the last repass."""
    override = settings.raw().get("applications_universe")
    if override:
        return Path(str(override)).expanduser()
    return self_record() / "11-future-role" / "analysis"


def sources():
    """Role corpora to ingest. Teardown sources first (fit-scored, curated
    slices), frontier last (full boards, no fit) — order matters: the first
    parse of a uid wins. Configured via `applications_sources` (explicit
    list of {slug, company, path}) or derived from `lab_root` (the local
    checkout holding the teardown explainer directories). Neither set ->
    the module is dormant (empty catalog), per the config philosophy."""
    cfg = settings.raw().get("applications_sources")
    if cfg:
        srcs = [{"slug": s["slug"], "company": s.get("company"),
                 "path": Path(str(s["path"])).expanduser()} for s in cfg]
    else:
        lab = settings.raw().get("lab_root")
        srcs = [] if not lab else [
            {"slug": "anthropic-jobs", "company": "Anthropic",
             "path": Path(str(lab)).expanduser() / "anthropic-jobs" / "data.js"},
            {"slug": "openai-jobs", "company": "OpenAI",
             "path": Path(str(lab)).expanduser() / "openai-jobs" / "data.js"},
            {"slug": "frontier-jobs", "company": None,  # per-job company field
             "path": Path(str(lab)).expanduser() / "frontier-jobs" / "data.js"},
        ]
    # the live boards snapshot (server/jobboards.py) rides along whenever it
    # exists — new-company postings appear here between scoring passes
    from . import jobboards
    snap = jobboards._snapshot_path()
    if snap.exists():
        srcs.append({"slug": "boards", "company": None, "path": snap})
    return srcs

STATUSES = ("none", "applied", "interviewing", "offer", "closed", "skipped")

_lock = threading.Lock()
_cache = {"key": None, "roles": None}
_conn_cache = {"mtime": None, "by_company": None}


def _now():
    return jobshared.now_iso()


# ---------------------------------------------------------------- ingest

def _parse_datajs(path):
    """A teardown data.js is `window.DATA={...json...}` (one assignment).
    A boards `snapshot.json` (server/jobboards.py) parses to the same
    {meta, jobs} shape here — open roles only, closed ones dropped."""
    raw = path.read_text(encoding="utf-8")
    if path.name.endswith(".json"):
        try:
            snap = json.loads(raw)
        except json.JSONDecodeError:
            return None
        jobs = [r for r in (snap.get("roles") or {}).values()
                if not r.get("closed")]
        return {"meta": {"source": f"live boards ({snap.get('fetched', '')})"},
                "jobs": jobs}
    m = re.search(r"window\.DATA\s*=\s*(\{.*)", raw, re.S)
    if not m:
        return None
    txt = m.group(1).rstrip().rstrip(";")
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return None


def role_uid(job):
    """Stable id for owner state. frontier records carry `uid` already
    (a-<greenhouse id> / o-<ashby uuid>); teardown records derive the same
    scheme from the posting URL (jobshared.url_uid) so the two sources
    dedupe against each other."""
    if job.get("uid"):
        return job["uid"]
    url = job.get("url") or job.get("apply") or ""
    uid = jobshared.url_uid(url)
    if uid:
        return uid
    if url:
        return "u-" + re.sub(r"[^a-z0-9]+", "-", url.lower())[-60:].strip("-")
    return "t-" + re.sub(r"[^a-z0-9]+", "-",
                         (job.get("title") or "untitled").lower())[:60]


def _fresh(job):
    """True while a live-boards role is newly listed (first_seen within
    jobboards.FRESH_DAYS) — the NEW badge in the UI. Baseline roles (the
    initial board load) are never fresh."""
    first = job.get("first_seen")
    if not first or job.get("baseline"):
        return False
    try:
        from . import jobboards
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(first)).total_seconds()
        return age < jobboards.FRESH_DAYS * 86400
    except (ValueError, TypeError):
        return False


def _norm(job, source):
    """Normalize a teardown/frontier job record to the module's role shape."""
    company = source["company"] or job.get("company") or "?"
    salary_min = job.get("annualMin", job.get("salaryMin"))
    salary_max = job.get("annualMax", job.get("salaryMax"))
    return {
        "uid": role_uid(job),
        "company": company,
        "title": job.get("title") or "?",
        "team": job.get("team") or job.get("dept") or "",
        "family": job.get("family") or job.get("function") or "",
        "locations": job.get("locations") or [],
        "remote": job.get("remote") or ("remote" if any(
            "remote" in (l or "").lower() for l in job.get("locations") or [])
            else ""),
        "seniority": job.get("seniority") or "",
        "salaryMin": salary_min,
        "salaryMax": salary_max,
        "equity": bool(job.get("equity")),
        "fit": job.get("fit"),
        "bucket": job.get("bucket") or "",
        "reason": job.get("reason") or "",
        "tags": job.get("tags") or [],
        "url": job.get("url") or "",
        "apply_url": job.get("apply") or job.get("url") or "",
        "blurb": (job.get("blurb") or "")[:400],
        "source": source["slug"],
        "comp_kind": job.get("comp") or "",
        "fresh": _fresh(job),
        "cut": job.get("cut") or "",
        "eligible": job.get("eligible", None),
    }


def _sources_key(srcs):
    parts = []
    for s in srcs:
        try:
            parts.append((s["slug"], str(s["path"]), s["path"].stat().st_mtime))
        except OSError:
            parts.append((s["slug"], str(s["path"]), None))
    return tuple(parts)


def load_roles():
    """Merged, deduped role catalog. Cached on source-file mtimes so teardown
    re-runs are picked up without a restart."""
    srcs = sources()
    key = _sources_key(srcs)
    with _lock:
        if _cache["key"] == key and _cache["roles"] is not None:
            return _cache["roles"]
    seen = {}
    meta = {"sources": []}
    for s in srcs:
        data = _parse_datajs(s["path"]) if s["path"].exists() else None
        if not data:
            meta["sources"].append({"slug": s["slug"], "ok": False})
            continue
        jobs = data.get("jobs") or []
        fresh = 0
        for j in jobs:
            r = _norm(j, s)
            if r["uid"] not in seen:
                seen[r["uid"]] = r
                fresh += 1
        meta["sources"].append({
            "slug": s["slug"], "ok": True, "jobs": len(jobs), "new": fresh,
            "source_note": (data.get("meta") or {}).get("source")
                           or (data.get("meta") or {}).get("captured") or "",
        })
    roles = list(seen.values())
    with _lock:
        _cache["key"] = key
        _cache["roles"] = (roles, meta)
    return roles, meta


# ------------------------------------------------------------ the universe

TIER_RANK = {"1": 0, "2": 1, "3": 2, "pass": 3, "": 4}

_universe_cache = {"key": None, "roles": None}


def _load_adjudication(udir):
    """The owner's standing ruling (owner-adjudication.json next to the
    universe): a pinned shortlist plus cut rules (no sales / no commission /
    no marketing, 2026-07-14). Cut is by comp structure and TITLE — never by
    the board's function label, which files base-comp deployment roles under
    'Sales & Go-To-Market'. Absent file -> no adjudication applied."""
    try:
        a = json.loads((udir / "owner-adjudication.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(a, dict):
        return None
    cut = a.get("cut") or {}
    return {
        "shortlist": {r["uid"]: i for i, r in
                      enumerate(a.get("shortlist") or []) if r.get("uid")},
        "cut_comp": set(cut.get("comp") or []),
        "cut_titles": [re.compile(p, re.I)
                       for p in cut.get("title_patterns") or []],
        "reason_comp": cut.get("reason_comp") or "cut on the owner's call",
        "reason_title": cut.get("reason_title") or "cut on the owner's call",
    }


def _apply_adjudication(role, adj):
    if not adj:
        return
    idx = adj["shortlist"].get(role["uid"])
    if idx is not None:
        role["shortlist"] = idx + 1          # 1-based page order
        return                                # a pick can never be cut
    reason = jobshared.cut_reason(role.get("comp_kind"),
                                  role.get("title"), adj)
    if reason:
        role["cut"] = reason


def _universe_key(udir):
    parts = []
    for p in (udir / "candidate-universe" / "manifest.json",
              udir / "owner-adjudication.json",
              udir / "candidate-universe" / "role",
              *sorted(udir.glob("*-raw-scores.json"))):
        try:
            parts.append((str(p), p.stat().st_mtime))
        except OSError:
            parts.append((str(p), None))
    # corpus mtimes too: the universe joins apply URLs from the corpora
    return tuple(parts) + _sources_key(sources())


def load_universe():
    """The curated universe: role/<uid>.json files overlaid with the repass
    scores (fit, tier, lane, why_fit, caveat, lead_with, comp_note). Scored
    roles carry `tier`; the rest were triaged out in the 8-agent pass and
    keep their v1 auto-score as `fit_old` only. Apply URLs are joined from
    the raw corpora when present (role files carry the posting url)."""
    udir = universe_dir()
    key = _universe_key(udir)
    with _lock:
        if _universe_cache["key"] == key and _universe_cache["roles"] is not None:
            return _universe_cache["roles"]
    role_dir = udir / "candidate-universe" / "role"
    scores = jobshared.load_scores(udir)
    adj = _load_adjudication(udir)
    corpus = {r["uid"]: r for r in load_roles()[0]}
    out = []
    if role_dir.is_dir():
        for f in sorted(role_dir.glob("*.json")):
            try:
                j = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            uid = j.get("uid") or f.stem
            sc = scores.get(uid, {})
            cr = corpus.get(uid, {})
            tier = str(sc.get("final_tier") or sc.get("tier") or "") \
                if sc else ""
            out.append({
                "uid": uid,
                "company": j.get("company") or "?",
                "title": j.get("title") or "?",
                "team": j.get("team") or j.get("dept") or "",
                "family": j.get("function") or "",
                "locations": j.get("locations") or [],
                "remote": j.get("remote") or "",
                "seniority": j.get("seniority") or "",
                "salaryMin": j.get("salaryMin"),
                "salaryMax": j.get("salaryMax"),
                "equity": bool(cr.get("equity")),
                "comp_kind": j.get("comp") or "",       # base / ote / hourly
                "fit": sc.get("fit") if sc else None,   # v2 repass score
                "fit_old": j.get("fit_old"),            # v1 auto-score
                "tier": tier,
                "lane": sc.get("lane") or "",
                "why_fit": sc.get("why_fit") or "",
                "caveat": sc.get("caveat") or "",
                "lead_with": sc.get("lead_with") or "",
                "comp_note": sc.get("comp_note") or "",
                "verdict": sc.get("verdict") or "",
                "served": j.get("served") or "",
                "bucket": "",
                "reason": "",
                "tags": j.get("tags") or [],
                "url": j.get("url") or cr.get("url") or "",
                "apply_url": cr.get("apply_url") or j.get("url") or "",
                "blurb": (j.get("blurb") or "")[:400],
                "source": "universe",
                "in_universe": True,
                "fresh": _fresh(j),
                "shortlist": 0,               # page-order rank when picked
                "cut": "",                    # reason text when cut
            })
            _apply_adjudication(out[-1], adj)
    # picks first in page order, then tiers, cut lane last (still visible)
    out.sort(key=lambda r: (
        (0, r["shortlist"], 0) if r["shortlist"] else
        (2, TIER_RANK.get(r["tier"], 4), -(r["fit"] or 0)) if r["cut"] else
        (1, TIER_RANK.get(r["tier"], 4), -(r["fit"] or 0)),
        -(r["fit_old"] or 0), r["company"], r["title"]))
    with _lock:
        _universe_cache["key"] = key
        _universe_cache["roles"] = out
    return out


# ------------------------------------------------------------ owner state

def _load_state():
    try:
        s = json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        s = {}
    if not isinstance(s, dict):
        s = {}
    s.setdefault("roles", {})
    return s


def _save_state(s):
    jsonstore.write_atomic(STORE, s, indent=1, ensure_ascii=False)


def get_state():
    with _lock:
        return _load_state()["roles"]


def update_state(uid, starred=None, status=None, comment=None,
                 job_id=None):
    """Merge one role's owner state. `comment` appends; the rest set."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    with _lock:
        s = _load_state()
        row = s["roles"].setdefault(uid, {})
        if starred is not None:
            row["starred"] = bool(starred)
        if status is not None:
            row["status"] = status
        if comment:
            row.setdefault("comments", []).append(
                {"text": str(comment)[:2000], "when": _now()})
        if job_id is not None:
            row["last_job"] = job_id
            row["applied_when"] = _now()
        row["updated"] = _now()
        _save_state(s)
        return row


# ------------------------------------------------------------ connections

def connections_by_company():
    """Company -> [{name, position}] from the LinkedIn export. The CSV opens
    with a notes preamble; the real header is `First Name,...`."""
    path = connections_csv()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    with _lock:
        if _conn_cache["mtime"] == mtime and _conn_cache["by_company"]:
            return _conn_cache["by_company"]
    rows = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
        start = next((i for i, ln in enumerate(lines)
                      if ln.startswith("First Name,")), None)
        if start is None:
            return {}
        rows = list(csv.DictReader(lines[start:]))
    except (OSError, csv.Error):
        return {}
    by = {}
    for r in rows:
        comp = (r.get("Company") or "").strip()
        if not comp:
            continue
        name = " ".join(x for x in
                        [(r.get("First Name") or "").strip(),
                         (r.get("Last Name") or "").strip()] if x)
        by.setdefault(comp.lower(), []).append(
            {"name": name, "company": comp,
             "position": (r.get("Position") or "").strip()})
    with _lock:
        _conn_cache["mtime"] = mtime
        _conn_cache["by_company"] = by
    return by


def connections_for(company):
    """Loose match: export company field contains the employer name."""
    needle = (company or "").strip().lower()
    if not needle:
        return []
    out = []
    for comp, people in connections_by_company().items():
        if needle in comp:
            out.extend(people)
    return out


# ------------------------------------------------------------- the payload

def compose(company=None, view="universe"):
    """The /api/applications payload: roles + owner state merged.

    view="universe" (default): the curated, owner-adjudicated candidate set
    (tier-then-fit order). view="all": the universe first, then every corpus
    role not in it (marked in_universe=False) — the discovery view for
    postings newer than the last repass."""
    uni = load_universe()
    _roles, meta = load_roles()
    if view == "all":
        in_uni = {r["uid"] for r in uni}
        rest = [dict(r, in_universe=False) for r in _roles
                if r["uid"] not in in_uni]
        rest.sort(key=lambda r: (r["fit"] is None,
                                 -(r["fit"] or 0), r["company"], r["title"]))
        roles = uni + rest
    else:
        roles = uni
    meta = dict(meta)
    meta["universe"] = {
        "total": len(uni),
        "scored": sum(1 for r in uni if r["fit"] is not None),
        "tier1": sum(1 for r in uni if r["tier"] == "1" and not r["cut"]),
        "shortlist": sum(1 for r in uni if r["shortlist"]),
        "cut": sum(1 for r in uni if r["cut"]),
        "dir": str(universe_dir()),
    }
    state = get_state()
    companies = {}
    out = []
    for r in roles:
        if company and r["company"].lower() != company.lower():
            continue
        row = dict(r)
        st = state.get(r["uid"], {})
        row["starred"] = bool(st.get("starred"))
        row["status"] = st.get("status", "none")
        row["comments"] = st.get("comments", [])
        row["last_job"] = st.get("last_job")
        out.append(row)
        c = companies.setdefault(r["company"], {"roles": 0, "scored": 0})
        c["roles"] += 1
        if r["fit"] is not None:
            c["scored"] += 1
    for name, c in companies.items():
        c["connections"] = len(connections_for(name))
    return {"roles": out, "companies": companies, "meta": meta}


# ------------------------------------------------------------- apply prompt

SKILL_MD = Path.home() / ".claude" / "skills" / "application-package" / "SKILL.md"


def find_role(uid):
    """Universe record first (carries the dossier fields), corpus fallback."""
    for r in load_universe():
        if r["uid"] == uid:
            return r
    for r in load_roles()[0]:
        if r["uid"] == uid:
            return r
    return None


def apply_prompt(role, note=""):
    """The prompt an Apply dispatch hands the agent session. cwd is the
    self-record so its CLAUDE.md (claim gate, confidentiality) auto-loads.
    Universe roles ride with their adjudicated dossier read (tier, lane,
    why_fit, lead_with, caveat) — the package build starts from that, not
    from scratch."""
    lines = [
        "Run the application-package skill "
        f"(read {SKILL_MD} and follow it end to end) for this role. "
        "Build the FULL package. Draft only — never submit anything.",
        "",
        "ROLE:",
        json.dumps({k: role.get(k) for k in
                    ("uid", "company", "title", "team", "family", "locations",
                     "seniority", "salaryMin", "salaryMax", "fit", "bucket",
                     "reason", "tags", "url", "apply_url")},
                   indent=1, ensure_ascii=False),
    ]
    dossier = {k: role.get(k) for k in
               ("tier", "lane", "why_fit", "lead_with", "caveat",
                "comp_note", "comp_kind", "verdict", "shortlist", "cut")
               if role.get(k)}
    if dossier:
        lines += ["", "DOSSIER READ (v2 repass + owner adjudication — the "
                  "adjudicated starting point; honor lead_with and state "
                  "the caveat honestly):",
                  json.dumps(dossier, indent=1, ensure_ascii=False)]
    if role.get("cut"):
        lines += ["", "WARNING: this role sits in a lane the owner CUT "
                  f"({role['cut']}). The owner dispatched it anyway — note "
                  "the tension plainly in the fit brief before building."]
    if note:
        lines += ["", f"Owner note with this dispatch: {note}"]
    lines += ["", "Close out exactly per the skill: tracker row `ready`, "
              "best-effort status mirror, open the package folder. Only the "
              "owner submits — never mark anything `applied`."]
    return "\n".join(lines)
