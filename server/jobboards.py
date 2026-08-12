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

from . import jobshared, jsonstore, settings, workplace

TIMEOUT = 30
ATS_KINDS = tuple(jobshared.ATS_PREFIX) + ("manual",)
JD_CAP = 24000          # keep snapshot JDs bounded
NOTIFY_TITLES = 3       # titles named in a ping before "+ k more"
NOTIFY_RETRY_DAYS = 2   # how long a failed ping keeps retrying
FRESH_DAYS = 10         # how long a role counts as NEW in the UI
VERIFY_DAYS = 2         # how long a sweep's "still listed" stays credible

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
    return jobshared.now_iso()


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
    return jobshared.http_get(url, as_json, headers, TIMEOUT)


def _strip_html(text):
    """A fetched description -> the readable text the snapshot stores.

    This used to strip tags and THEN unescape entities, which is exactly
    backwards for a board that escapes its description inside the JSON
    string: Greenhouse's `&lt;div&gt;` matched no tag, so the strip did
    nothing and the unescape turned the markup into literal text — every
    Anthropic jd in the snapshot began `<div class="content-intro">`, and
    every scoring session deep-read that. It also flattened whitespace,
    so a real HTML body became one 10,000-character line with no
    paragraph or bullet left in it.

    `jobdesc.to_markdown` is the one converter now; the snapshot holds
    what the description panel renders and what a session can read.
    """
    from . import jobdesc
    return jobdesc.to_markdown(text)


def snapshot_role(uid):
    """(role record, the sweep stamp it was fetched on) for one uid — the
    read `jobdesc` needs, so the snapshot's shape stays this module's
    business rather than something another module reaches into."""
    snap = _read_json(_snapshot_path(), {})
    roles = snap.get("roles")
    rec = roles.get(uid) if isinstance(roles, dict) else None
    return (rec if isinstance(rec, dict) else None), (snap.get("fetched") or "")


_CONDITIONAL_SALES_OTE = re.compile(
    r"\bfor sales roles,\s*the range provided is the role(?:'|’)?s\s+"
    r"on[- ]target earnings\s*\([^)]*\)\s*range,\s*meaning that the range\s+"
    r"includes both the sales commissions?/sales bonuses? target and annual\s+"
    r"base salary for the role\.?",
    re.I)

_ROLE_OTE = re.compile(
    r"on[- ]target earnings|\bOTE\b|uncapped commission|quota[- ]carrying|"
    r"commission[- ]based compensation|"
    r"base salary\s+(?:plus|and)\s+(?:a\s+)?commission|"
    r"eligible for (?:a\s+)?variable (?:compensation|pay)",
    re.I)


def _comp_kind(jd_text, salary_min=None):
    """Classify the role's compensation, not a board-wide disclosure.

    Some employers append a conditional explanation of what the displayed
    range means *for sales roles* to every posting.  That sentence is not
    evidence that the posting in hand is quota-compensated, so discard it
    before looking for role-specific OTE markers.  Selling titles remain an
    independent owner-adjudication cut in jobshared.cut_reason.
    """
    role_text = _CONDITIONAL_SALES_OTE.sub(" ", jd_text or "")
    if _ROLE_OTE.search(role_text):
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
            board, uid=jobshared.board_uid("greenhouse", j.get("id"), slug),
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
        comp = j.get("compensation") or {}
        sal_min = sal_max = None
        for tier in comp.get("summaryComponents") or []:
            if tier.get("compensationType") == "Salary":
                sal_min = tier.get("minValue")
                sal_max = tier.get("maxValue")
        jd = _strip_html(j.get("descriptionHtml")
                         or j.get("descriptionPlain") or "")[:JD_CAP]
        out.append(_norm(
            board, uid=jobshared.board_uid("ashby", j.get("id"), slug),
            title=j.get("title"), dept=j.get("department") or "",
            team=j.get("team") or "", locations=locs,
            salary_min=sal_min, salary_max=sal_max,
            url=j.get("jobUrl") or j.get("applyUrl"),
            published=(j.get("publishedAt") or "")[:10], jd=jd,
            remote_flag=bool(j.get("isRemote"))))
    return out


