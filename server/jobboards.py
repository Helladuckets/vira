"""Job boards — the live feed behind the Applications module.

The D6-a universe expansion (owner ballot 2026-07-15, built 2026-07-17):
where `applications.py` reads static teardown corpora, this module OWNS the
board layer — a registry of company job boards, deterministic fetchers per
ATS, a snapshot + diff state so every poll knows what is NEW and what
CLOSED, an iMessage ping when a new eligible role appears, and a poller
that runs the whole loop on a cadence. Everything expensive (deep-read
scoring) stays agent work dispatched on demand; everything here is plain
HTTP + JSON and safe to run every few minutes.

Design:
- **The registry is data, not code** (`boards.json` in the boards dir —
  default `<universe>/boards/` next to the candidate universe in the
  owner's self-record). Adding a company is one registry entry; the next
  poll sweeps it. Supported `ats` kinds: greenhouse, ashby, lever,
  microsoft (Eightfold pcsx), google (embedded-JSON careers pages), and
  `manual` for boards that cannot be fetched headlessly (surfaced in the
  UI as such, never silently dropped).
- **Snapshot + state live next to the registry** (`snapshot.json`,
  `state.json`) — the self-record is the source of truth for the search,
  so the fetched universe lives there too, where scoring sessions read
  and extend it. Roles that disappear from a board are marked `closed`,
  never deleted.
- **Eligibility gates the PING, not the data.** Every fetched role lands
  in the snapshot. A role is `eligible` when its location passes the
  owner's NYC-or-remote rule AND it survives the standing owner
  adjudication (comp `ote` / selling-marketing titles cut — reused from
  `applications._load_adjudication`; never cut by the board's function
  label). Only new eligible roles ping the phone; the rest are visible
  in the module's All-boards view.
- **Notifications ride notify.agent_ping** (the proven iMessage path) —
  one batched message per poll cycle, deduped per-uid in state so a
  restart never re-pings.
"""
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import jsonstore, settings

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 30
ATS_KINDS = ("greenhouse", "ashby", "lever", "microsoft", "google", "manual")
JD_CAP = 24000          # keep snapshot JDs bounded
NOTIFY_TITLES = 3       # titles named in a ping before "+ k more"
NOTIFY_RETRY_DAYS = 2   # how long a failed ping keeps retrying
FRESH_DAYS = 10         # how long a role counts as NEW in the UI

_lock = threading.Lock()


# ------------------------------------------------------------------ paths

def boards_dir() -> Path:
    override = settings.raw().get("applications_boards")
    if override:
        return Path(str(override)).expanduser()
    from . import applications
    return applications.universe_dir() / "boards"


def _registry_path():
    return boards_dir() / "boards.json"


def _snapshot_path():
    return boards_dir() / "snapshot.json"


def _state_path():
    return boards_dir() / "state.json"


def _read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path, obj):
    jsonstore.write_atomic(path, obj, indent=1, ensure_ascii=False)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------- registry

def load_registry():
    reg = _read_json(_registry_path(), {})
    boards = reg.get("boards") if isinstance(reg, dict) else None
    return {"boards": boards if isinstance(boards, list) else []}


def add_board(company, ats, slug="", query="", location="", note=""):
    company = (company or "").strip()
    ats = (ats or "").strip().lower()
    if not company:
        raise ValueError("company is required")
    if ats not in ATS_KINDS:
        raise ValueError(f"ats must be one of {ATS_KINDS}")
    if ats in ("greenhouse", "ashby", "lever") and not slug.strip():
        raise ValueError(f"{ats} boards need a slug")
    if ats in ("microsoft", "google") and not query.strip():
        raise ValueError(f"{ats} boards need a query")
    with _lock:
        reg = load_registry()
        key = _board_key({"company": company, "ats": ats,
                          "slug": slug, "query": query})
        if any(_board_key(b) == key for b in reg["boards"]):
            raise ValueError("that board is already registered")
        reg["boards"].append({
            "company": company, "ats": ats, "slug": slug.strip(),
            "query": query.strip(), "location": location.strip(),
            "note": note.strip(), "added": _now()[:10],
        })
        _write_json(_registry_path(), reg)
    return reg


def _board_key(b):
    return (b.get("ats", ""), b.get("slug", "") or b.get("query", ""),
            b.get("company", ""))


def _board_id(b):
    base = b.get("slug") or re.sub(r"[^a-z0-9]+", "-",
                                   (b.get("company") or "").lower())
    return f"{b.get('ats')}-{base}".strip("-")


