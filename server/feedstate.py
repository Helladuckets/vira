"""Per-item feed state: read/unread and hidden, persisted server-side so the
phone and the desktop see the same state. Keys are the feed item rowids
(chat.db ROWID ints for iMessage, "mail-<account>-<uid>" strings for email),
stored as strings. Pruned so the file never grows unbounded.
"""
import json
import threading
import time
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "data" / "feed-state.json"
MAX_KEYS = 4000

_lock = threading.Lock()
_cache = None


def _load():
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(STATE.read_text())
        except (OSError, json.JSONDecodeError):
            _cache = {"read": {}, "hidden": {}}
    return _cache


def _save():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(_cache))


def _prune(bucket):
    if len(bucket) > MAX_KEYS:
        for k in sorted(bucket, key=bucket.get)[:len(bucket) - MAX_KEYS]:
            bucket.pop(k, None)


def annotate(items):
    """Stamp read/hidden flags onto feed items in place."""
    s = _load()
    read, hidden = s["read"], s["hidden"]
    for it in items:
        k = str(it.get("rowid"))
        it["read"] = k in read
        it["hidden"] = k in hidden
    return items


def set_state(rowid, read=None, hidden=None):
    with _lock:
        s = _load()
        k = str(rowid)
        now = time.time()
        if read is True:
            s["read"][k] = now
        elif read is False:
            s["read"].pop(k, None)
        if hidden is True:
            s["hidden"][k] = now
        elif hidden is False:
            s["hidden"].pop(k, None)
        _prune(s["read"])
        _prune(s["hidden"])
        _save()
    return {"rowid": k, "read": k in s["read"], "hidden": k in s["hidden"]}


def read_all(rowids):
    with _lock:
        s = _load()
        now = time.time()
        for r in rowids:
            s["read"][str(r)] = now
        _prune(s["read"])
        _save()
    return {"read_count": len(s["read"])}
