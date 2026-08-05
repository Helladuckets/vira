"""Reading rooms — the validated write behind the Reader, store-native.

A room is a researched consumption queue: every worthwhile talk, paper,
post, and episode on one subject, ranked and deduplicated. The research
is a model's job; the DATA is not. An agent session proposes a payload
through the native `create_reading_room` tool (server/viratools.py), and
everything that touches disk happens here, behind a schema. Same
discipline as update_module_map — the agent proposes, the server
validates and applies.

THE STORE IS THE ROOM (owner's call, 2026-07-27). The first design made
each room a generated standalone HTML page, and every problem the owner
hit was that one fact wearing different costumes: an iframe document
inherits nothing from the app (the white-scrollbar bug, skins not
reaching rooms), items trapped in HTML are invisible to search and the
vault, counting them meant regexing a 200KB page, and updating meant
regenerating a file with no clean diff to notify on. So a room now lives
at data/reading/rooms/<slug>.json and the Reader renders it NATIVELY —
one implementation, wearing whatever skin the app wears. The standalone
page survives as an EXPORT (`export_html`, served on demand at
/reading/<slug>.html): shareable, readable outside Vira, never the
source of truth.

Stable ids are the other reason to centralize this. An item's id is
derived from its URL (or its title when it has none), so REBUILDING a
room — a wider repass, a fresh sweep months later — keeps every done-mark
the owner has earned (done-marks stay in reading.py's per-slug store,
keyed by the same slug). Ids the model invents would not survive that.

THE SKILL THAT BUILDS A ROOM is fully specified and topic-agnostic:
frontdoor's reader module carries the interview (subject / why / modes /
depth / people) and `frontdoor._reader_prompt` composes the research
method — vault-aware sweep, primary sources, honest P1-P3 ranking —
ending in one `create_reading_room` call. "Pop in another topic" is that
same interview, reachable from the Reader shelf's New-room card.
`update_prompt` is the standing-refresh half; the weekly room-scout
routine dispatches it for every room, and `build()` pings the owner when
a rebuild lands new items (the applications-module watch pattern).
"""
import hashlib
import html
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
PAGES_DIR = ROOT / "static" / "reading"     # legacy pages + the export target
ROOMS_DIR = ROOT / "data" / "reading" / "rooms"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# Month- and year-precision dates are real (a podcast page often says only
# "April 2025"); refusing them forced fabricated day-parts. String sort
# still orders partial dates correctly against full ones.
DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")

MODES = ("watch", "listen", "read")
STATUSES = ("MISSING", "PARTIAL", "HAVE")
PRIOS = ("P1", "P2", "P3")
DEPTHS = ("core", "thorough", "exhaustive")

MAX_ITEMS = 2000          # far above any real room; guards a runaway payload
MAX_TEXT = 1200           # per free-text field
MAX_PEOPLE = 24


class BuildError(ValueError):
    """Raised with a message written for the model that proposed the
    payload — it is handed straight back as the tool result, so it says
    what was wrong and what the field expects."""


def _text(v, field, cap=MAX_TEXT, required=False):
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise BuildError(f"{field} must be a string, got {type(v).__name__}")
    v = " ".join(v.split())
    if required and not v:
        raise BuildError(f"{field} is required and cannot be empty")
    return v[:cap]


def item_id(it):
    """Stable across rebuilds: the URL identifies the thing, and a room
    re-run months later must not orphan the owner's done-marks. Falls
    back to the title for items with no link."""
    basis = (it.get("url") or "").strip().lower() or _text(
        it.get("title"), "title").lower()
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]


