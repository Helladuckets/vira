"""Pivot reconnect — who in the dormant network is worth reactivating for
the active AI-role search, and why now.

The generic quiet/loops lists in brief.py and radar.py answer "who have I
neglected"; this module answers a narrower, career-shaped question: of the
people gone quiet, who sits closest to the companies and lanes the owner is
actually chasing right now? It is additive career-transition intelligence,
not a replacement for the relationship-maintenance lists.

Same two-rung shape as radar.py's groupings engine:

  1. Deterministic candidates — dormant/quiet CRM contacts scored against a
     PIVOT PICTURE (target companies, lanes, keywords) derived at runtime
     from the Applications universe. Nothing here is hardcoded to any
     particular company or role; the target picture is whatever the owner
     is currently chasing in Applications. Every point carries a reason.
  2. One AI curation pass picks the top 2-3 as "why them, why now" targets
     with a re-warming opener. A model outage falls back to the raw
     top-ranked candidates, honestly marked `curated: false`.

Cached in data/reconnect.json; dismissals persist and self-re-arm (the key
embeds the last-contact date, so a relationship that wakes up and goes
quiet again shows up as a fresh candidate). Dormant by construction when
the Applications universe is empty or absent — this module never invents
a target picture.
"""
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import brief
from . import data as crm
from . import radar
from . import settings
from . import suggest
from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "reconnect.json"

DORMANT_DAYS = 45        # quiet enough that reaching out is a reactivation
MAX_CANDIDATES = 15      # offered to the AI curator
TOP_N = 3                # targets kept after curation
FLOOR = 12               # below this a signal is coincidence, not relevance
SENIORITY_TOKENS = {"founder": 10, "ceo": 10, "cto": 10, "vp": 8, "head": 8,
                    "director": 6, "partner": 8, "principal": 5, "lead": 4,
                    "chief": 10, "recruiter": 6, "talent": 6}

_lock = threading.Lock()
_refresh_lock = threading.Lock()


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- the pivot picture (derived, never stored) ----------

def pivot_picture():
    """Target companies, lanes, and a keyword fingerprint, read straight off
    the live Applications universe. Cut roles (no sales / no commission /
    no marketing) contribute nothing — they are the lane the owner ruled
    out. Returns None when there is no universe to read (dormant module),
    per the brief.py degradation pattern."""
    try:
        from . import applications
        roles = applications.load_universe()
    except Exception:  # noqa: BLE001 — the universe is an optional input
        return None
    uncut = [r for r in roles if not r.get("cut")]
    if not uncut:
        return None
    companies = {}
    for r in uncut:
        name = (r.get("company") or "").strip()
        if not name:
            continue
        info = companies.setdefault(name.lower(),
                                    {"name": name, "tier1": 0, "shortlist": 0})
        if r.get("tier") == "1":
            info["tier1"] += 1
        if r.get("shortlist"):
            info["shortlist"] += 1
    if not companies:
        return None
    lanes = sorted({r["lane"] for r in uncut if r.get("lane")})
    text = " ".join(
        " ".join(str(r.get(k) or "") for k in
                 ("lane", "title", "why_fit", "lead_with"))
        for r in uncut)
    keywords = radar._words(text)
    override = settings.raw().get("pivot_keywords") or []
    keywords |= {str(k).strip().lower() for k in override if str(k).strip()}
    shortlisted = sorted((r for r in uncut if r.get("shortlist")),
                         key=lambda r: r["shortlist"])
    return {
        "companies": companies,
        "lanes": lanes,
        "keywords": keywords,
        "shortlist_titles": [r["title"] for r in shortlisted][:8],
    }


def _display_picture(picture):
    """The trimmed form the store/UI carry — display names, not the scoring
    internals."""
    return {
        "companies": sorted({info["name"]
                             for info in picture["companies"].values()}),
        "lanes": list(picture.get("lanes") or []),
    }


# ---------- who's current company/title, freshest source wins ----------

def _linkedin_positions():
    """casefolded full name -> {company, position} off the LinkedIn export —
    the freshest "where are they now" signal Vira has, since CRM master
    data can be stale. Exact-name match only: a loose match here would
    misattribute a company to the wrong contact."""
    try:
        from . import applications
        by_company = applications.connections_by_company()
    except Exception:  # noqa: BLE001 — the export is an optional input
        return {}
    out = {}
    for rows in by_company.values():
        for row in rows:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            out[name.casefold()] = {"company": row.get("company") or "",
                                    "position": row.get("position") or ""}
    return out


# ---------- who's dormant ----------