def fetch_lever(board):
    slug = board["slug"]
    data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        locs = list(cats.get("allLocations") or
                    ([cats.get("location")] if cats.get("location") else []))
        rng = j.get("salaryRange") or {}
        ts = j.get("createdAt")
        published = (datetime.fromtimestamp(ts / 1000, timezone.utc)
                     .date().isoformat() if ts else "")
        jd = _strip_html(" ".join(
            [j.get("descriptionPlain") or ""] +
            [" ".join(_strip_html(c.get("content") or "")
                      for c in (j.get("lists") or []))]))[:JD_CAP]
        out.append(_norm(
            board, uid=jobshared.board_uid("lever", j.get("id"), slug),
            title=j.get("text"), dept=cats.get("department") or "",
            team=cats.get("team") or "", locations=locs,
            salary_min=rng.get("min"), salary_max=rng.get("max"),
            url=j.get("hostedUrl"), published=published, jd=jd,
            remote_flag=(j.get("workplaceType") or "").lower() == "remote"))
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
                uid = jobshared.board_uid("microsoft", p.get("id"))
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
            uid = jobshared.board_uid("google", jid)
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

# Which fetchers return the COMPLETE board, so absence is proof a posting
# is gone. greenhouse/ashby/lever hand back every listed role; microsoft
# and google are QUERY searches, where a role can fall out of the result
# set while the posting is perfectly alive — so a miss there is only
# evidence about roles that board has actually served before, never proof
# about the wider namespace.
FULL_BOARD = ("greenhouse", "ashby", "lever")


def _q(s):
    from urllib.parse import quote
    return quote(s or "")


def _norm(board, uid, title, dept="", team="", locations=None,
          salary_min=None, salary_max=None, url="", published="", jd="",
          remote_flag=False):
    """One role, in the module's shape.

    `remote_flag` is the BOARD's remote checkbox (Ashby `isRemote`,
    Lever `workplaceType`). It is an inference, not the employer's own
    words, and it is wrong often enough to matter: on OpenAI's board 422
    of 735 listed roles set it while their descriptions say the role is
    based in one named office with a hybrid schedule. So the flag only
    earns a "Remote" location when the body does not contradict it.

    A location string the board actually PUBLISHED ("US - Remote") is
    left exactly as written even when the body contradicts it -- those
    are the employer's words and rewriting them would hide the
    disagreement. `workplace` carries the body's reading alongside, and
    eligibility reads that; the row shows both, so a posting whose two
    halves disagree looks like what it is.
    """
    locations = [str(x) for x in (locations or []) if x]
    wp = workplace.read(jd)
    if remote_flag and not any("remote" in x.lower() for x in locations) \
            and not (wp and wp["binds"]):
        locations.append("Remote")
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
        "workplace": wp,
        "locations": locations,
        "url": url or "",
        "apply": url or "",
        "published": published,
        "blurb": (jd or "")[:400],
        "jd": jd or "",
        "board": _board_id(board),
    }


# ------------------------------------------------------------- eligibility

REMOTE_RE = re.compile(r"\bremote\b", re.I)

# The out-of-region and in-region hint sets. These are DEFAULTS for an
# owner who works in the US — the shipped starting point for
# `applications_remote_exclude` / `applications_region_hints`, both of
# which config overrides wholesale. They are not a claim about where jobs
# are worth having.
DEFAULT_REMOTE_EXCLUDE = (
    r"europe|emea|\buk\b|london|paris|munich|berlin|frankfurt|madrid|"
    r"dublin|amsterdam|zurich|geneva|stockholm|warsaw|canada|toronto|"
    r"vancouver|montreal|india|bangalore|mumbai|apac|australia|sydney|"
    r"brazil|s[aã]o paulo|mexico|bogot|singapore|japan|tokyo|korea|seoul|"
    r"israel|tel aviv|riyadh|dubai|saudi|\buae\b|hong kong|taipei|"
    r"beijing|shanghai|middle east|latam")