def clean_item(raw, index):
    if not isinstance(raw, dict):
        raise BuildError(f"item {index} is not an object")
    where = f"item {index}"
    it = {}
    it["title"] = _text(raw.get("title"), f"{where}.title", required=True)
    it["url"] = _text(raw.get("url"), f"{where}.url", cap=600)
    if it["url"] and not it["url"].startswith(("http://", "https://")):
        raise BuildError(f"{where}.url must be http(s), got {it['url'][:60]!r}")

    d = _text(raw.get("date"), f"{where}.date", cap=10)
    if d and not DATE_RE.match(d):
        raise BuildError(f"{where}.date must be YYYY, YYYY-MM or "
                         f"YYYY-MM-DD, got {d!r}")
    it["date"] = d
    it["year"] = (raw.get("year") or (d[:4] if d else "")) or ""
    it["year"] = _text(it["year"], f"{where}.year", cap=4)

    for field, allowed, default in (("mode", MODES, "read"),
                                    ("status", STATUSES, "MISSING"),
                                    ("prio", PRIOS, "P2")):
        v = (raw.get(field) or default)
        if not isinstance(v, str) or v not in allowed:
            raise BuildError(
                f"{where}.{field} must be one of {'|'.join(allowed)}, "
                f"got {v!r}")
        it[field] = v

    people = raw.get("people") or []
    if isinstance(people, str):            # a model handing back "A, B"
        people = [p.strip() for p in people.split(",")]
    if not isinstance(people, list):
        raise BuildError(f"{where}.people must be a list of names")
    it["people"] = [_text(p, f"{where}.people[]", cap=80)
                    for p in people if str(p).strip()][:MAX_PEOPLE]

    it["type"] = _text(raw.get("type"), f"{where}.type", cap=40)
    it["venue"] = _text(raw.get("venue"), f"{where}.venue", cap=120)
    it["note"] = _text(raw.get("note"), f"{where}.note")
    it["why"] = _text(raw.get("why"), f"{where}.why")
    it["vault"] = _text(raw.get("vault"), f"{where}.vault", cap=300)
    it["pay"] = bool(raw.get("pay"))
    it["id"] = item_id(it)
    return it


def clean_items(items):
    if not isinstance(items, list):
        raise BuildError("items must be a list")
    if not items:
        raise BuildError("items is empty — a room needs at least one entry")
    if len(items) > MAX_ITEMS:
        raise BuildError(f"{len(items)} items exceeds the {MAX_ITEMS} cap")
    out, seen = [], {}
    for i, raw in enumerate(items):
        it = clean_item(raw, i)
        # Dedupe on the stable id: two sources naming the same talk are one
        # entry, and the richer record wins (more filled fields).
        prev = seen.get(it["id"])
        if prev is None:
            seen[it["id"]] = len(out)
            out.append(it)
        else:
            filled = sum(1 for v in it.values() if v not in ("", [], False))
            was = sum(1 for v in out[prev].values() if v not in ("", [], False))
            if filled > was:
                out[prev] = it
    return out


def _shell(slug, title, subtitle, items, legacy_key="", built=""):
    """The EXPORT page — a standalone, shareable projection of the store,
    rendered on demand. Structure mirrors what reading-room.js expects;
    everything cosmetic lives in the tracked stylesheet."""
    room = {"slug": slug}
    if legacy_key:
        room["legacyKey"] = legacy_key
    # </script> inside the JSON would close the tag early — the one escape
    # a JSON payload embedded in HTML actually needs.
    data = json.dumps(items, ensure_ascii=False).replace("</", "<\\/")
    room_json = json.dumps(room, ensure_ascii=False).replace("</", "<\\/")
    built = built or date.today().isoformat()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="/reading-room.css">
</head>
<body>
<header>
  <h1>{html.escape(title)}</h1>
  <p>{html.escape(subtitle)}</p>
</header>
<div class="bar"><div class="bar-inner">
  <input class="search" id="q" type="search" placeholder="Search titles, notes, people" autocomplete="off">
  <div class="chips" id="prioChips">
    <button class="chip p1" data-k="prio" data-v="P1">P1</button>
    <button class="chip p2" data-k="prio" data-v="P2">P2</button>
    <button class="chip p3" data-k="prio" data-v="P3">P3</button>
  </div>
  <div class="chips" id="statusChips">
    <button class="chip new" data-k="status" data-v="MISSING">Unseen</button>
    <button class="chip partial" data-k="status" data-v="PARTIAL">Secondhand</button>
  </div>
  <div class="chips" id="modeChips">
    <button class="chip" data-k="mode" data-v="watch">Watch</button>
    <button class="chip" data-k="mode" data-v="listen">Listen</button>
    <button class="chip" data-k="mode" data-v="read">Read</button>
  </div>
  <select id="person"><option value="">Anyone</option></select>
  <select id="year"><option value="">Any year</option></select>
  <select id="sort">
    <option value="prio">Priority</option>
    <option value="new">Newest</option>
    <option value="old">Oldest</option>
  </select>
  <button class="chip" id="hideDone" aria-pressed="false">Hide done</button>
  <button class="clear" id="clear">Reset</button>
  <span class="count" id="count"></span>