# ------------------------------------------------------------ http helper

def _get(url, as_json=True, headers=None):
    import requests
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    r = requests.get(url, headers=h, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json() if as_json else r.text


def _strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = (text.replace("&amp;", "&").replace("&lt;", "<")
            .replace("&gt;", ">").replace("&#39;", "'")
            .replace("&quot;", '"').replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", text).strip()


def _comp_kind(jd_text, salary_min=None):
    """`ote` is the hard cut signal; `base` only claimed when a salary is
    actually stated without an OTE marker."""
    if re.search(r"on[- ]target earnings|\bOTE\b|uncapped commission",
                 jd_text or "", re.I):
        return "ote"
    if salary_min:
        return "base"
    return ""


# ---------------------------------------------------------------- fetchers

def fetch_greenhouse(board):
    slug = board["slug"]
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}"
                f"/jobs?content=true")
    out = []
    for j in data.get("jobs") or []:
        jd = _strip_html(j.get("content") or "")[:JD_CAP]
        locs = []
        if (j.get("location") or {}).get("name"):
            locs = [x.strip() for x in j["location"]["name"].split(";")
                    if x.strip()]
        sal_min = sal_max = None
        for rng in j.get("pay_input_ranges") or []:
            try:
                lo = float(rng.get("min_cents", 0)) / 100
                hi = float(rng.get("max_cents", 0)) / 100
            except (TypeError, ValueError):
                continue
            sal_min = lo if sal_min is None else min(sal_min, lo)
            sal_max = hi if sal_max is None else max(sal_max, hi)
        out.append(_norm(
            board, uid=f"g-{slug}-{j.get('id')}",
            title=j.get("title"), dept=(j.get("departments") or [{}])[0].get("name", ""),
            locations=locs, salary_min=sal_min, salary_max=sal_max,
            url=j.get("absolute_url"), published=(j.get("first_published")
                                                  or j.get("updated_at")
                                                  or "")[:10],
            jd=jd))
    return out


def fetch_ashby(board):
    slug = board["slug"]
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
                f"?includeCompensation=true")
    out = []
    for j in data.get("jobs") or []:
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""]
        for sec in j.get("secondaryLocations") or []:
            locs.append(sec.get("location") or "")
        locs = [x for x in locs if x]
        if j.get("isRemote") and not any("remote" in x.lower() for x in locs):
            locs.append("Remote")
        comp = j.get("compensation") or {}
        sal_min = sal_max = None
        for tier in comp.get("summaryComponents") or []:
            if tier.get("compensationType") == "Salary":
                sal_min = tier.get("minValue")
                sal_max = tier.get("maxValue")
        jd = _strip_html(j.get("descriptionHtml")
                         or j.get("descriptionPlain") or "")[:JD_CAP]
        out.append(_norm(
            board, uid=f"as-{slug}-{j.get('id')}",
            title=j.get("title"), dept=j.get("department") or "",
            team=j.get("team") or "", locations=locs,
            salary_min=sal_min, salary_max=sal_max,
            url=j.get("jobUrl") or j.get("applyUrl"),
            published=(j.get("publishedAt") or "")[:10], jd=jd))
    return out


def fetch_lever(board):
    slug = board["slug"]
    data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        locs = list(cats.get("allLocations") or
                    ([cats.get("location")] if cats.get("location") else []))
        if (j.get("workplaceType") or "").lower() == "remote" and \
                not any("remote" in x.lower() for x in locs):
            locs.append("Remote")
        rng = j.get("salaryRange") or {}
        ts = j.get("createdAt")
        published = (datetime.fromtimestamp(ts / 1000, timezone.utc)
                     .date().isoformat() if ts else "")
        jd = _strip_html(" ".join(
            [j.get("descriptionPlain") or ""] +
            [" ".join(_strip_html(c.get("content") or "")
                      for c in (j.get("lists") or []))]))[:JD_CAP]
        out.append(_norm(
            board, uid=f"lv-{slug}-{j.get('id')}",
            title=j.get("text"), dept=cats.get("department") or "",
            team=cats.get("team") or "", locations=locs,
            salary_min=rng.get("min"), salary_max=rng.get("max"),
            url=j.get("hostedUrl"), published=published, jd=jd))
    return out