DEFAULT_REGION_HINTS = (
    r"\bUSA?\b|United States|San Francisco|Seattle|Austin|Boston|"
    r"Washington|Chicago|Los Angeles|Palo Alto|Mountain View|Denver|"
    r"Miami|Atlanta|,\s*[A-Z]{2}(?:,|\s|$)")


def _rx(pattern):
    """A compiled pattern, or None when the owner cleared it (empty
    string / empty list means 'do not apply this test at all')."""
    if not pattern:
        return None
    if isinstance(pattern, (list, tuple)):
        parts = [str(p).strip() for p in pattern if str(p).strip()]
        if not parts:
            return None
        pattern = "|".join(re.escape(p) for p in parts)
    try:
        return re.compile(pattern, re.I)
    except re.error:            # a bad config pattern must not stop a poll
        return None


def location_rule():
    """The owner's location rule, read from config.

    `applications_locations` is a list of place substrings the owner will
    work in ("New York", "NYC"). `applications_remote_ok` (default true)
    accepts remote roles. `applications_remote_exclude` and
    `applications_region_hints` tune which remote postings are actually
    reachable — a bare "Remote" on a role whose named cities are all in
    another region usually is not."""
    cfg = settings.raw()
    return {
        "places": _rx(cfg.get("applications_locations")),
        "remote_ok": cfg.get("applications_remote_ok", True) is not False,
        "exclude": _rx(cfg.get("applications_remote_exclude")
                       if "applications_remote_exclude" in cfg
                       else DEFAULT_REMOTE_EXCLUDE),
        "hints": _rx(cfg.get("applications_region_hints")
                     if "applications_region_hints" in cfg
                     else DEFAULT_REGION_HINTS),
    }