</div></div>
<main id="list"></main>
<footer>Built {built} by Vira. Personal layer - served locally, never committed.
Done-marks sync through Vira (data/reading/), shared across all your devices.</footer>
<script>window.ROOM={room_json};window.DATA={data};</script>
<script src="/reading-room.js"></script>
</body>
</html>
"""


def load_room(slug):
    """The stored room, or None. Never raises — a corrupt store reads as
    absent rather than taking the Reader down."""
    if not SLUG_RE.match(slug or ""):
        return None
    try:
        room = json.loads((ROOMS_DIR / f"{slug}.json")
                          .read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(room, dict) or not isinstance(room.get("items"), list):
        return None
    return room


def list_rooms():
    """Every stored room's summary (no items), oldest slug order."""
    out = []
    try:
        files = sorted(ROOMS_DIR.glob("*.json"))
    except OSError:
        return out
    for f in files:
        room = load_room(f.stem)
        if room:
            out.append({k: room.get(k) for k in
                        ("slug", "title", "subtitle", "built", "updated")}
                       | {"items": len(room["items"])})
    return out


MAX_PILLS = 24
SOURCE_KINDS = ("youtube", "rss")


def _clean_people(raw):
    """People as PILLS — {name, ref, qualifier}, the ref a vault-relative
    person page (wiki/cat-wu.md) so a refresh tracks THE Cat Wu who heads
    Claude Code product, not any name-match. The legacy comma string (older
    definitions, the interview's free-text answer) still parses: each name
    becomes an unresolved pill with an empty ref."""
    if raw is None:
        raw = []
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.split(",") if p.strip()]
    if not isinstance(raw, list):
        raise BuildError("definition.people must be a list or a comma string")
    out = []
    for i, p in enumerate(raw):
        if isinstance(p, str):
            p = {"name": p}
        if not isinstance(p, dict):
            raise BuildError(
                f"definition.people[{i}] must be an object or a name")
        name = _text(p.get("name"), f"definition.people[{i}].name",
                     cap=80, required=True)
        ref = _text(p.get("ref"), f"definition.people[{i}].ref", cap=200)
        if ref and (ref.startswith(("/", "http")) or ".." in ref):
            raise BuildError(
                f"definition.people[{i}].ref must be a vault-relative "
                f"note path, got {ref!r}")
        out.append({"name": name, "ref": ref,
                    "qualifier": _text(p.get("qualifier"),
                                       f"definition.people[{i}].qualifier",
                                       cap=200)})
    return out[:MAX_PILLS]


def _clean_sources(raw):
    """Publishing surfaces as FEEDS — each one enumerable deterministically
    on refresh (the whole point: keyword search alone missed known-channel
    items twice). label names it for the owner; feed is the URL the sweep
    fetches; kind youtube|rss decides the parse."""
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise BuildError("definition.sources must be a list")
    out = []
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            raise BuildError(f"definition.sources[{i}] must be an object")
        label = _text(s.get("label"), f"definition.sources[{i}].label",
                      cap=80, required=True)
        feed = _text(s.get("feed"), f"definition.sources[{i}].feed",
                     cap=400, required=True)
        if not feed.startswith(("http://", "https://")):
            raise BuildError(
                f"definition.sources[{i}].feed must be http(s), "
                f"got {feed[:60]!r}")
        kind = s.get("kind") or "rss"
        if kind not in SOURCE_KINDS:
            raise BuildError(
                f"definition.sources[{i}].kind must be one of "
                f"{'|'.join(SOURCE_KINDS)}, got {kind!r}")
        out.append({"label": label, "feed": feed, "kind": kind})
    return out[:MAX_PILLS]


def _clean_watch(raw):
    """The standing watch list — 'new Dario essays', 'Boris interviews' —
    split out of the notes blob so the refresh prompt can state it as its
    own contract instead of hoping the model finds it in prose."""
    if raw is None:
        raw = []
    if isinstance(raw, str):
        raw = [w.strip() for w in raw.split(",") if w.strip()]
    if not isinstance(raw, list):
        raise BuildError("definition.watch must be a list")
    return [_text(w, "definition.watch[]", cap=160)
            for w in raw if str(w).strip()][:MAX_PILLS]


def people_line(d):
    """The pills as the display/legacy comma string."""
    ppl = d.get("people") or []
    if isinstance(ppl, str):
        return ppl
    return ", ".join(p.get("name", "") for p in ppl if p.get("name"))