def fetch_microsoft(board):
    """Eightfold pcsx search on apply.careers.microsoft.com. Query-scoped
    (a company-wide sweep is 10k+ roles); the registry's `location` narrows
    server-side and a second remote pass catches work-from-home roles."""
    query = board.get("query") or ""
    passes = [board.get("location") or "New York"]
    out, seen = [], set()
    for loc in passes:
        start, total = 0, None
        while start < (300 if total is None else min(total, 300)):
            data = _get(
                "https://apply.careers.microsoft.com/api/pcsx/search"
                f"?domain=microsoft.com&query={_q(query)}"
                f"&location={_q(loc)}&start={start}&num=10")
            payload = (data or {}).get("data") or {}
            total = payload.get("count") or 0
            positions = payload.get("positions") or []
            if not positions:
                break
            for p in positions:
                uid = f"ms-{p.get('id')}"
                if uid in seen:
                    continue
                seen.add(uid)
                ts = p.get("postedTs") or p.get("creationTs")
                published = (datetime.fromtimestamp(ts, timezone.utc)
                             .date().isoformat() if ts else "")
                locs = list(p.get("standardizedLocations") or
                            p.get("locations") or [])
                if (p.get("workLocationOption") or "") == "remote" and \
                        not any("remote" in x.lower() for x in locs):
                    locs.append("Remote")
                out.append(_norm(
                    board, uid=uid, title=p.get("name"),
                    dept=p.get("department") or "", locations=locs,
                    url=("https://apply.careers.microsoft.com"
                         + (p.get("positionUrl") or "")),
                    published=published, jd=""))
            start += 10
    return out


def fetch_google(board):
    """Google's careers site server-renders results with the data embedded
    in an AF_initDataCallback block — parse it out. Query-scoped (e.g.
    '"DeepMind"'); entries are kept only when the embedded company field
    matches the registry company, so stray full-text hits drop."""
    query = board.get("query") or ""
    # the embedded company field is e.g. "DeepMind" — match on the query
    # text (quotes stripped), not the registry's display company name
    want = query.strip().strip('"').lower()
    out, seen = [], set()
    for page in range(1, 11):
        html = _get("https://www.google.com/about/careers/applications/"
                    f"jobs/results?q={_q(query)}&page={page}", as_json=False)
        m = re.search(r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(.*?)"
                      r", sideChannel", html, re.S)
        if not m:
            break
        try:
            jobs = json.loads(m.group(1))[0] or []
        except (json.JSONDecodeError, IndexError, TypeError):
            break
        fresh = 0
        for j in jobs:
            try:
                jid, title, company = j[0], j[1], j[7]
            except (IndexError, TypeError):
                continue
            uid = f"gg-{jid}"
            if uid in seen:
                continue
            seen.add(uid)
            fresh += 1
            if want and want not in (company or "").lower():
                continue
            locs = []
            for loc in (j[9] or []) if len(j) > 9 else []:
                if isinstance(loc, list) and loc and isinstance(loc[0], str):
                    locs.append(loc[0])
            jd = " ".join(_strip_html(part[1])
                          for part in (j[10:11] or []) + [j[3], j[4]]
                          if isinstance(part, list) and len(part) > 1
                          and isinstance(part[1], str))[:JD_CAP]
            ts = None
            if len(j) > 12 and isinstance(j[12], list) and j[12]:
                ts = j[12][0]
            out.append(_norm(
                board, uid=uid, title=title, dept="",
                locations=locs,
                url=("https://www.google.com/about/careers/applications/"
                     f"jobs/results/{jid}"),
                published=(datetime.fromtimestamp(ts, timezone.utc)
                           .date().isoformat() if ts else ""),
                jd=jd))
        if fresh < 20:
            break
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "ashby": fetch_ashby,
    "lever": fetch_lever,
    "microsoft": fetch_microsoft,
    "google": fetch_google,
}


def _q(s):
    from urllib.parse import quote
    return quote(s or "")


def _norm(board, uid, title, dept="", team="", locations=None,
          salary_min=None, salary_max=None, url="", published="", jd=""):
    locations = [str(x) for x in (locations or []) if x]
    remote = "remote" if any("remote" in x.lower() for x in locations) else ""
    return {
        "uid": uid,
        "company": board.get("company") or "?",
        "title": (title or "?").strip(),
        "dept": dept or "",
        "team": team or dept or "",
        "function": dept or "",
        "seniority": "",
        "salaryMin": salary_min,
        "salaryMax": salary_max,
        "currency": "USD" if salary_min else "",
        "comp": _comp_kind(jd, salary_min),
        "remote": remote,
        "locations": locations,
        "url": url or "",
        "apply": url or "",
        "published": published,
        "blurb": (jd or "")[:400],
        "jd": jd or "",
        "board": _board_id(board),
    }


