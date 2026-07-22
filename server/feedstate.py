"""Per-item feed state: read/unread and hidden, persisted server-side so the
phone and the desktop see the same state. Keys are the feed item rowids
(chat.db ROWID ints for iMessage, "mail-<account>-<uid>" strings for email),
stored as strings. Pruned so the file never grows unbounded.

Storage rides the shared jsonstore discipline: fresh read per op (no
module cache), file-locked mutations, atomic writes.
"""
import time
from pathlib import Path

from . import jsonstore

STATE = Path(__file__).resolve().parent.parent / "data" / "feed-state.json"
MAX_KEYS = 4000


# NB: default dicts are passed as fresh literals per call — jsonstore.read
# returns the default object itself on a missing file, and fn mutates it.
def _norm(s):
    s.setdefault("read", {})
    s.setdefault("hidden", {})
    return s


def annotate(items):
    """Stamp read/hidden flags onto feed items in place."""
    s = _norm(jsonstore.read(STATE, {"read": {}, "hidden": {}}))
    read, hidden = s["read"], s["hidden"]
    for it in items:
        k = str(it.get("rowid"))
        it["read"] = k in read
        it["hidden"] = k in hidden
    return items


def set_state(rowid, read=None, hidden=None):
    k = str(rowid)

    def fn(s):
        _norm(s)
        now = time.time()
        if read is True:
            s["read"][k] = now
        elif read is False:
            s["read"].pop(k, None)
        if hidden is True:
            s["hidden"][k] = now
        elif hidden is False:
            s["hidden"].pop(k, None)
        jsonstore.prune_oldest(s["read"], MAX_KEYS)
        jsonstore.prune_oldest(s["hidden"], MAX_KEYS)

    s = jsonstore.mutate(STATE, fn, {"read": {}, "hidden": {}})
    return {"rowid": k, "read": k in s["read"], "hidden": k in s["hidden"]}


def read_all(rowids):
    def fn(s):
        _norm(s)
        now = time.time()
        for r in rowids:
            s["read"][str(r)] = now
        jsonstore.prune_oldest(s["read"], MAX_KEYS)

    s = jsonstore.mutate(STATE, fn, {"read": {}, "hidden": {}})
    return {"read_count": len(s["read"])}