def clean_definition(raw):
    """The room's DEFINITION — the owner-visible spec of what this room
    tracks and why: subject, ranking rule, people, modes, depth, standing
    notes. It is what the Details reveal shows, what a refresh follows, and
    what a FORK starts from ('switch the company name to OpenAI'). Same
    field vocabulary as the front-door interview, deliberately — the
    definition IS the interview's answers, kept editable."""
    if not isinstance(raw, dict):
        raise BuildError("definition must be an object")
    d = {}
    d["subject"] = _text(raw.get("subject"), "definition.subject", cap=200)
    d["why"] = _text(raw.get("why"), "definition.why")
    d["people"] = _clean_people(raw.get("people"))
    d["sources"] = _clean_sources(raw.get("sources"))
    d["watch"] = _clean_watch(raw.get("watch"))
    modes = raw.get("modes") or []
    if isinstance(modes, str):
        modes = [m.strip() for m in modes.split(",") if m.strip()]
    if not isinstance(modes, list):
        raise BuildError("definition.modes must be a list")
    bad = [m for m in modes if m not in MODES]
    if bad:
        raise BuildError(
            f"definition.modes entries must be one of {'|'.join(MODES)}, "
            f"got {bad}")
    d["modes"] = modes
    depth = _text(raw.get("depth"), "definition.depth", cap=20)
    if depth and depth not in DEPTHS:
        raise BuildError(
            f"definition.depth must be one of {'|'.join(DEPTHS)}, "
            f"got {depth!r}")
    d["depth"] = depth
    d["notes"] = _text(raw.get("notes"), "definition.notes")
    return d


def set_meta(slug, title=None, subtitle=None):
    """Rename a room's title and/or the line under it. Safe by
    construction: the SLUG keys the done-mark store and the item ids, so a
    retitle orphans nothing — it flows to the switcher, the export page and
    the list on the next read. None keeps the existing value; an empty
    title is refused (a room must be nameable)."""
    if title is not None:
        title = _text(title, "title", cap=120, required=True)
    if subtitle is not None:
        subtitle = _text(subtitle, "subtitle", cap=300)
    path = ROOMS_DIR / f"{slug}.json"
    with locked(ROOT / "data" / "reading" / f"{slug}.build"):
        room = load_room(slug)
        if room is None:
            raise KeyError(slug)
        if title is not None:
            room["title"] = title
        if subtitle is not None:
            room["subtitle"] = subtitle
        room["updated"] = datetime.now(timezone.utc).astimezone() \
            .isoformat(timespec="seconds")
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(room, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(path)
    return {"title": room["title"], "subtitle": room.get("subtitle", "")}


def set_definition(slug, raw):
    """Validate and write a room's definition in place. Items untouched.
    Raises KeyError on an unknown room, BuildError on a bad payload."""
    d = clean_definition(raw)
    path = ROOMS_DIR / f"{slug}.json"
    with locked(ROOT / "data" / "reading" / f"{slug}.build"):
        room = load_room(slug)
        if room is None:
            raise KeyError(slug)
        room["definition"] = d
        room["updated"] = datetime.now(timezone.utc).astimezone() \
            .isoformat(timespec="seconds")
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(room, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(path)
    return d


def _ping_additions(slug, title, new_titles):
    """Best-effort owner ping when a rebuild lands new items — the
    applications-module watch pattern: the diff is the signal, one ping per
    batch, keyed so a retry of the same batch never re-pings. A failed or
    passive-blocked ping never fails the build."""
    try:
        from . import notify
        batch = hashlib.sha1("|".join(sorted(new_titles))
                             .encode("utf-8")).hexdigest()[:8]
        head = ", ".join(new_titles[:2])
        more = f" (+{len(new_titles) - 2} more)" if len(new_titles) > 2 else ""
        notify.agent_ping(
            f'Vira: reading room "{title}" has {len(new_titles)} new '
            f"item{'s' if len(new_titles) != 1 else ''} — {head}{more}",
            key=f"room-new:{slug}:{batch}")
    except Exception:  # noqa: BLE001 — a ping is never worth a lost room
        pass


def build(slug, title, subtitle, items, legacy_key=""):
    """Validate a proposed room and write THE STORE. Returns a summary dict.

    Rebuilding an existing slug is deliberate and supported — a repass
    replaces the item list while the done-mark store (keyed by the same
    slug, with ids stable across rebuilds) carries the owner's progress
    forward untouched. A rebuild that lands new items pings the owner."""
    slug = (slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise BuildError(
            "slug must be lowercase letters, digits and hyphens "
            f"(1-64 chars), got {slug!r}")
    title = _text(title, "title", cap=120, required=True)
    subtitle = _text(subtitle, "subtitle", cap=300)
    clean = clean_items(items)

    prev = load_room(slug)
    prev_ids = {it.get("id") for it in (prev or {}).get("items", [])}
    new_titles = [it["title"] for it in clean if it["id"] not in prev_ids] \
        if prev else []

    room = {
        "slug": slug, "title": title, "subtitle": subtitle,
        "built": (prev or {}).get("built") or date.today().isoformat(),
        "updated": datetime.now(timezone.utc).astimezone()
                   .isoformat(timespec="seconds"),
        "legacy_key": legacy_key or (prev or {}).get("legacy_key") or "",
        # A rebuild replaces the ITEMS; the owner's definition rides along.
        "definition": (prev or {}).get("definition") or {},
        "items": clean,
    }
    ROOMS_DIR.mkdir(parents=True, exist_ok=True)
    path = ROOMS_DIR / f"{slug}.json"
    with locked(ROOT / "data" / "reading" / f"{slug}.build"):
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(room, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(path)

    if prev and new_titles:
        _ping_additions(slug, title, new_titles)

    by_mode = {}
    by_prio = {}
    for it in clean:
        by_mode[it["mode"]] = by_mode.get(it["mode"], 0) + 1
        by_prio[it["prio"]] = by_prio.get(it["prio"], 0) + 1
    return {
        "slug": slug, "title": title, "url": f"/reading/{slug}.html",
        "items": len(clean), "rebuilt": bool(prev),
        "added": len(new_titles) if prev else 0,
        "by_mode": by_mode, "by_prio": by_prio,
        "dropped": len(items) - len(clean),
    }


# ------------------------------------------- sources: resolve + sweep ---
#
# The deterministic half of a refresh. A room's definition names its
# publishing surfaces as feeds; enumerating them and diffing against the
# item URLs is a set operation, not a judgment call — so it happens HERE,
# in code, and the model is handed the residual to verify and rank.
# Measured need, 2026-08-05: the 07-27 keyword sweep missed two items
# (Dianne Penn on Lenny's, the Boris YC keynote) sitting in channels the
# room already named in prose.

_YT_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})")
# YouTube spells the id three ways depending on which rendering the UA
# gets: "channelId":"UC…" / "externalId":"UC…" (JSON blobs) or the page
# head's RSS link, channel_id=UC… — measured 2026-08-05: the plain-UA
# page carries ONLY the third.
_YT_CHANNEL_RE = re.compile(
    r'(?:"channelId"\s*:\s*"|"externalId"\s*:\s*"|channel_id=)'
    r"(UC[0-9A-Za-z_-]{16,})")
_FEED_LINK_RE = re.compile(
    r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]*href="([^"]+)"',
    re.I)