def dormant_candidates():
    """Every tiered, named, non-company CRM contact who has gone quiet (or
    has no recorded contact at all — a LinkedIn-only relationship can be
    exactly the right reconnect). Deliberately its own scan rather than
    brief._going_quiet(), which is active-tier-only and capped at 8."""
    c = crm._load()
    try:
        live = brief._live_imsg_last()
    except Exception:  # noqa: BLE001 — chat.db is optional off-Mac
        live = {}
    now = datetime.now()
    out = []
    for p in c["people"]:
        tier = p.get("profile_tier") or p.get("master_tier")
        if not tier:
            continue
        if (p.get("name") or "").startswith("("):
            continue
        if brief._is_company(p):
            continue
        last = crm._last_contact(p)
        for h in p.get("handles", {}).get("imessage", []):
            last = max(last, live.get(h, ""))
        days = None
        if last:
            try:
                days = (now - datetime.fromisoformat(last[:19])).days
            except ValueError:
                days = None
        if days is not None and days < DORMANT_DAYS:
            continue
        out.append({"pid": p["id"], "name": p.get("name") or p["id"],
                    "tier": tier, "days": days, "last_contact": last or None})
    return out


# ---------- deterministic scoring, every point carries a reason ----------

def score_candidates(picture):
    """Dormant contacts ranked by pivot relevance. Every component adds a
    reason string; a candidate whose total falls below FLOOR is dropped —
    a seniority token alone is not pivot relevance, but a target-company
    match alone is."""
    if not picture:
        return []
    c = crm._load()
    li = _linkedin_positions()
    companies = picture.get("companies") or {}
    keywords = picture.get("keywords") or set()
    out = []
    for cand in dormant_candidates():
        pid = cand["pid"]
        person = c["by_id"].get(pid)
        if not person:
            continue
        master = (crm.get_person(pid) or {}).get("master") or {}
        prof = c["profiles"].get(pid) or {}
        li_row = li.get((person.get("name") or "").casefold())
        if li_row and (li_row.get("company") or li_row.get("position")):
            company = li_row.get("company") or ""
            title = li_row.get("position") or ""
            source = "linkedin"
        else:
            company = master.get("company") or ""
            title = master.get("title") or ""
            source = "crm"

        score = 0.0
        reasons = []

        comp_l = company.strip().lower()
        if comp_l:
            best = None
            for key, info in companies.items():
                if key and (key in comp_l or comp_l in key):
                    if best is None or (info.get("shortlist", 0)
                                        > best[1].get("shortlist", 0)):
                        best = (key, info)
            if best:
                _key, info = best
                pts = 60 + (10 if info.get("shortlist") else 0)
                score += pts
                extra = (f" ({info['shortlist']} shortlist roles)"
                        if info.get("shortlist") else "")
                reasons.append(
                    f"works at {info['name']} — a target company{extra}")

        title_toks = set(re.findall(r"[a-z]+",
                                    (title + " " + company).lower()))
        sen_hits = [SENIORITY_TOKENS[t] for t in title_toks
                   if t in SENIORITY_TOKENS]
        if sen_hits:
            score += min(10, max(sen_hits))
            reasons.append(f"senior reach: {title}" if title
                           else "senior reach")

        toks = radar.person_tokens(person, prof, master)
        hits = toks & keywords
        strong = [t for t in hits if len(t) >= radar.MIN_SUBJECT_LEN]
        if strong:
            score += min(24, 6 * len(hits))
            top = sorted(hits, key=lambda t: (-len(t), t))[:3]
            reasons.append("pivot ground: " + ", ".join(top))

        days = cand["days"]
        if isinstance(days, (int, float)):
            if 45 <= days < 180:
                score += 6
                reasons.append(
                    f"quiet {int(days)}d — warm enough to reactivate")
            elif days >= 365:
                score -= 6
                reasons.append(f"dormant {int(days)}d")

        if score < FLOOR:
            continue
        out.append({
            "pid": pid, "name": cand["name"], "tier": cand["tier"],
            "days": days, "last_contact": cand["last_contact"],
            "company": company, "title": title, "company_source": source,
            "score": round(score, 1), "reasons": reasons,
        })
    out.sort(key=lambda x: -x["score"])
    return out[:MAX_CANDIDATES]


# ---------- the AI curation pass ----------

CURATE_PROMPT = """You are {owner}'s chief of staff, deciding which of \
{owner}'s dormant contacts are worth reactivating right now for {owner}'s \
active AI-role job search.

Target companies: {companies}
Lanes under consideration: {lanes}
Shortlisted role titles: {shortlist_titles}

Below are candidates — dormant or quiet contacts scored for pivot \
relevance, each with their current company/title (source-labeled: CRM or \
the LinkedIn export), how long they have been quiet, the deterministic \
reasons they were surfaced, and a short relationship summary.

Pick the 2-3 contacts most worth reactivating NOW for the pivot, from the \
candidate list only — never invent a person. For each pick:
  why    — one or two sentences naming the CONCRETE pivot value (their \
company, their reach, their lane) and why NOW is the moment
  opener — a short re-warming message {owner} could send that does not \
ask for a favor in the first line

Return ONLY a JSON object:
{{"targets": [{{"person_id": "pid", "why": "...", "opener": "..."}}]}}

Candidates:
{candidates}
"""