# ------------------------------------------------------------- eligibility

NYC_RE = re.compile(r"new york|nyc", re.I)
REMOTE_RE = re.compile(r"\bremote\b", re.I)
NON_US_RE = re.compile(
    r"europe|emea|\buk\b|london|paris|munich|berlin|frankfurt|madrid|"
    r"dublin|amsterdam|zurich|geneva|stockholm|warsaw|canada|toronto|"
    r"vancouver|montreal|india|bangalore|mumbai|apac|australia|sydney|"
    r"brazil|s[aã]o paulo|mexico|bogot|singapore|japan|tokyo|korea|seoul|"
    r"israel|tel aviv|riyadh|dubai|saudi|\buae\b|hong kong|taipei|"
    r"beijing|shanghai|middle east|latam", re.I)
US_HINT_RE = re.compile(
    r"\bUSA?\b|United States|San Francisco|Seattle|Austin|Boston|"
    r"Washington|Chicago|Los Angeles|Palo Alto|Mountain View|Denver|"
    r"Miami|Atlanta|,\s*[A-Z]{2}(?:,|\s|$)")


def eligible_location(rec):
    """The owner's location rule: New York City, or US-reachable remote.
    A bare 'Remote' tag on a role whose named locations are all non-US
    (Cohere Korea, Riyadh, ...) does not qualify."""
    locs = rec.get("locations") or []
    for loc in locs:
        if NYC_RE.search(loc):
            return True
    if not any(REMOTE_RE.search(loc) for loc in locs):
        return False
    named = [loc for loc in locs if not REMOTE_RE.search(loc)]
    if named and any(NON_US_RE.search(loc) for loc in named) \
            and not any(US_HINT_RE.search(loc) for loc in named):
        return False
    return not any(NON_US_RE.search(loc) for loc in locs if
                   REMOTE_RE.search(loc))


def _adjudication():
    from . import applications
    try:
        return applications._load_adjudication(applications.universe_dir())
    except Exception:  # noqa: BLE001 — a broken file must not stop a poll
        return None


def evaluate(rec, adj):
    """Stamp `eligible` (location) and `cut` (owner adjudication) onto a
    snapshot record. Cut is by comp structure and TITLE only — never the
    board's function label (three of the owner's eight picks carry a
    'Sales & GTM' label)."""
    rec["eligible"] = eligible_location(rec)
    rec["cut"] = ""
    if adj:
        if rec.get("comp") in adj["cut_comp"]:
            rec["cut"] = adj["reason_comp"]
        else:
            for pat in adj["cut_titles"]:
                if pat.search(rec.get("title") or ""):
                    rec["cut"] = adj["reason_title"]
                    break
    return rec


# ------------------------------------------------------------ poll + diff

