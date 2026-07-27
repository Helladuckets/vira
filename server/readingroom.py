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
    d["people"] = _text(raw.get("people"), "definition.people", cap=400)
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
    if any(d.get(k) for k in ("subject", "why", "people", "notes")):
        lines += ["", "STANDING INSTRUCTIONS (owner-edited; these govern "
                      "what belongs and how it ranks):"]
        for label, key in (("Subject", "subject"),
                           ("What the owner wants out of it", "why"),
                           ("Prioritize these people", "people"),
                           ("Notes", "notes")):
            if d.get(key):
                lines.append(f"- {label}: {d[key]}")
        if d.get("modes"):
            lines.append("- Include: " + ", ".join(d["modes"]))
        if d.get("depth"):
            lines.append(f"- Depth: {d['depth']}")
    lines += [
        "",
        "Do this, in order:",
        f"1. Read the current room store at {path} — its `items` array is "
        "the room. EVERY existing item must carry forward into your "
        "rebuild — item ids are derived from URLs, and a dropped item "
        "orphans the owner's done-marks.",
        f"2. Research what is new on this subject since {since}: new talks, "
        "papers, posts, podcast episodes, interviews. Real URLs only — never "
        "invent an item you cannot link. If genuinely nothing new exists, "
        "say so and stop; do not pad.",
        "3. Merge: existing items unchanged (keep their titles, URLs, dates, "
        "modes, priorities and notes exactly), new items appended with "
        "honest mode/prio/why fields.",
        f"4. Rebuild via the mcp__vira__create_reading_room tool with the SAME "
        f"slug \"{slug}\" — the server keeps ids stable so progress survives, "
        "and it notifies the owner of what arrived on its own.",
        "5. Report what you added, in one short list.",
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
