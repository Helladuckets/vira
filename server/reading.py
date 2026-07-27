"""Done-marks for reading-room pages (static/reading/*).

Reading-room pages are personal-layer static pages (never committed) that
render consumption queues — lists of things to watch/listen/read. Their
done-marks must follow the owner across devices, so they live server-side
instead of per-browser localStorage: one JSON store per list name under
data/reading/. The page GETs the authoritative set on load, POSTs each
toggle, and may bulk-merge a legacy localStorage set once on migration.

Same cross-process discipline as the other JSON stores: fresh read per
op, fcntl-locked mutations, atomic tmp+rename writes.
"""
import json
import re
import time
from pathlib import Path

from .filelock import locked

STORE_DIR = Path(__file__).resolve().parent.parent / "data" / "reading"
PAGES_DIR = Path(__file__).resolve().parent.parent / "static" / "reading"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
SUBTITLE_RE = re.compile(r"<header>.*?<p>(.*?)</p>", re.I | re.S)
BUILT_RE = re.compile(r"Built (\d{4}-\d{2}-\d{2})")
# Two generations of room page embed their items differently: the generator
# (server/readingroom.py) writes `window.DATA=[...]`, the pre-generator
# hand-built room writes `const DATA = [...]`. Both are one JSON array, so the
# marker just locates the `[` and json handles the rest.
DATA_RE = re.compile(r"(?:window\.DATA\s*=|const DATA\s*=)\s*(\[)")
MAX_KEYS = 20000  # far above any real list; guards a runaway client


def list_pages():
    """Every reading room: store-native ones first (the canonical kind since
    2026-07-27), then any legacy page still on disk that has not been
    migrated. The url stays /reading/<slug>.html for both — a native room's
    is served as an on-demand export from the store.

    The Reader launcher calls this to decide whether to show itself at all
    and what to list."""
    from . import readingroom               # lazy: readingroom imports us too
    pages = []
    seen = set()
    for r in readingroom.list_rooms():
        seen.add(r["slug"])
        pages.append({"name": r["slug"], "title": r["title"],
                      "url": f"/reading/{r['slug']}.html", "native": True})
    try:
        files = sorted(PAGES_DIR.glob("*.html"))
    except OSError:
        return pages
    for p in files:
        if p.stem in seen:
            continue
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            continue
        m = TITLE_RE.search(head)
        title = " ".join(m.group(1).split()) if m else p.stem
        pages.append({"name": p.stem, "title": title,
                      "url": f"/reading/{p.name}", "native": False})
    return pages


def _room_items(text):
    """The item list embedded in a room page, or None when it cannot be read.

    None (unparseable) and [] (genuinely empty) are different answers: a card
    facing an unparseable page should omit the count, not claim zero."""
    m = DATA_RE.search(text)
    if not m:
        return None
    try:
        items, _ = json.JSONDecoder().raw_decode(text, m.start(1))
    except (ValueError, TypeError):
        return None
    return items if isinstance(items, list) else None


def page_details():
    """list_pages() plus what a card needs: subtitle, item count, done count,
    and the date the room was last built/refreshed. A native room answers
    straight from its store; a legacy page is parsed best-effort — a field a
    page does not yield is simply absent from the row, never an exception."""
    from . import readingroom
    out = []
    for row in list_pages():
        if row.get("native"):
            room = readingroom.load_room(row["name"]) or {"items": []}
            row["subtitle"] = room.get("subtitle", "")
            row["built"] = (room.get("updated") or "")[:10] \
                or room.get("built", "")
            ids = {str(it.get("id")) for it in room["items"]
                   if isinstance(it, dict)}
            try:
                done = set(get_done(row["name"]))
            except ValueError:
                done = set()
            row["items"] = len(room["items"])
            row["done"] = len(done & ids) if ids else len(done)
            out.append(row)
            continue
        p = PAGES_DIR / f"{row['name']}.html"
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            out.append(row)
            continue
        m = SUBTITLE_RE.search(text[:4096])
        if m:
            row["subtitle"] = " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split())[:300]
        m = BUILT_RE.search(text)
        if m:
            row["built"] = m.group(1)
        items = _room_items(text)
        try:
            done = set(get_done(row["name"]))
        except ValueError:
            done = set()
        if items is not None:
            ids = {str(it.get("id")) for it in items if isinstance(it, dict)}
            row["items"] = len(items)
            # Intersect rather than trust the raw store: a rebuilt room may
            # have dropped entries whose marks would otherwise overcount.
            row["done"] = len(done & ids) if ids else len(done)
        else:
            row["done"] = len(done)
        out.append(row)
    return out


def _path(name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError("bad list name")
    return STORE_DIR / f"{name}.json"


def _load(path):
    try:
        s = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        s = {}
    if not isinstance(s, dict):
        s = {}
    s.setdefault("done", {})  # id -> epoch seconds marked
    return s


def _save(path, s):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
    tmp.replace(path)


def _clean_id(item_id):
    if not item_id or not isinstance(item_id, str):
        raise ValueError("id required")
    return item_id[:64]


def get_done(name):
    """The authoritative done-id list for a reading list (empty if none)."""
    s = _load(_path(name))
    return sorted(s["done"], key=lambda k: s["done"][k])


def set_done(name, item_id, done=True):
    """Mark or unmark one item; idempotent. Returns the updated id list."""
    item_id = _clean_id(item_id)
    path = _path(name)
    with locked(path):
        s = _load(path)
        if done:
            s["done"].setdefault(item_id, int(time.time()))
        else:
            s["done"].pop(item_id, None)
        if len(s["done"]) > MAX_KEYS:  # oldest first
            keep = sorted(s["done"].items(), key=lambda kv: kv[1])
            s["done"] = dict(keep[-MAX_KEYS:])
        _save(path, s)
        return sorted(s["done"], key=lambda k: s["done"][k])


def merge_done(name, item_ids):
    """One-shot union merge (legacy localStorage migration). Returns the list."""
    ids = [_clean_id(i) for i in item_ids if i]
    path = _path(name)
    with locked(path):
        s = _load(path)
        now = int(time.time())
        for i in ids:
            s["done"].setdefault(i, now)
        _save(path, s)
        return sorted(s["done"], key=lambda k: s["done"][k])