def poll_once(notify_new=True):
    """Fetch every pollable board, diff against state, mark new/closed,
    ping the owner about new eligible roles. Returns a summary dict."""
    reg = load_registry()
    if not reg["boards"]:
        return {"ok": False, "reason": "no boards registered"}
    adj = _adjudication()
    with _lock:
        snapshot = _read_json(_snapshot_path(), {})
        state = _read_json(_state_path(), {})
    roles = dict(snapshot.get("roles") or {})
    board_meta = dict(snapshot.get("boards") or {})
    st_roles = dict(state.get("roles") or {})
    now = _now()
    new_uids, closed_uids = [], []

    for b in reg["boards"]:
        bid = _board_id(b)
        fetcher = FETCHERS.get(b.get("ats"))
        if fetcher is None:
            board_meta[bid] = {"company": b.get("company"),
                               "ats": b.get("ats"), "ok": False,
                               "manual": True, "at": now,
                               "note": b.get("note")
                               or "not headlessly pollable"}
            continue
        try:
            fetched = fetcher(b)
        except Exception as e:  # noqa: BLE001 — one board never kills a poll
            board_meta[bid] = {"company": b.get("company"),
                               "ats": b.get("ats"), "ok": False,
                               "error": str(e)[:200], "at": now}
            continue
        fetched_uids = set()
        for rec in fetched:
            evaluate(rec, adj)
            uid = rec["uid"]
            fetched_uids.add(uid)
            prior = st_roles.get(uid)
            if prior is None:
                st_roles[uid] = {"first_seen": now, "last_seen": now}
                new_uids.append(uid)
            else:
                prior["last_seen"] = now
                prior.pop("closed", None)
            rec["first_seen"] = st_roles[uid]["first_seen"]
            # baseline roles (the initial load) are never "NEW" in the UI
            if st_roles[uid].get("notified") == "baseline":
                rec["baseline"] = True
            rec.pop("closed", None)
            roles[uid] = rec
        # anything this board owned that vanished is closed (not deleted)
        for uid, rec in roles.items():
            if rec.get("board") == bid and uid not in fetched_uids \
                    and not rec.get("closed"):
                rec["closed"] = now
                st_roles.setdefault(uid, {})["closed"] = now
                closed_uids.append(uid)
        board_meta[bid] = {"company": b.get("company"), "ats": b.get("ats"),
                           "ok": True, "count": len(fetched_uids), "at": now}

    eligible_new = [roles[u] for u in new_uids
                    if roles[u].get("eligible") and not roles[u].get("cut")]
    notified = 0
    if notify_new:
        # candidates: any open eligible role still lacking the notified
        # stamp and seen first within the retry window — so a transient
        # iMessage failure retries next poll instead of being swallowed
        cutoff = time.time() - NOTIFY_RETRY_DAYS * 86400
        cands = []
        for uid, rec in roles.items():
            if rec.get("closed") or not rec.get("eligible") or rec.get("cut"):
                continue
            if (st_roles.get(uid) or {}).get("notified"):
                continue
            try:
                first = datetime.fromisoformat(
                    (st_roles.get(uid) or {}).get("first_seen") or "")
            except ValueError:
                continue
            if first.timestamp() > cutoff:
                cands.append(rec)
        notified = _notify_new(cands, st_roles) if cands else 0
    else:
        # baseline sweep (initial load, CLI --no-notify): stamp so these
        # never storm the phone once notifications turn on
        for uid in new_uids:
            if roles[uid].get("eligible") and not roles[uid].get("cut"):
                st_roles.setdefault(uid, {})["notified"] = "baseline"

    with _lock:
        _write_json(_snapshot_path(), {"fetched": now, "boards": board_meta,
                                       "roles": roles})
        _write_json(_state_path(), {"roles": st_roles})
    return {"ok": True, "at": now, "boards": board_meta,
            "total": len([r for r in roles.values() if not r.get("closed")]),
            "new": len(new_uids), "eligible_new": len(eligible_new),
            "closed": len(closed_uids), "notified": notified}


def _notify_new(eligible_new, st_roles):
    """One batched iMessage per poll cycle; per-uid dedupe in state."""
    from . import notify
    fresh = [r for r in eligible_new
             if not (st_roles.get(r["uid"]) or {}).get("notified")]
    if not fresh:
        return 0
    parts = []
    for r in fresh[:NOTIFY_TITLES]:
        loc = "NYC" if any(NYC_RE.search(x) for x in r["locations"]) \
            else "Remote"
        parts.append(f"{r['company']}: {r['title']} ({loc})")
    text = f"Vira: {len(fresh)} new job{'s' if len(fresh) != 1 else ''} — " \
           + "; ".join(parts)
    if len(fresh) > NOTIFY_TITLES:
        text += f"; +{len(fresh) - NOTIFY_TITLES} more"
    text += " — open Applications"
    ok = notify.agent_ping(text, key="jobboards:" +
                           ",".join(sorted(r["uid"] for r in fresh))[:80])
    if ok:
        for r in fresh:
            st_roles.setdefault(r["uid"], {})["notified"] = _now()
    return len(fresh) if ok else 0


# ------------------------------------------------------------------ status

def status():
    reg = load_registry()
    snapshot = _read_json(_snapshot_path(), {})
    state = _read_json(_state_path(), {})
    roles = snapshot.get("roles") or {}
    st = state.get("roles") or {}
    open_roles = {u: r for u, r in roles.items() if not r.get("closed")}
    cutoff = time.time() - FRESH_DAYS * 86400
    fresh = unscored = 0
    scored = _scored_uids()
    for uid, r in open_roles.items():
        first = (st.get(uid) or {}).get("first_seen") or r.get("first_seen")
        try:
            is_fresh = first and not r.get("baseline") \
                and (st.get(uid) or {}).get("notified") != "baseline" \
                and datetime.fromisoformat(first).timestamp() > cutoff
        except ValueError:
            is_fresh = False
        if is_fresh:
            fresh += 1
        if r.get("eligible") and not r.get("cut") and uid not in scored:
            unscored += 1
    return {
        "registered": len(reg["boards"]),
        "boards": snapshot.get("boards") or {},
        "fetched": snapshot.get("fetched") or "",
        "roles_open": len(open_roles),
        "eligible": sum(1 for r in open_roles.values()
                        if r.get("eligible") and not r.get("cut")),
        "fresh": fresh,
        "unscored_eligible": unscored,
    }