def eligible_location(rec, rule=None):
    """Whether a role's location clears the owner's rule.

    UNCONFIGURED MEANS UNFILTERED. An install that has never said where
    its owner will work sees every role — inheriting some other owner's
    city was the old behavior and it silently emptied the module for
    anyone who did not live in it. Eligibility gates the phone ping and
    the default view, never what is fetched: every role lands in the
    snapshot either way."""
    rule = rule or location_rule()
    if rule["places"] is None and rule["remote_ok"]:
        return True                       # nothing configured, nothing cut
    locs = rec.get("locations") or []

    # THE BODY OUTRANKS THE LOCATION FIELD. A posting whose description
    # says "based in San Francisco, CA -- hybrid, 3 days in the office"
    # is not reachable from New York whatever its location field or its
    # board's remote checkbox claims, and 179 snapshot roles read as
    # eligible on exactly that mistake. The reading only ever narrows:
    # it names offices, so it can refuse, but it never manufactures
    # eligibility a location string did not already support.
    wp = rec.get("workplace")
    if wp and not workplace.allows(wp, rule["places"], locs):
        return False

    if rule["places"] is not None:
        for loc in locs:
            if rule["places"].search(loc):
                return True
    if not rule["remote_ok"]:
        return False
    if not any(REMOTE_RE.search(loc) for loc in locs):
        return False
    if rule["exclude"] is None:
        return True
    named = [loc for loc in locs if not REMOTE_RE.search(loc)]
    if named and any(rule["exclude"].search(loc) for loc in named) \
            and not (rule["hints"] is not None
                     and any(rule["hints"].search(loc) for loc in named)):
        return False
    return not any(rule["exclude"].search(loc) for loc in locs if
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
    'Sales & GTM' label); jobshared.cut_reason is the one implementation
    both this and applications._apply_adjudication use."""
    rec["eligible"] = eligible_location(rec)
    rec["cut"] = jobshared.cut_reason(rec.get("comp"), rec.get("title"), adj)
    return rec


# ------------------------------------------------------------ poll + diff

def _catalog_uids():
    """Every uid the Applications catalog holds — the curated universe AND
    the raw corpora behind it. A board sweep closes against these too, not
    only against what the snapshot already knows.

    The corpora are FROZEN files (a teardown captured on one day and never
    refetched), so a role in them is exactly the kind that can quietly die
    with nothing noticing: it was never in a snapshot, so a snapshot-only
    sweep could never learn it. That is how two of the owner's eight picks
    sat in the module as live candidates weeks after their postings came
    down. A full board owns its whole namespace, so absence there IS the
    answer for every one of them."""
    try:
        from . import applications
        uids = {r["uid"] for r in applications.load_universe()}
        uids |= {r["uid"] for r in applications.load_roles()[0]}
        return uids
    except Exception:  # noqa: BLE001 — a broken catalog never stops a poll
        return set()


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
    st_boards = dict(state.get("boards") or {})
    catalog = _catalog_uids()
    now = _now()
    new_uids, closed_uids = [], []
    # A board's FIRST-EVER sweep is a baseline, never news: registering a
    # company means discovering its whole board at once (Anthropic and
    # OpenAI together are ~1,150 roles), and announcing that as "new jobs"
    # would bury the one ping that matters. Same rule that protected the
    # original load; it just never covered later registrations.
    #
    # `board_meta` is read alongside the state so an install upgrading into
    # this rule does not baseline its whole registry on the first poll —
    # the prior snapshot already records every board ever swept, and
    # suppressing that cycle's genuinely new roles would be a silent miss.
    swept = set(st_boards) | set(board_meta)
    baseline_boards = {bid for bid in (_board_id(b) for b in reg["boards"])
                       if bid not in swept}

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
                if bid in baseline_boards:
                    st_roles[uid]["notified"] = "baseline"
                else:
                    new_uids.append(uid)
            else:
                # A catalog-closing sweep mints state entries carrying ONLY
                # {closed: ts} — a corpus role no board had ever served.
                # When such a role then appears in a fetch, the entry has
                # no first_seen, and reading it crashed EVERY sweep from
                # 2026-08-02 to 08-05: poll_once died mid-loop, the
                # snapshot was never written, and the boards feed sat
                # silently three days stale behind an "error: 'first_seen'"
                # status line nothing surfaced. Its first board sighting
                # is now — which is also the honest date.
                prior.setdefault("first_seen", now)
                prior["last_seen"] = now
                prior.pop("closed", None)
            rec["first_seen"] = st_roles[uid]["first_seen"]
            # baseline roles (the initial load) are never "NEW" in the UI
            if st_roles[uid].get("notified") == "baseline":
                rec["baseline"] = True
            rec.pop("closed", None)
            roles[uid] = rec
        # Anything this board owns that did not come back is closed — kept,
        # never deleted, because the analysis on top of it is the expensive
        # part. A full board owns its whole uid namespace (so a role it has
        # never served, carried in from a corpus, still gets checked); a
        # query board can only speak for roles it has served before.
        if b.get("ats") in FULL_BOARD:
            prefix = jobshared.uid_prefix(b["ats"], b.get("slug") or "")
            owned = [u for u in set(roles) | set(st_roles) | catalog
                     if u.startswith(prefix)]
        else:
            owned = [u for u, r in roles.items() if r.get("board") == bid]
        for uid in owned:
            if uid in fetched_uids:
                continue
            rec = roles.get(uid)
            if (rec or {}).get("closed") or (st_roles.get(uid) or {}).get("closed"):
                continue
            if rec is not None:
                rec["closed"] = now
            st_roles.setdefault(uid, {})["closed"] = now
            closed_uids.append(uid)
        st_boards[bid] = {"last_sweep": now, "ats": b.get("ats"),
                          "company": b.get("company"),
                          "full_board": b.get("ats") in FULL_BOARD,
                          "prefix": jobshared.uid_prefix(
                              b["ats"], b.get("slug") or "")}
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
        # the state file carries more than this function's two keys (the
        # auto-score record below) — re-read at write time so a wholesale
        # write cannot drop what another writer added mid-poll
        cur = _read_json(_state_path(), {})
        out_state = {"roles": st_roles, "boards": st_boards}
        if cur.get("score") is not None:
            out_state["score"] = cur["score"]
        _write_json(_state_path(), out_state)
    return {"ok": True, "at": now, "boards": board_meta,
            "total": len([r for r in roles.values() if not r.get("closed")]),
            "new": len(new_uids), "eligible_new": len(eligible_new),
            "closed": len(closed_uids), "notified": notified,
            "baselined": sorted(baseline_boards)}


def _notify_new(eligible_new, st_roles):
    """One batched iMessage per poll cycle; per-uid dedupe in state."""
    from . import notify
    fresh = [r for r in eligible_new
             if not (st_roles.get(r["uid"]) or {}).get("notified")]
    if not fresh:
        return 0
    parts = []
    # The ping's location label follows the owner's OWN rule: a role that
    # matched a configured place is named by that place, anything else that
    # got here is remote. With no places configured the label would be
    # noise, so it is left off entirely.
    rule = location_rule()
    for r in fresh[:NOTIFY_TITLES]:
        locs = r.get("locations") or []
        hit = ""
        if rule["places"] is not None:
            for x in locs:
                m = rule["places"].search(x)
                if m:
                    hit = m.group(0)
                    break
            if not hit:
                hit = "Remote"
        parts.append(f"{r['company']}: {r['title']}"
                     + (f" ({hit})" if hit else ""))
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


# ------------------------------------------------------------ availability

def availability_map():
    """uid -> {state: open|gone|unverified, checked|since}. The boards
    STATE is the authority here, not the snapshot: a sweep can close a
    role the snapshot never held (a catalog role carried in from a frozen
    corpus), and that verdict has to survive somewhere the catalog reads.

    `open` means A SWEEP CONFIRMED IT RECENTLY — within VERIFY_DAYS — not
    merely that some sweep saw it once. A stale last_seen degrades to
    `unverified` carrying its date, which is the difference between "this
    is up" and "this was up when something last looked". The case that
    forced it: a manual board's roles are written to state once, by hand,
    and never refetched, so they would have read as live indefinitely.
    The same rule covers a board that has been erroring for days and a
    poller that has been down.

    A uid absent from the map is `unverified` too — no registered board
    has ever seen it. Never infer `gone` from absence."""
    state = _read_json(_state_path(), {})
    cutoff = time.time() - VERIFY_DAYS * 86400
    out = {}
    for uid, st in (state.get("roles") or {}).items():
        if st.get("closed"):
            out[uid] = {"state": "gone", "since": st["closed"]}
            continue
        seen = st.get("last_seen")
        if not seen:
            continue
        try:
            fresh = datetime.fromisoformat(seen).timestamp() >= cutoff
        except ValueError:
            fresh = False
        out[uid] = {"state": "open" if fresh else "unverified",
                    "checked": seen}
    return out


def arm_if_stale(poller=None):
    """Ask the background poller for a sweep when the snapshot has gone
    stale — what opening the Applications module calls.

    Deliberately NOT a synchronous fetch: ten boards take the better part
    of a minute, and blocking the module's first paint on that would make
    opening it feel broken. The module paints from the last sweep and the
    rows correct themselves when this one lands. Honest on a passive
    instance, which runs no poller at all: nothing is armed and the caller
    is told so rather than waiting for a sweep that will never come."""
    fetched = (_read_json(_snapshot_path(), {}) or {}).get("fetched") or ""
    minutes = float(settings.raw().get("boards_poll_minutes") or 15)
    stale = True
    if fetched:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(fetched)).total_seconds()
            stale = age > minutes * 60
        except ValueError:
            pass
    running = bool(poller is not None and poller.is_alive())
    if stale and running:
        poller.poll_now()
    return {"stale": stale, "armed": bool(stale and running),
            "running": running, "fetched": fetched}


def snapshot():
    """The boards snapshot as read from disk — the public accessor for
    modules that need to know which uids a board is currently serving
    (jobscores.known_uids validates a proposed score against it)."""
    return _read_json(_snapshot_path(), {})


def state_mtime():
    """Mtime of the boards state, for cache keys in the catalog — without
    it a poll closing a role would not invalidate the universe cache and
    the module would keep serving the role as live until a role file or
    score file happened to change."""
    try:
        return _state_path().stat().st_mtime
    except OSError:
        return None


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
    sc = state.get("score") or {}
    return {
        "registered": len(reg["boards"]),
        "boards": snapshot.get("boards") or {},
        "fetched": snapshot.get("fetched") or "",
        "roles_open": len(open_roles),
        "eligible": sum(1 for r in open_roles.values()
                        if r.get("eligible") and not r.get("cut")),
        "fresh": fresh,
        "unscored_eligible": unscored,
        "auto_score": auto_score_enabled(),
        "scoring": ({"job": sc.get("job"), "roles": sc.get("roles"),
                     "at": sc.get("at"), "live": _score_job_live(sc),
                     "kind": sc.get("kind") or "board-score"}
                    if sc.get("job") else None),
        **_score_freshness(),
        **_rescore_status(),
    }


def _rescore_status():
    """How much of the stale backlog is still queued, and whether the drain
    is on. `jobrescore.status` is the one implementation — reported here so
    the boards strip states the queue in the same sentence as the staleness
    count it follows from."""
    from . import jobrescore
    return jobrescore.status()


def _score_freshness():
    """How much of the analysis was written against the CURRENT canon.

    The owner's question that made this worth reporting: the canon and the
    adjudication keep moving, so a score's age is not a curiosity — it is
    whether the "why" on a role still reflects what he now says about
    himself. Reported rather than acted on: nothing here rescores anything.
    """
    from . import applications, jobscores
    st = jobscores.status(applications.universe_dir())
    return {k: st[k] for k in ("canon_at", "scored_total", "scores_stale",
                               "scores_current", "scores_unstamped")}


def _scored_uids():
    from . import applications
    return set(jobshared.load_scores(applications.universe_dir()))


# --------------------------------------------------------- score dispatch

SCORE_SHAPE = ("uid, fit (0-100), screen (0-100), tier, final_tier, lane, "
               "why_fit, lead_with, caveat, comp_note, verdict")


def score_prompt(limit=40):
    """The prompt a 'Score new roles' dispatch hands an agent session
    (cwd = the self-record, so its CLAUDE.md claim gate loads when there
    is one). The session deep-reads the unscored eligible roles in the
    boards snapshot and extends the candidate universe.

    Every reference below is DERIVED, not hardcoded: an install that has
    accumulated a standing ruling and prior score files gets told to
    honor them, and a fresh one — which has neither — gets the shape
    described inline instead of being pointed at files that only ever
    existed on one Mac."""
    from . import applications
    udir = applications.universe_dir()
    snapshot = _read_json(_snapshot_path(), {})
    scored = _scored_uids()
    todo = [r for r in (snapshot.get("roles") or {}).values()
            if r.get("eligible") and not r.get("cut")
            and not r.get("closed") and r["uid"] not in scored]
    todo.sort(key=lambda r: r.get("first_seen") or "", reverse=True)
    todo = todo[:limit]

    # What this install actually has to honor.
    ruling = sorted(udir.glob("*owner-adjudication*.md"))
    prior = sorted(udir.glob("*-raw-scores.json"))
    facts = applications.self_record() / "canon" / "MASTER_HISTORY.md"

    step = 1

    def nxt(text):
        nonlocal step
        line = f"{step}. {text}"
        step += 1
        return line

    lines = ["Score the NEW job-board roles into the candidate universe.",
             ""]
    lines.append(nxt(f"Read the boards snapshot at {_snapshot_path()} — "
                     "the roles to score are listed below by uid."))
    if ruling:
        lines.append(nxt(
            f"Read {ruling[-1]} (the owner's standing ruling) and honor "
            "it — picks stay picked, cuts stay cut."))
    lines.append(nxt(
        "Score with the TWO-SCORE discipline: narrative resonance AND "
        "screening probability, separately. A hard minimum in a JD is a "
        "probability screen, not something to reframe away."))
    if facts.exists():
        lines.append(nxt(
            f"Every claim about the owner's background passes the "
            f"{facts.name} gate — read {facts} first and assert nothing "
            "it does not support."))
    lines.append(nxt(
        f"For each role write a role file at {udir}/candidate-universe/"
        "role/<uid>.json (uid, company, title, team, function, locations, "
        "seniority, salaryMin, salaryMax, comp, url, tags, blurb)."))
    lines.append(nxt(
        "File every score with ONE call to mcp__vira__record_role_scores — "
        f"scores_json is a JSON array of objects: {SCORE_SHAPE}. Do NOT "
        "write or edit a score file by hand: the server validates each "
        "entry and stamps when it was written and which canon it was "
        "written against, and a score that supplied its own provenance "
        "would defeat the staleness report those stamps feed. A refused "
        "entry names its reason — fix and re-file only that one."))
    if prior:
        lines.append(nxt(
            "Leave owner-adjudication.json and the older *-raw-scores.json "
            "files alone — they are the owner's own calls and the record of "
            "prior passes."))
    lines += ["", f"ROLES TO SCORE ({len(todo)}):"]
    for r in todo:
        lines.append(json.dumps(
            {k: r.get(k) for k in ("uid", "company", "title", "locations",
                                   "salaryMin", "salaryMax", "comp", "url")},
            ensure_ascii=False))
    return "\n".join(lines), len(todo)


# ---------------------------------------------------------- auto-scoring

# The owner's ruling (2026-08-05): no Score-new button. A new eligible role
# gets scored as it arrives, and the standing backlog drains batch by batch
# behind the same gate — one dispatch in flight at a time, so the spend is
# serialized, and score_prompt's per-batch cap (40) bounds each session.

SCORE_STALE_HOURS = 3   # a score job silent this long no longer blocks

# Floor between dispatches, live or dead. A failing ACCOUNT (the monthly
# spend limit, hit 2026-08-05 within an hour of auto-scoring going live)
# still probes green — `claude auth status` says logged in — so every
# launch dies in seconds and without a floor the poller mints a dead
# ledger job every few minutes, ~500 a day. A healthy batch takes longer
# than this anyway, so the happy path never waits.
SCORE_GAP_MIN = 20


def auto_score_enabled():
    return settings.raw().get("boards_auto_score", True) is not False


def _score_job_live(sc):
    """Whether the recorded score dispatch is still running. Asks the live
    session registry — the poller runs inside the server process, and the
    supervisor re-attaches detached runners across restarts, so a running
    job answers here even after a bounce. The age cap is the backstop: a
    runner that died without finalizing must not wedge auto-scoring."""
    jid = (sc or {}).get("job")
    if not jid:
        return False
    try:
        started = datetime.fromisoformat(sc.get("at") or "")
        age = (datetime.now(timezone.utc) - started).total_seconds()
        if age > SCORE_STALE_HOURS * 3600:
            return False
    except (ValueError, TypeError):
        pass
    try:
        from . import session
        snap = session.sessions.get(jid)
    except Exception:  # noqa: BLE001 — a broken registry never blocks
        return False
    return bool(snap) and snap.get("status") == "running"


def _record_score(entry):
    with _lock:
        state = _read_json(_state_path(), {})
        if entry is None:
            if state.pop("score", None) is None:
                return
        else:
            state["score"] = entry
        _write_json(_state_path(), state)


def maybe_auto_score():
    """Dispatch the scoring session for unscored eligible roles, unasked.

    Guards, in cost order: the config switch (`boards_auto_score`), one
    dispatch in flight at a time, the AI-ready probe (a machine with no
    model connected must not mint dead jobs — the routines rule), then
    the batch itself. The poller never runs under VIRA_PASSIVE, so a test
    clone never dispatches. Returns a small dict saying what happened;
    callers log it, nothing raises."""
    if not auto_score_enabled():
        return {"ok": False, "reason": "disabled"}
    sc = (_read_json(_state_path(), {}).get("score")) or {}
    if _score_job_live(sc):
        return {"ok": False, "reason": "in flight", "job": sc.get("job")}
    if sc.get("at"):
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(sc["at"])).total_seconds()
            if age < SCORE_GAP_MIN * 60:
                return {"ok": False,
                        "reason": "cooling down since the last dispatch"}
        except (ValueError, TypeError):
            pass
    from . import routines
    if not routines._ai_ready():
        return {"ok": False, "reason": "no AI connected"}
    prompt, n = score_prompt()
    kind = "board-score"
    if not n:
        # SECOND PHASE: nothing is unscored, so drain the stale backlog.
        # Unscored roles come first on purpose — the owner cannot act on a
        # role with no analysis at all, while a stale one at least says
        # something. Both phases share ONE dispatch record, so the
        # in-flight check and the 20-minute floor above cover them
        # together; a second record would let two sessions run at once,
        # which is exactly what that floor exists to prevent.
        from . import jobrescore
        if jobrescore.auto_rescore_enabled():
            prompt, n = jobrescore.batch_prompt()
            kind = "board-rescore"
    if not n:
        if sc:
            _record_score(None)     # done — clear the finished record
        return {"ok": False, "reason": "nothing to score or rescore"}
    try:
        from . import applications, session
        jid = session.sessions.launch(
            prompt, cwd=str(applications.self_record()),
            model=settings.raw().get("boards_score_model") or None,
            meta={"kind": kind, "machine": True})
    except ValueError as e:      # live-session cap — retry next tick
        return {"ok": False, "reason": str(e)[:160]}
    _record_score({"job": jid, "at": _now(), "roles": n, "kind": kind})
    return {"ok": True, "job": jid, "roles": n, "kind": kind}


# ------------------------------------------------------------------ poller

class Poller(threading.Thread):
    """Background poll loop — ticks every minute, polls every
    `boards_poll_minutes` (default 15). Dormant until boards are
    registered. Started from main._startup, skipped under VIRA_PASSIVE
    like every worker."""

    SCORE_CHECK_S = 180   # auto-score attempt cadence between polls

    def __init__(self):
        super().__init__(daemon=True, name="vira-jobboards")
        self.status = "starting"
        self.next_poll = time.time() + 90     # settle after boot
        self.next_score = time.time() + 120
        self.score_note = ""

    def poll_now(self):
        self.next_poll = 0.0

    def run(self):
        while True:
            try:
                if not load_registry()["boards"]:
                    self.status = "dormant — no boards registered"
                else:
                    if time.time() >= self.next_poll:
                        r = poll_once()
                        minutes = float(settings.raw()
                                        .get("boards_poll_minutes") or 15)
                        self.next_poll = time.time() + minutes * 60
                        self.status = (f"ok — {r.get('new', 0)} new / "
                                       f"{r.get('closed', 0)} closed at "
                                       f"{datetime.now().strftime('%H:%M')}")
                        self.next_score = 0.0   # new sweep — check at once
                    if time.time() >= self.next_score:
                        sc = maybe_auto_score()
                        self.next_score = time.time() + self.SCORE_CHECK_S
                        self.score_note = (
                            f"scoring {sc['roles']} (job {sc['job']})"
                            if sc.get("ok") else sc.get("reason", ""))
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
