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
MAX_KEYS = 20000  # far above any real list; guards a runaway client


def list_pages():
    """The personal reading-room pages that exist on disk (empty if none).

    The Reader launcher in the app calls this to decide whether to show
    itself at all and what to list. Titles come from each page's <title>."""
    pages = []
    try:
        files = sorted(PAGES_DIR.glob("*.html"))
    except OSError:
        return pages
    for p in files:
        try:
            head = p.read_text(errors="replace")[:4096]
        except OSError:
            continue
        m = TITLE_RE.search(head)
        title = " ".join(m.group(1).split()) if m else p.stem
        pages.append({"name": p.stem, "title": title, "url": f"/reading/{p.name}"})
    return pages


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
