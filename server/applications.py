"""Applications module — the job-application catalog.

A merged, deduplicated role catalog the owner can star, comment on, track
status against, and — the point — write an application for, which
dispatches the `application-package` skill as a live agent session that
builds the full package (tailored CV, cover letter, form answers,
interview prep).  Whether a role HAS a package is read off disk rather
than off that dispatch (`applicationmap.written_for`), so the catalog
reports what exists rather than what was asked for.

Roles arrive from two kinds of source: the live board poller
(server/jobboards.py, generic — a registry of company boards per ATS) and
any static corpora the owner points `applications_sources` at. Setup
(server/frontdoor.py) wires the first for a new install; the second is
how an existing analysis pipeline plugs in.

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

from . import jobcompare, jobshared, jsonstore, settings, workplace

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
    return (self_record() / "evidence" / "linkedin" /
            "linkedin-archive-2026-07-15-complete" / "Connections.csv")


def universe_dir() -> Path:
    """The curated candidate universe — the adjudicated starting point that
    the module shows by default; the raw corpora remain as the 'all' view
    for discovering postings newer than the last repass.

    The path is configuration, not convention. It used to default to one
    instance's filing scheme (`11-future-role/analysis`), which meant a
    stranger's install pointed at a directory that only ever existed on
    one Mac. Setup writes `applications_universe` when it builds the
    record; the neutral fallback below is what a hand-configured install
    gets."""
    override = settings.raw().get("applications_universe")
    if override:
        return Path(str(override)).expanduser()
    return self_record() / "analysis"


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
MAP_NOTE_LANES = ("resume", "cover", "narrative")

_lock = threading.Lock()
_cache = {"key": None, "roles": None}
_conn_cache = {"mtime": None, "by_company": None}


def _now():
    return jobshared.now_iso()


# ---------------------------------------------------------------- ingest

def _parse_datajs(path):
    """A teardown data.js is `window.DATA={...json...}` (one assignment).
    A boards `snapshot.json` (server/jobboards.py) parses to the same
    {meta, jobs} shape here.

    Closed roles are KEPT. Dropping them was the opposite of what the
    owner wants from a posting that comes down: the analysis stacked on
    top of it is the expensive part, so a dead role is marked and
    filterable, never deleted out from under its own notes."""
    raw = path.read_text(encoding="utf-8")
    if path.name.endswith(".json"):
        try:
            snap = json.loads(raw)
        except json.JSONDecodeError:
            return None
        jobs = list((snap.get("roles") or {}).values())
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


# ------------------------------------------------------------ place facets

# Canonical city names for the location filter. Location strings are
# free text typed by hundreds of recruiters ("New York, NY" / "New York
# City" / "NYC" / "New York, NY, US"), so the facet is the FIRST comma
# segment of each pipe-separated part, folded through this alias table.
# Merging is case-insensitive; the display name is the canonical spelling
# where one exists, else the first spelling seen.
PLACE_ALIASES = {
    "nyc": "New York", "new york city": "New York",
    "new york metro": "New York",
    "sf": "San Francisco", "sf bay area": "San Francisco",
    "bay area": "San Francisco",
    "washington": "Washington DC", "washington dc": "Washington DC",
    "washington d c": "Washington DC",
}


def _facet(part):
    """One location fragment -> its canonical place name."""
    seg = str(part).split(",")[0].strip()
    key = re.sub(r"[^a-z ]", "", seg.lower())
    key = re.sub(r"\s+", " ", key).strip()
    return PLACE_ALIASES.get(key) or seg


def places_for(locs, wp=None):
    """Location strings -> canonical place facets, deduped, order kept.
    Any part carrying the word 'remote' facets as Remote; the rest facet
    by their leading city segment.

    `wp` is the body's own workplace reading (server.workplace). Where
    it BINDS -- the description names the offices and rules remote out
    -- it decides the facets instead: the Remote facet is dropped and
    the offices the body names are folded in. That is what stops a
    posting tagged "US - Remote" whose body says "based in San
    Francisco, CA, hybrid 3 days a week" from answering the Remote
    filter, and what makes it answer the San Francisco one, which is
    where the owner would actually look for it. The raw `locations`
    strings are never touched -- the row shows both, so the
    disagreement stays visible rather than being quietly resolved.
    """
    from . import jobboards
    bind = bool(wp and wp.get("binds"))
    out, seen = [], set()

    def add(name):
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)

    if bind:
        for p in wp.get("places") or []:
            add(_facet(p))
    for loc in locs or []:
        for part in re.split(r"[|;]", str(loc)):
            part = part.strip()
            if not part:
                continue
            if jobboards.REMOTE_RE.search(part):
                if bind:
                    continue      # the body says this is not remote work
                add("Remote")
            else:
                add(_facet(part))
    return out


def _availability(uid, avail, own_closed=""):
    """One role's availability, from the boards state (jobboards owns the
    verdict). Three values, and the third is the point: `unverified` means
    no registered board has ever seen this posting, so the module says so
    instead of showing a role from a frozen corpus as though it were
    checked this morning."""
    a = avail.get(uid)
    if a:
        return a["state"], a.get("since") or a.get("checked") or ""
    if own_closed:                    # a snapshot record closed pre-migration
        return "gone", own_closed
    return "unverified", ""


def _norm(job, source, avail=None, rule=None):
    """Normalize a teardown/frontier job record to the module's role shape.
    `rule` (jobboards.location_rule) lets eligibility be computed for
    corpus roles that never went through a board sweep — a stamped
    `eligible` (the snapshot's) still wins over the computed one, except
    that the body's own workplace reading can veto it (see below)."""
    company = source["company"] or job.get("company") or "?"
    salary_min = job.get("annualMin", job.get("salaryMin"))
    salary_max = job.get("annualMax", job.get("salaryMax"))
    uid = role_uid(job)
    state, when = _availability(uid, avail or {}, job.get("closed") or "")
    locs = job.get("locations") or []
    # The body's reading, stamped by a board sweep where one has run and
    # derived here otherwise, so a corpus role frozen before this
    # existed is read the same way as one swept this morning.
    wp = job.get("workplace")
    if wp is None:
        wp = workplace.read(job.get("jd") or "")
    if "eligible" in job and job.get("eligible") is not None:
        eligible = bool(job["eligible"])
    elif rule is not None:
        from . import jobboards
        eligible = jobboards.eligible_location(
            {"locations": locs, "workplace": wp}, rule)
    else:
        eligible = None
    # A stamp written before the body was ever read can say True where
    # the description names one office in another city. The reading only
    # ever NARROWS -- it can veto a stamp, never manufacture eligibility
    # a stamp withheld -- so the sweep stays the authority on everything
    # except the one thing it could not see.
    if eligible and rule is not None \
            and not workplace.allows(wp, rule["places"], locs):
        eligible = False
    return {
        "uid": uid,
        "company": company,
        "title": job.get("title") or "?",
        "team": job.get("team") or job.get("dept") or "",
        "family": job.get("family") or job.get("function") or "",
        "locations": locs,
        "places": places_for(locs, wp),
        "workplace": wp,
        "workplace_label": workplace.label(wp),
        "remote": "" if (wp and wp.get("binds")) else (
            job.get("remote") or ("remote" if any(
                "remote" in (l or "").lower() for l in locs) else "")),
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
        # Kept on the server-side role record for comparison and application
        # package work. compose() deliberately removes it from the catalog
        # payload: thousands of full postings would make first paint enormous.
        "jd": str(job.get("jd") or "")[:24000],
        "source": source["slug"],
        "comp_kind": job.get("comp") or "",
        "fresh": _fresh(job),
        "first_seen": job.get("first_seen") or "",
        "baseline": bool(job.get("baseline")),
        "cut": job.get("cut") or "",
        "eligible": eligible,
        "availability": state,
        "availability_when": when,
    }


def _sources_key(srcs):
    parts = []
    for s in srcs:
        try:
            parts.append((s["slug"], str(s["path"]), s["path"].stat().st_mtime))
        except OSError:
            parts.append((s["slug"], str(s["path"]), None))
    # the boards state decides availability, so a sweep that closes a role
    # has to invalidate both caches — otherwise the catalog keeps serving
    # it as live until some unrelated file happens to change
    from . import jobboards
    return tuple(parts) + (("boards-state", jobboards.state_mtime()),)


def load_roles():
    """Merged, deduped role catalog. Cached on source-file mtimes so teardown
    re-runs are picked up without a restart."""
    srcs = sources()
    key = _sources_key(srcs)
    with _lock:
        if _cache["key"] == key and _cache["roles"] is not None:
            return _cache["roles"]
    from . import jobboards
    avail = jobboards.availability_map()
    rule = jobboards.location_rule()
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
            r = _norm(j, s, avail, rule)
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
    universe): the cut rules (no sales / no commission / no marketing,
    2026-07-14). Cut is by comp structure and TITLE — never by the board's
    function label, which files base-comp deployment roles under 'Sales &
    Go-To-Market'. Absent file -> no adjudication applied.

    THE PINNED SHORTLIST IS DELIBERATELY NOT READ (owner's call,
    2026-08-13: "the idea of picks is stale"). It was a July snapshot of
    eight roles and three of the seven survivors were dead postings by
    August; a rank frozen against a catalog that has since tripled is a
    claim the module cannot keep true. The list is still in the file,
    untouched, so restoring picks is a code change and not a data loss.
    """
    try:
        a = json.loads((udir / "owner-adjudication.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(a, dict):
        return None
    cut = a.get("cut") or {}
    return {
        "cut_comp": set(cut.get("comp") or []),
        "cut_titles": [re.compile(p, re.I)
                       for p in cut.get("title_patterns") or []],
        "reason_comp": cut.get("reason_comp") or "cut on the owner's call",
        "reason_title": cut.get("reason_title") or "cut on the owner's call",
    }


def _apply_adjudication(role, adj):
    if not adj:
        return
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
    # The per-role score store is where a rescore lands, and a directory's
    # own mtime does not move when a file inside it is rewritten in place —
    # so the key folds in the newest mtime UNDER it. Without this a rescore
    # writes correctly and the module keeps serving the old why_fit until
    # some unrelated file happens to change (the jobboards.state_mtime seam).
    from . import jobscores
    parts.append(("scores", jobscores.dir_mtime(udir)))
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
    from . import jobscores
    # Computed ONCE per catalog load: canon_at stats two files, and asking
    # it per role would stat them a thousand times to get one answer.
    canon = jobscores.canon_at(udir)
    corpus = {r["uid"]: r for r in load_roles()[0]}
    from . import jobboards
    avail = jobboards.availability_map()
    rule = jobboards.location_rule()
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
            av_state, av_when = _availability(uid, avail)
            locs = j.get("locations") or []
            wp = j.get("workplace")
            if wp is None:
                wp = workplace.read(j.get("jd") or cr.get("jd") or "")
            out.append({
                "uid": uid,
                "company": j.get("company") or "?",
                "title": j.get("title") or "?",
                "team": j.get("team") or j.get("dept") or "",
                "family": j.get("function") or "",
                "locations": locs,
                "places": places_for(locs, wp),
                "workplace": wp,
                "workplace_label": workplace.label(wp),
                "eligible": jobboards.eligible_location(
                    {"locations": locs, "workplace": wp}, rule),
                "first_seen": j.get("first_seen")
                              or cr.get("first_seen") or "",
                "baseline": bool(cr.get("baseline")),
                "remote": "" if (wp and wp.get("binds")) \
                    else (j.get("remote") or ""),
                "seniority": j.get("seniority") or "",
                "salaryMin": j.get("salaryMin"),
                "salaryMax": j.get("salaryMax"),
                "equity": bool(cr.get("equity")),
                "comp_kind": j.get("comp") or "",       # base / ote / hourly
                "fit": sc.get("fit") if sc else None,   # v2 repass score
                # The OTHER half of the two-score discipline the scoring
                # prompt has always mandated: narrative resonance (fit) and
                # screening probability (screen), separately. 1,204 entries
                # carried it and nothing read it until 2026-08-12.
                "screen": sc.get("screen") if sc else None,
                "scored_at": (sc.get("scored_at") or "") if sc else "",
                "score_stale": bool(sc) and jobscores.is_stale(sc, canon),
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
                "jd": str(j.get("jd") or cr.get("jd") or "")[:24000],
                "source": "universe",
                "in_universe": True,
                # role files carry no first_seen; the corpus overlay does
                "fresh": _fresh(j) or bool(cr.get("fresh")),
                "cut": "",                    # reason text when cut
                "availability": av_state,
                "availability_when": av_when,
            })
            _apply_adjudication(out[-1], adj)
    # tiers first, cut lane last (demoted, never hidden)
    out.sort(key=lambda r: (
        1 if r["cut"] else 0, TIER_RANK.get(r["tier"], 4), -(r["fit"] or 0),
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


def _map_notes_from_row(row):
    raw = row.get("evidence_map", {}).get("notes", {})
    if not isinstance(raw, dict):
        return []
    out = []
    for concept_key, lanes in raw.items():
        if not isinstance(lanes, dict):
            continue
        for lane, note in lanes.items():
            if lane not in MAP_NOTE_LANES or not isinstance(note, dict):
                continue
            text = str(note.get("text") or "").strip()
            if text:
                out.append({"concept_key": concept_key, "lane": lane,
                            "text": text, "when": note.get("when") or ""})
    return out


def map_notes(uid):
    """Application-specific drafting notes keyed to a stable requirement.

    These live with the role's other owner state.  They are planning
    annotations, never additions to the self-record or proof of a claim.
    """
    with _lock:
        row = _load_state()["roles"].get(uid, {})
        return _map_notes_from_row(row)


def update_map_note(uid, concept_key, lane, text):
    """Upsert one lane note; empty text removes it."""
    concept_key = str(concept_key or "").strip()
    if not re.fullmatch(r"[a-f0-9]{16}", concept_key):
        raise ValueError("invalid requirement key")
    if lane not in MAP_NOTE_LANES:
        raise ValueError(f"lane must be one of {MAP_NOTE_LANES}")
    text = str(text or "").strip()
    if len(text) > 4000:
        raise ValueError("planning note must be 4000 characters or fewer")
    with _lock:
        s = _load_state()
        row = s["roles"].setdefault(uid, {})
        evidence_map = row.setdefault("evidence_map", {})
        notes = evidence_map.setdefault("notes", {})
        lanes = notes.setdefault(concept_key, {})
        if text:
            lanes[lane] = {"text": text, "when": _now()}
        else:
            lanes.pop(lane, None)
            if not lanes:
                notes.pop(concept_key, None)
        row["updated"] = _now()
        _save_state(s)
        return _map_notes_from_row(row)


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

def compose(company=None, view=None):
    """The /api/applications payload: roles + owner state merged.

    ONE LIST. There used to be a Universe / All-boards mode toggle, and
    the owner's verdict on it was that he neither understood nor wanted
    the distinction — fair, because it was a fact about this module's
    plumbing (curated-and-scored vs raw-posting) dressed up as a place to
    navigate to. The analyzed roles still lead the order and "analyzed vs
    not yet analyzed" survives as a filter line, which is what that split
    was actually good for. `view` is accepted and ignored so an old
    bookmark or a stale client cannot 404."""
    uni = load_universe()
    _roles, meta = load_roles()
    in_uni = {r["uid"] for r in uni}
    rest = [dict(r, in_universe=False) for r in _roles
            if r["uid"] not in in_uni]
    rest.sort(key=lambda r: (r["fit"] is None,
                             -(r["fit"] or 0), r["company"], r["title"]))
    roles = uni + rest
    meta = dict(meta)
    meta["universe"] = {
        "total": len(uni),
        "scored": sum(1 for r in uni if r["fit"] is not None),
        "tier1": sum(1 for r in uni if r["tier"] == "1" and not r["cut"]),
        "cut": sum(1 for r in uni if r["cut"]),
        "unanalyzed": len(rest),
        "dir": str(universe_dir()),
    }
    meta["availability"] = {
        "gone": sum(1 for r in roles if r["availability"] == "gone"),
        "unverified": sum(1 for r in roles
                          if r["availability"] == "unverified"),
    }
    facets = {}
    elig = {"eligible": 0, "outside": 0}
    for r in roles:
        for p in r.get("places") or []:
            facets[p] = facets.get(p, 0) + 1
        if r.get("eligible") is True:
            elig["eligible"] += 1
        elif r.get("eligible") is False:
            elig["outside"] += 1
    meta["locations"] = [
        {"name": k, "count": v}
        for k, v in sorted(facets.items(), key=lambda kv: (-kv[1], kv[0]))]
    meta["eligibility"] = elig
    cfg = settings.raw()
    meta["location_rule"] = {
        "places": list(cfg.get("applications_locations") or []),
        "remote_ok": cfg.get("applications_remote_ok", True) is not False,
        # unconfigured means unfiltered (jobboards.eligible_location) —
        # the dropdown only offers the rule rows when there is a rule
        "configured": bool(cfg.get("applications_locations"))
                      or "applications_remote_ok" in cfg,
    }
    state = get_state()
    try:
        from . import research
        research_rows = research.catalog()
    except Exception:  # noqa: BLE001 -- applications must work without graphs
        research_rows = []

    def graph_for(company_name):
        wanted = str(company_name or "").strip().casefold()
        if not wanted:
            return None
        exact = next((g for g in research_rows if wanted in {
            str(g.get("id") or "").casefold(),
            str(g.get("name") or "").casefold(),
            str(g.get("company") or "").casefold(),
        }), None)
        return exact or next((g for g in research_rows if wanted in
                              str(g.get("company") or "").casefold()), None)

    # Which roles have an application package on disk. Derived, not stored,
    # and deliberately NOT read off `last_job` — see applicationmap.written_for
    # for why a dispatch stamp is a different fact from a written package.
    from . import applicationmap
    written = applicationmap.written_for(roles)

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
        row["written"] = r["uid"] in written
        row["package"] = written.get(r["uid"])
        # The list only needs the capability flag. Full descriptions stay
        # server-side and travel only for the roles the owner compares.
        row["has_description"] = jobcompare.description_available(
            row.get("jd") or "")
        row.pop("jd", None)
        graph = graph_for(r["company"])
        if graph and graph.get("status") == "ready":
            row["research"] = {
                "slug": graph["id"],
                "name": graph["name"],
                "claim_count": graph.get("claim_count", 0),
            }
        out.append(row)
        c = companies.setdefault(r["company"], {"roles": 0, "scored": 0})
        c["roles"] += 1
        if r["fit"] is not None:
            c["scored"] += 1
    for name, c in companies.items():
        c["connections"] = len(connections_for(name))
        graph = graph_for(name)
        if graph and graph.get("status") == "ready":
            c["research"] = {
                "slug": graph["id"],
                "name": graph["name"],
                "claim_count": graph.get("claim_count", 0),
            }
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


def compare_roles(uids):
    """Resolve catalog ids and return a deterministic description comparison."""
    if not isinstance(uids, list):
        raise ValueError("uids must be a list")
    if not jobcompare.MIN_ROLES <= len(uids) <= jobcompare.MAX_ROLES:
        raise ValueError(
            f"choose between {jobcompare.MIN_ROLES} and "
            f"{jobcompare.MAX_ROLES} roles")
    roles = []
    for uid in uids:
        role = find_role(str(uid))
        if role is None:
            raise KeyError(str(uid))
        roles.append(role)
    return jobcompare.compare(roles)


def _slug_part(text, cap=None):
    s = re.sub(r"-{2,}", "-",
               re.sub(r"[^a-z0-9]+", "-", str(text or "").lower())).strip("-")
    return s[:cap].strip("-") if cap else s


def session_slug(role, when=None):
    """`<company>-<title>-<YYYY-MM-DD>` — the label that leads every Apply
    prompt.

    Every Apply dispatch used to open on the same sentence ("Run the
    application-package skill..."), and joblog derives a session's name from
    the prompt's first substantive line — so a dozen concurrent Apply runs
    were a dozen identically-named rows in Live and a dozen identical
    terminal title bars. This slug is what tells them apart.

    It is budgeted against joblog.TITLE_CAP by trimming the TITLE on a
    hyphen boundary, so COMPANY and DATE always survive whole. That is not
    cosmetic: `_short` cuts from the end, and 826 of the 3,197 catalog roles
    exceed the cap, so the naive slug would have the date — the half that
    separates one attempt at a role from the next — eaten off exactly the
    quarter of roles with the longest titles."""
    from . import joblog  # lazy: naming is joblog's, the label is ours
    day = (when or datetime.now()).strftime("%Y-%m-%d")
    company = _slug_part(role.get("company"), 32) or "role"
    budget = joblog.TITLE_CAP - len(company) - len(day) - 2
    title = _slug_part(role.get("title"))
    if len(title) > budget:
        title = title[:max(budget, 0)].rsplit("-", 1)[0].strip("-")
    return "-".join(p for p in (company, title, day) if p)


def apply_prompt(role, note=""):
    """The prompt an Apply dispatch hands the agent session. cwd is the
    self-record; on Claude its CLAUDE.md auto-loads, but the load-bearing
    source ladder, claim gate, and confidentiality rules are EMBEDDED in the
    prompt text so they survive every backend. Universe roles ride with their
    adjudicated dossier read (tier, lane, why_fit, lead_with, caveat) — the
    package build starts from that, then selects evidence from the whole career
    rather than the smallest already-rendered subset."""
    record = self_record()
    history = record / "canon" / "MASTER_HISTORY.md"
    voice = record / "canon" / "VOICE.md"
    freshness = record / "renderings" / "check_freshness.py"
    inventory = record / "INVENTORY.md"
    lines = [
        # Leads the prompt because joblog names the run from its first
        # line — see session_slug. Company and title are also stated for
        # real in the ROLE block below; this line is the label.
        session_slug(role),
        "",
        "Run the application-package skill "
        f"(read {SKILL_MD} and follow it end to end) for this role. "
        "Build the FULL package. Draft only — never submit anything.",
        "",
        "GROUND RULES — these bind regardless of what else your harness "
        "loaded:",
        f"- Read {history} FRESH before selecting evidence. It is the "
        "single canonical career record: the body is the comprehensive "
        "human-readable account and provenance index, and ITS ENDNOTES ARE "
        "THE CLAIM GATE. Every approved outward wording, citability rule, "
        "INTERNAL boundary, and required qualifier sits in the endnote on "
        "the sentence it governs. Read the governing endnote before using "
        "any claim; it wins every conflict. self.json is a distillation that "
        "follows it; an existing resume, bio, deck, fit brief, or cached "
        "summary is never a source. "
        f"Use {inventory} to find recent source additions. Run `python3 "
        f"{freshness}` at intake and reconcile any named stale source before "
        "drafting.",
        f"- Read {voice} for how the writing must SOUND and what SHAPE the "
        "rendering takes. The claim gate still wins on any claim.",
        "- A useful claim with no supporting endnote triggers evidence "
        "review and active adjudication, not silent deletion. Follow the "
        "nearest Master History endnote to the source. If the evidence "
        "supports an unambiguous sanitized form, add the ruling to that "
        "endnote before use; otherwise keep it out of outward artifacts and "
        "record it as NEEDS ADJUDICATION in the package evidence map.",
        f"- {record / 'evidence' / 'sentinel'} is admissible PRIVATE evidence for "
        "reconstructing role, craft, authorship, sequence, tools, and scope. "
        "Use it internally to adjudicate sanitized language; never expose the "
        "private dossier, proprietary fund/tenant/vendor/strategy/rendering "
        "detail, confidential employment mechanics, or non-public deal "
        "figures. Nothing from "
        f"{record / 'evidence' / 'personal'} becomes a career claim; the SSN quarantine "
        "is absolute.",
        "- Before drafting, create an internal evidence map covering current "
        "systems, VP Operations, VP Acquisitions, AVP, Analyst, and pre-"
        "Sentinel foundations. CONSIDERED, NOT SELECTED is valid; silence is "
        "not. Preserve distinct roles and never make the four personally led "
        "VP deals stand in for the entire career.",
        f"- The full rulebook is {record / 'CLAUDE.md'} — read it first "
        "if your harness did not load it for you.",
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
                "comp_note", "comp_kind", "verdict", "cut")
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
    # Feedback the owner left while READING the last draft (the resume
    # viewport's "this application" lane, and the role's Notes pane). Without
    # this the notes were stored and never read back, so the next version
    # bump repeated whatever he had already objected to. They are reactions
    # to a draft, not evidence — the claim gate still governs every sentence.
    # Read the owner state DIRECTLY: find_role returns the catalog record and
    # never merges it, so role.get("comments") is always empty here — only
    # compose() attaches owner state, and that path serves the UI, not this.
    owner = get_state().get(role.get("uid") or "", {})
    comments = [c.get("text", "") for c in (owner.get("comments") or [])
                if c.get("text")]
    if comments:
        lines += ["", "OWNER FEEDBACK ON EARLIER DRAFTS (newest last — these "
                  "are drafting instructions about THIS application, never "
                  "evidence for a claim):"]
        lines += [f"- {c}" for c in comments[-12:]]
    # This is the same stable requirement keyspace and source-matching frame
    # the interactive Map renders. Map notes therefore become build inputs,
    # instead of annotations the package agent can never see.
    from . import applicationmap
    plan = applicationmap.prompt_plan(role)
    lines += [
        "",
        "APPLICATION EVIDENCE PLAN (generated by the same framework as "
        "Vira's interactive Map):",
        "- Account for EVERY requirement key below before drafting. Candidate "
        "anchors are places to inspect, not claims that the requirement is met.",
        "- Classify each line as DIRECT, TRANSFERABLE ANALOGUE, NEEDS "
        "ADJUDICATION, or GAP. A transferable analogue must name the shared "
        "mechanism and the changed context; never imply the owner performed "
        "the employer's exact work when he did not.",
        "- Creativity means finding a truthful bridge across contexts. It "
        "never means stretching weak language overlap into an absurd match, "
        "inventing scope, or concealing a gap.",
        "- Owner notes came from right-click edits in the Map and are drafting "
        "instructions. They are not evidence.",
        "- Write these same stable keys into evidence-map.md. For every key, "
        "record the final classification, exact Master History endnote "
        "anchor or open adjudication, selected resume treatment, "
        "cover-letter angle, and "
        "interview narrative/talking point. Use GAP where no truthful treatment "
        "exists; do not force every requirement into every artifact.",
        json.dumps(plan, indent=1, ensure_ascii=False),
    ]
    try:
        from . import research
        context = research.application_context(role=role, claim_limit=8)
    except Exception:  # noqa: BLE001 -- package dispatch is still usable
        context = None
    if context:
        bridge = context.get("personal_bridge") or {}
        taxonomy = bridge.get("taxonomy") or {}
        bridge_items = []
        for item in (taxonomy.get("items") or [])[:14]:
            overlap = item.get("matt_overlap") or {}
            bridge_items.append({
                "rank": item.get("rank"),
                "id": item.get("id"),
                "label": item.get("label"),
                "application_use": item.get("application_use"),
                "facts_anchors": overlap.get("facts_anchors") or [],
                "permitted_language": overlap.get("permitted_language") or [],
                "boundaries": overlap.get("boundaries") or [],
            })
        graph = (context.get("research") or {}).get("graph") or {}
        lines += [
            "",
            "EMPLOYER RESEARCH CONTEXT:",
            f"- Public employer evidence describes {role.get('company') or 'the employer'}, not the owner. "
            "Use it to understand the employer and to choose emphasis; every "
            "statement about the owner still requires a Master History "
            "endnote anchor.",
            "- The personal bridge is a local interpretation, not canonical "
            "research evidence. Follow every listed boundary and re-check "
            "its claim-gate anchors before using permitted language.",
            f"- Canonical public-language graph: {graph.get('database', '')}",
            f"- Local application bridge: {bridge.get('path', '')}",
            "- Open the Vira Research view for expandable claim phrasings, "
            "speakers, events, repost relations, and root-source links.",
            "",
            "PRIORITIZED APPLICATION BRIDGE:",
            json.dumps(bridge_items, indent=1, ensure_ascii=False),
        ]
    lines += ["", "Close out exactly per the skill: tracker row `ready`, "
              "best-effort status mirror, open the package folder. Only the "
              "owner submits — never mark anything `applied`."]
    return "\n".join(lines)