FEED_TIMEOUT = 8
MAX_CANDIDATES = 60


def _http_get(url, timeout=FEED_TIMEOUT):
    """One small GET — module-level so tests stub it and nothing here ever
    spends real network in a suite."""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "vira-reader"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def resolve_source(text):
    """What the owner typed -> an enumerable {label, feed, kind}.

    Accepts a YouTube handle/channel URL (resolved ONCE, at save time, to
    the channel's RSS feed — the id is stable so the fetch never repeats),
    a direct feed URL, or a page URL advertising an RSS/atom alternate.
    Raises ValueError for anything else: a source that cannot be
    enumerated is prose and belongs in notes, not in this list."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty source")

    m = re.match(r"^@([\w.-]+)$", text)
    yt = re.search(r"youtube\.com/(@[\w.-]+|channel/(UC[0-9A-Za-z_-]{16,}))",
                   text)
    if m or yt:
        if yt and yt.group(2):
            cid, label = yt.group(2), yt.group(1)
        else:
            handle = ("@" + m.group(1)) if m else yt.group(1)
            page = _http_get(f"https://www.youtube.com/{handle}")
            found = _YT_CHANNEL_RE.search(page)
            if not found:
                raise ValueError(
                    f"could not resolve {handle} to a YouTube channel id")
            cid, label = found.group(1), handle
        return {"label": label, "kind": "youtube",
                "feed": "https://www.youtube.com/feeds/videos.xml"
                        f"?channel_id={cid}"}

    if not text.startswith(("http://", "https://")):
        raise ValueError(
            f"{text!r} is not a URL or @handle — free-text guidance "
            "belongs in the notes field")
    body = _http_get(text)
    stripped = body.lstrip()
    if stripped.startswith("<?xml") or "<rss" in stripped[:400] \
            or "<feed" in stripped[:400]:
        m2 = re.search(r"<title[^>]*>([^<]+)</title>", body)
        label = (m2.group(1).strip() if m2
                 else re.sub(r"^www\.", "",
                             re.sub(r"^https?://", "", text).split("/")[0]))
        return {"label": label[:80], "feed": text, "kind": "rss"}
    alt = _FEED_LINK_RE.search(body)
    if alt:
        from urllib.parse import urljoin
        feed = urljoin(text, alt.group(1))
        label = re.sub(r"^www\.", "",
                       re.sub(r"^https?://", "", text).split("/")[0])
        return {"label": label[:80], "feed": feed, "kind": "rss"}
    raise ValueError(f"no feed found at {text} — is there an RSS URL?")


def _parse_feed(xml_text):
    """Atom or RSS2 -> [{title, url, date}]. Namespace-blind on purpose:
    YouTube's feed is Atom with three namespaces, podcast feeds are RSS2
    with arbitrary extensions, and matching on local names covers both."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in ("entry", "item"):
            continue
        title = url = when = ""
        for c in node:
            ct = c.tag.rsplit("}", 1)[-1]
            if ct == "title":
                title = (c.text or "").strip()
            elif ct == "link":
                url = (c.get("href") or c.text or "").strip() or url
            elif ct in ("published", "pubDate", "updated") and not when:
                when = (c.text or "").strip()
        if title or url:
            out.append({"title": title, "url": url, "date": _feed_date(when)})
    return out