def _scored_uids():
    from . import applications
    udir = applications.universe_dir()
    uids = set()
    for sf in sorted(udir.glob("*-raw-scores.json")):
        try:
            for s in json.loads(sf.read_text()):
                for k in (s.get("uid"), s.get("_fulluid")):
                    if k:
                        uids.add(k)
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return uids


# --------------------------------------------------------- score dispatch

def score_prompt(limit=40):
    """The prompt a 'Score new roles' dispatch hands an agent session
    (cwd = the self-record, so its CLAUDE.md claim gate loads). The
    session deep-reads the unscored eligible roles in the boards snapshot
    and extends the candidate universe the same way the D6 pass did."""
    from . import applications
    udir = applications.universe_dir()
    snapshot = _read_json(_snapshot_path(), {})
    scored = _scored_uids()
    todo = [r for r in (snapshot.get("roles") or {}).values()
            if r.get("eligible") and not r.get("cut")
            and not r.get("closed") and r["uid"] not in scored]
    todo.sort(key=lambda r: r.get("first_seen") or "", reverse=True)
    todo = todo[:limit]
    lines = [
        "Score the NEW job-board roles into the candidate universe. "
        "Follow the standing method exactly:",
        "",
        f"1. Read the boards snapshot at {_snapshot_path()} — the roles "
        "to score are listed below by uid.",
        f"2. Read {udir}/2026-07-17_owner-adjudication.md (the standing "
        "ruling) and score with the TWO-SCORE discipline: narrative "
        "resonance AND screening probability, separately. Hard minimums "
        "in a JD are probability screens, not reframes.",
        "3. Every claim about the owner's background passes the FACTS.md "
        "gate (this folder's CLAUDE.md governs).",
        f"4. For each role, write a role file at {udir}/candidate-universe/"
        "role/<uid>.json (same shape as the existing files) and append a "
        f"score entry to {udir}/d6-raw-scores.json (same shape as "
        "v2-raw-scores.json: uid, fit, tier, final_tier, lane, why_fit, "
        "lead_with, caveat, comp_note, verdict).",
        "5. Do NOT touch v2-raw-scores.json, owner-adjudication.json, or "
        "the owner's eight picks.",
        "",
        f"ROLES TO SCORE ({len(todo)}):",
    ]
    for r in todo:
        lines.append(json.dumps(
            {k: r.get(k) for k in ("uid", "company", "title", "locations",
                                   "salaryMin", "salaryMax", "comp", "url")},
            ensure_ascii=False))
    return "\n".join(lines), len(todo)


# ------------------------------------------------------------------ poller

class Poller(threading.Thread):
    """Background poll loop — ticks every minute, polls every
    `boards_poll_minutes` (default 15). Dormant until boards are
    registered. Started from main._startup, skipped under VIRA_PASSIVE
    like every worker."""

    def __init__(self):
        super().__init__(daemon=True, name="vira-jobboards")
        self.status = "starting"
        self.next_poll = time.time() + 90     # settle after boot

    def poll_now(self):
        self.next_poll = 0.0

    def run(self):
        while True:
            try:
                if not load_registry()["boards"]:
                    self.status = "dormant — no boards registered"
                elif time.time() >= self.next_poll:
                    r = poll_once()
                    minutes = float(settings.raw().get("boards_poll_minutes")
                                    or 15)
                    self.next_poll = time.time() + minutes * 60
                    self.status = (f"ok — {r.get('new', 0)} new / "
                                   f"{r.get('closed', 0)} closed at "
                                   f"{datetime.now().strftime('%H:%M')}")
            except Exception as e:  # noqa: BLE001 — the loop never dies
                self.status = f"error: {str(e)[:160]}"
                self.next_poll = time.time() + 900
            time.sleep(60)


# ---------------------------------------------------------------- CLI

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "sweep":
        print(json.dumps(poll_once(notify_new="--notify" in sys.argv),
                         indent=1))
    elif cmd == "status":
        print(json.dumps(status(), indent=1))
    else:
        print("usage: python -m server.jobboards [sweep|status]")
