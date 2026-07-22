"""Building a reading room — the validated write behind the Reader's
front door.

A room is a researched consumption queue: every worthwhile talk, paper,
post, and episode on one subject, ranked and deduplicated. The research
is a model's job; the FILE is not. This module is the server side of that
split — an agent session proposes a payload through the native
`create_reading_room` tool (server/viratools.py), and everything that
touches disk happens here, behind a schema.

Why the write is not the agent's: the room's file name keys its done-mark
store (data/reading/<slug>.json), the page is served by Vira to every
device the owner reads on, and a malformed one would be a broken page in
a list the owner cannot easily repair. Same discipline as
update_module_map — the agent proposes, the server validates and applies.

Stable ids are the other reason to centralize this. An item's id is
derived from its URL (or its title when it has none), so REBUILDING a
room — a wider repass, a fresh sweep months later — keeps every done-mark
the owner has earned. Ids the model invents would not survive that.

The generated page is deliberately thin: title, subtitle, its own items,
and two <script>/<link> references to the tracked generic assets
(static/reading-room.{css,js}). A style or behavior fix ships to every
room a user has ever built, rather than being frozen into each copy.
"""
import hashlib
import html
import json
import re
from datetime import date
from pathlib import Path

from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
PAGES_DIR = ROOT / "static" / "reading"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MODES = ("watch", "listen", "read")
STATUSES = ("MISSING", "PARTIAL", "HAVE")
PRIOS = ("P1", "P2", "P3")

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
        raise BuildError(f"{where}.date must be YYYY-MM-DD, got {d!r}")
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


def _shell(slug, title, subtitle, items, legacy_key=""):
    """The generated page. Structure mirrors what reading-room.js expects;
    everything cosmetic lives in the tracked stylesheet."""
    room = {"slug": slug}
    if legacy_key:
        room["legacyKey"] = legacy_key
    # </script> inside the JSON would close the tag early — the one escape
    # a JSON payload embedded in HTML actually needs.
    data = json.dumps(items, ensure_ascii=False).replace("</", "<\\/")
    room_json = json.dumps(room, ensure_ascii=False).replace("</", "<\\/")
    built = date.today().isoformat()
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
    <button class="chip have" data-k="status" data-v="HAVE">In the vault</button>
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


def build(slug, title, subtitle, items, legacy_key=""):
    """Validate a proposed room and write it. Returns a summary dict.

    Rebuilding an existing slug is deliberate and supported — a repass
    replaces the page while the done-mark store (keyed by the same slug,
    with ids stable across rebuilds) carries the owner's progress
    forward untouched."""
    slug = (slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise BuildError(
            "slug must be lowercase letters, digits and hyphens "
            f"(1-64 chars), got {slug!r}")
    title = _text(title, "title", cap=120, required=True)
    subtitle = _text(subtitle, "subtitle", cap=300)
    clean = clean_items(items)

    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    path = PAGES_DIR / f"{slug}.html"
    existed = path.exists()
    page = _shell(slug, title, subtitle, clean, legacy_key)
    # Lock beside the done-marks, not beside the page: static/reading/ is a
    # SERVED directory, and a stray <slug>.html.lock there would be handed
    # out over HTTP alongside the rooms.
    with locked(ROOT / "data" / "reading" / f"{slug}.build"):
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(page, encoding="utf-8")
        tmp.replace(path)

    by_mode = {}
    by_prio = {}
    for it in clean:
        by_mode[it["mode"]] = by_mode.get(it["mode"], 0) + 1
        by_prio[it["prio"]] = by_prio.get(it["prio"], 0) + 1
    return {
        "slug": slug, "title": title, "url": f"/reading/{slug}.html",
        "items": len(clean), "rebuilt": existed,
        "by_mode": by_mode, "by_prio": by_prio,
        "dropped": len(items) - len(clean),
    }


def summary_line(res):
    """One line for the tool result the model reads back."""
    modes = ", ".join(f"{n} to {m}" for m, n in sorted(res["by_mode"].items()))
    verb = "Rebuilt" if res["rebuilt"] else "Built"
    extra = f" ({res['dropped']} duplicates merged)" if res["dropped"] else ""
    return (f"{verb} reading room \"{res['title']}\" at {res['url']} — "
            f"{res['items']} items{extra}: {modes}. "
            f"P1={res['by_prio'].get('P1', 0)}, "
            f"P2={res['by_prio'].get('P2', 0)}, "
            f"P3={res['by_prio'].get('P3', 0)}. "
            "It is live in the Reader now.")