def _dossier(cd, prof):
    bits = [cd["name"], f"pid: {cd['pid']}"]
    comp_title = []
    if cd["company"]:
        comp_title.append(f"company: {cd['company']} ({cd['company_source']})")
    if cd["title"]:
        comp_title.append(f"title: {cd['title']}")
    if comp_title:
        bits.append(", ".join(comp_title))
    bits.append(f"quiet {cd['days']}d" if cd["days"] is not None
               else "no recorded contact")
    bits.append("reasons: " + "; ".join(cd["reasons"]))
    summ = prof.get("relationship_summary")
    if isinstance(summ, str) and summ:
        bits.append(summ[:200])
    return " | ".join(bits)


def _row(cd, *, why, opener, curated):
    lc = (cd.get("last_contact") or "never")[:10]
    return {
        "key": f"rec:{cd['pid']}:{lc}",
        "person_id": cd["pid"],
        "name": cd["name"],
        "company": cd["company"],
        "title": cd["title"],
        "days": cd["days"],
        "score": cd["score"],
        "reasons": cd["reasons"],
        "why": why,
        "opener": opener,
        "curated": curated,
    }


def refresh():
    """Regenerate the curated reconnect list (deterministic candidates + one
    AI curation pass). Serialized; safe to fire from a thread."""
    with _refresh_lock:
        picture = pivot_picture()
        if not picture:
            with _lock, locked(STORE):
                s = _read()
                s["generated"] = _now_iso()
                s["targets"] = []
                s["picture"] = {}
                s["curated"] = True
                _write(s)
            return []

        cands = score_candidates(picture)
        disp = _display_picture(picture)
        if not cands:
            with _lock, locked(STORE):
                s = _read()
                s["generated"] = _now_iso()
                s["targets"] = []
                s["picture"] = disp
                s["curated"] = True
                _write(s)
            return []

        c = crm._load()
        by_pid = {cd["pid"]: cd for cd in cands}
        blocks = [f"- {_dossier(cd, c['profiles'].get(cd['pid']) or {})[:600]}"
                 for cd in cands]
        owner = settings.get("owner_name") or "the owner"
        prompt = CURATE_PROMPT.format(
            owner=owner,
            companies=", ".join(disp["companies"]) or "(none named)",
            lanes=", ".join(disp["lanes"]) or "(none named)",
            shortlist_titles=(", ".join(picture.get("shortlist_titles") or [])
                              or "(none)"),
            candidates="\n".join(blocks)[:20_000])

        targets, curated = [], True
        try:
            parsed = suggest._extract_json(suggest.complete(prompt))
            for t in (parsed.get("targets") or [])[:TOP_N]:
                cd = by_pid.get(t.get("person_id"))
                if not cd:
                    continue
                targets.append(_row(cd, why=(t.get("why") or "")[:500],
                                    opener=(t.get("opener") or "")[:500],
                                    curated=True))
        except Exception:  # noqa: BLE001 — fall back to the raw ranking
            # never leave the surface empty because the model was down; the
            # fallback just cannot claim to be curated
            curated = False
            for cd in cands[:TOP_N]:
                targets.append(_row(cd, why="; ".join(cd["reasons"]),
                                    opener="", curated=False))

        seen = set()
        targets = [t for t in targets
                  if not (t["key"] in seen or seen.add(t["key"]))]
        with _lock, locked(STORE):
            s = _read()
            s["generated"] = _now_iso()
            s["targets"] = targets
            s["picture"] = disp
            s["curated"] = curated
            _write(s)
        return targets


# ---------- store ----------

def _blank():
    return {"generated": None, "targets": [], "picture": {}, "dismissed": [],
            "curated": True}


def _read():
    try:
        s = json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        s = _blank()
    if not isinstance(s, dict):
        s = _blank()
    base = _blank()
    for k, v in base.items():
        s.setdefault(k, v)
    return s


def _write(s):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_name(STORE.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STORE)


def _read_store():
    """Lock-free read for the hot path — writers land the file with an
    atomic rename, so a reader never sees a torn store."""
    return _read()


def list_targets():
    s = _read_store()
    dismissed = set(s["dismissed"])
    return {"generated": s["generated"],
            "targets": [t for t in s["targets"]
                       if t.get("key") not in dismissed],
            "picture": s["picture"], "curated": s.get("curated", True)}


def dismiss(key, restore=False):
    with _lock, locked(STORE):
        s = _read()
        d = set(s["dismissed"])
        (d.discard if restore else d.add)(key)
        s["dismissed"] = sorted(d)
        _write(s)