def _feed_date(raw):
    """Best-effort YYYY-MM-DD from Atom (ISO) or RSS2 (RFC 2822) stamps."""
    raw = (raw or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(raw).date().isoformat()
    except Exception:                                    # noqa: BLE001
        return ""


def _norm_url(url):
    return re.sub(r"^www\.", "",
                  re.sub(r"^https?://", "", (url or "").strip().lower())
                  ).rstrip("/")


def enumerate_sources(room):
    """Sweep every feed the room names; return what the feeds carry that
    the room does not. Never raises — a dead feed is a named error line,
    not a failed refresh. YouTube entries diff on the VIDEO ID (item URLs
    carry query-string variants); everything else on the normalized URL."""
    d = room.get("definition") or {}
    sources = d.get("sources") or []
    items = room.get("items") or []
    known_vids = set()
    known_urls = set()
    for it in items:
        u = it.get("url") or ""
        known_urls.add(_norm_url(u))
        mv = _YT_ID_RE.search(u)
        if mv:
            known_vids.add(mv.group(1))
    candidates, errors = [], []
    for s in sources:
        try:
            entries = _parse_feed(_http_get(s["feed"]))
        except Exception as e:                           # noqa: BLE001
            errors.append(f"{s['label']}: {type(e).__name__}: {e}")
            continue
        for e in entries:
            mv = _YT_ID_RE.search(e["url"])
            if mv and mv.group(1) in known_vids:
                continue
            if not mv and _norm_url(e["url"]) in known_urls:
                continue
            if len(candidates) < MAX_CANDIDATES:
                candidates.append({**e, "source": s["label"]})
    return {"candidates": candidates, "errors": errors,
            "swept": len(sources)}


def merge_items(slug, new_items):
    """Append/merge into an existing room WITHOUT re-emitting it — the
    answer to the single-string ceiling that blocked the 2026-08-03 scout
    write (350 items is ~70k tokens; no single model message carries it).

    Existing items ride through clean_items' own dedupe, so an incoming
    duplicate either vanishes or wins only by being the richer record;
    ids stay URL-derived so done-marks survive; the additions ping fires
    through the same path a rebuild uses. Raises KeyError on an unknown
    room, BuildError on a bad payload."""
    if not isinstance(new_items, list):
        raise BuildError("items must be a list")
    path = ROOMS_DIR / f"{slug}.json"
    with locked(ROOT / "data" / "reading" / f"{slug}.build"):
        room = load_room(slug)
        if room is None:
            raise KeyError(slug)
        prev_ids = {it.get("id") for it in room["items"]}
        merged = clean_items(list(room["items"]) + list(new_items))
        new_titles = [it["title"] for it in merged
                      if it["id"] not in prev_ids]
        room["items"] = merged
        room["updated"] = datetime.now(timezone.utc).astimezone() \
            .isoformat(timespec="seconds")
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(room, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(path)
    if new_titles:
        _ping_additions(slug, room["title"], new_titles)
    return {"slug": slug, "items": len(merged), "added": len(new_titles),
            "titles": new_titles}


def export_html(slug):
    """The standalone page, rendered from the store on demand — for the
    full-tab link and for sharing. Raises KeyError on an unknown room."""
    room = load_room(slug)
    if room is None:
        raise KeyError(slug)
    return _shell(slug, room["title"], room.get("subtitle", ""),
                  room["items"], room.get("legacy_key", ""),
                  built=room.get("built", ""))


def update_prompt(slug):
    """The dispatch prompt for refreshing an existing room in place.

    A room is a durable tracker over a standing subject, so 'update' means:
    carry every existing item forward (their ids are stable by URL — dropping
    one orphans the owner's done-marks), sweep for what is NEW since the room
    was last refreshed, and rebuild the same slug. The session reads the
    current items off the store file rather than having them inlined here — a
    real room can hold hundreds of items and the store is the truth anyway.

    Raises KeyError when no such room exists (the route turns that into 404)."""
    room = load_room(slug)
    if room is None:
        raise KeyError(slug)
    path = ROOMS_DIR / f"{slug}.json"
    since = (room.get("updated") or "")[:10] or room.get("built") \
        or "an unknown date"
    n = len(room["items"])
    lines = [
        f"You are refreshing the reading room \"{room['title']}\" so it stays "
        "current. A reading room is a researched consumption queue — every "
        "worthwhile talk, paper, post, interview and episode on one subject, "
        "ranked and deduplicated.",
        "",
        f"Subject, as the room states it: "
        f"{room.get('subtitle') or room['title']}",
        f"The room was last refreshed on {since} and holds {n} items.",
    ]
    d = room.get("definition") or {}
    if any(d.get(k) for k in ("subject", "why", "people", "notes", "watch")):
        lines += ["", "STANDING INSTRUCTIONS (owner-edited; these govern "
                      "what belongs and how it ranks):"]
        for label, key in (("Subject", "subject"),
                           ("What the owner wants out of it", "why")):
            if d.get(key):
                lines.append(f"- {label}: {d[key]}")
        ppl = d.get("people") or []
        if isinstance(ppl, str):                  # a pre-pill definition
            lines.append(f"- Prioritize these people: {ppl}")
        elif ppl:
            lines.append("- Prioritize these people — each resolved to an "
                         "identity, so track THAT person, not name-matches:")
            for p in ppl:
                bit = f"  - {p['name']}"
                if p.get("qualifier"):
                    bit += f" — {p['qualifier']}"
                if p.get("ref"):
                    bit += f" (identity page: {p['ref']})"
                lines.append(bit)
        if d.get("watch"):
            lines.append("- Standing watch — always check for these:")
            lines += [f"  - {w}" for w in d["watch"]]
        if d.get("notes"):
            lines.append(f"- Notes: {d['notes']}")
        if d.get("modes"):
            lines.append("- Include: " + ", ".join(d["modes"]))
        if d.get("depth"):
            lines.append(f"- Depth: {d['depth']}")

    # The deterministic half runs HERE, before any model judgment: the
    # room's feeds are enumerated and diffed against its items, and the
    # session is handed the residual as ground truth to verify and rank.
    swept = enumerate_sources(room)
    if swept["swept"]:
        cands, errors = swept["candidates"], swept["errors"]
        lines += ["", f"DETERMINISTIC SWEEP — the room's {swept['swept']} "
                  "source feeds were enumerated just now and diffed against "
                  "the room:"]
        if cands:
            lines.append(
                f"These {len(cands)} entries are in the feeds and NOT in "
                "the room. They come from the channels themselves, so the "
                "URLs are real; your job is judgment, not discovery — "
                "verify each is in scope, rank it against the room's "
                "'why', and include what belongs:")
            lines += [f"- {c['date'] or '????'} · {c['title'][:100]} "
                      f"({c['url']}) [from {c['source']}]" for c in cands]
        else:
            lines.append("Every feed entry is already in the room — the "
                         "feeds hold nothing new.")
        for err in errors:
            lines.append(f"- FEED ERROR, sweep this source by hand: {err}")
    lines += [
        "",
        "Do this, in order:",
        f"1. Read the current room store at {path} — its `items` array is "
        "the room; know what it covers so you never re-propose it.",
        f"2. Judge the deterministic-sweep candidates above (if any), then "
        f"research what ELSE is new on this subject since {since} — "
        "appearances outside the room's own feeds: conference talks, other "
        "podcasts, essays, papers. Real URLs only — never invent an item "
        "you cannot link. If genuinely nothing new exists, say so and "
        "stop; do not pad.",
        f"3. Add what belongs via the mcp__vira__add_reading_room_items "
        f"tool with slug \"{slug}\" — pass ONLY the new items; the server "
        "merges by stable id, keeps every existing item and its done-marks, "
        "and notifies the owner of what arrived. NEVER re-emit the whole "
        "room (create_reading_room is for restructuring, not refreshes).",
        "4. Report what you added, in one short list.",
    ]
    return "\n".join(lines)


def refresh_all_prompt():
    """One session, every room, the same contract each — composed at
    dispatch by the weekly room-scout routine. Empty string when there are
    no rooms (the routine treats that as a quiet no-op, not a failure)."""
    rooms = list_rooms()
    if not rooms:
        return ""
    parts = [
        f"You are Vira's weekly reading-room scout. {len(rooms)} room"
        f"{'s' if len(rooms) != 1 else ''} to refresh, each with the same "
        "contract. Work through them one at a time; a room with nothing new "
        "is a sentence in your report, not a failure.",
    ]
    for r in rooms:
        parts.append("\n" + "-" * 8 + "\n" + update_prompt(r["slug"]))
    return "\n".join(parts)


# ------------------------------------------------------------- migration ---

_LEGACY_KEY_RE = re.compile(r'LS_KEY\s*=\s*"([^"]+)"')


def migrate(slug):
    """Fold a legacy page-based room into the store, remapping done-marks.

    Pre-generator pages carry their own item ids (a different hash scheme),
    so a naive rebuild would orphan every mark the owner has earned —
    measured on the Anthropic room: 345 of 345 ids differ. Each marked old
    id is translated to the generator id of the SAME item before the store
    is written. The page file is renamed to .html.migrated (kept as a
    backup, invisible to globs) — the export route re-renders the same URL
    from the store."""
    from . import reading
    page = PAGES_DIR / f"{slug}.html"
    if load_room(slug):
        raise BuildError(f"{slug} is already store-native")
    try:
        text = page.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise KeyError(slug)
    items = reading._room_items(text)
    if not items:
        raise BuildError(f"no item data found in {page.name}")

    m = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
    title = " ".join(m.group(1).split()) if m else slug
    m = re.search(r"<header>.*?<p>(.*?)</p>", text[:4096], re.I | re.S)
    subtitle = " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split()) if m else ""
    m = _LEGACY_KEY_RE.search(text)
    legacy_key = m.group(1) if m else ""

    # Old id -> the item, so marks translate by identity of the THING.
    by_old = {str(it.get("id")): it for it in items if isinstance(it, dict)}
    old_done = [i for i in reading.get_done(slug) if i in by_old]

    res = build(slug, title, subtitle, items, legacy_key=legacy_key)

    remapped = 0
    for old_id in old_done:
        new_id = item_id(by_old[old_id])
        if new_id != old_id:
            reading.set_done(slug, new_id, True)
            remapped += 1
    page.rename(page.with_suffix(".html.migrated"))
    return {**res, "remapped_marks": remapped,
            "legacy_key": legacy_key, "backup": page.name + ".migrated"}


def summary_line(res):
    """One line for the tool result the model reads back."""
    modes = ", ".join(f"{n} to {m}" for m, n in sorted(res["by_mode"].items()))
    verb = "Rebuilt" if res["rebuilt"] else "Built"
    extra = f" ({res['dropped']} duplicates merged)" if res["dropped"] else ""
    new = f" {res['added']} new items;" if res.get("added") else ""
    return (f"{verb} reading room \"{res['title']}\" —{new} "
            f"{res['items']} items{extra}: {modes}. "
            f"P1={res['by_prio'].get('P1', 0)}, "
            f"P2={res['by_prio'].get('P2', 0)}, "
            f"P3={res['by_prio'].get('P3', 0)}. "
            "It is live in the Reader now.")


if __name__ == "__main__":  # pragma: no cover — thin CLI over the functions
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "migrate":
        print(json.dumps(migrate(sys.argv[2]), indent=1))
    elif len(sys.argv) >= 3 and sys.argv[1] == "export":
        print(export_html(sys.argv[2]))
    else:
        print("usage: python -m server.readingroom migrate|export <slug>")
