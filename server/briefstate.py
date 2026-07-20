"""Dismissed-row state for the Daily Brief.

The brief's Waiting-on-you and Going-quiet sections are DERIVED live from
chat.db and the CRM activity snapshot — there is nothing to "close" in a
source store, so clearing a row from the brief needs its own memory. Keys
are self-re-arming: they embed the state that produced the row (the
message timestamp, the last-contact date), so dismissing a "waiting 3h"
row hides that specific wait — a NEW message from that person mints a new
key and the row comes back. Open loops are NOT dismissed here; closing a loop writes
real state back to the CRM profile (see data.update_loop).

Same cross-process discipline as the other JSON stores: fresh read per op,
fcntl-locked mutations, atomic tmp+rename writes.
"""
import json
import time
from pathlib import Path

from .filelock import locked

STORE = Path(__file__).resolve().parent.parent / "data" / "brief-state.json"
MAX_KEYS = 500  # plenty; keys age out naturally as their rows stop deriving


def _load():
    try:
        s = json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        s = {}
    if not isinstance(s, dict):
        s = {}
    s.setdefault("dismissed", {})
    return s


def _save(s):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_name(STORE.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
    tmp.replace(STORE)


def dismiss(key):
    if not key or not isinstance(key, str):
        raise ValueError("key required")
    with locked(STORE):
        s = _load()
        s["dismissed"][key[:300]] = int(time.time())
        if len(s["dismissed"]) > MAX_KEYS:  # oldest first
            keep = sorted(s["dismissed"].items(), key=lambda kv: kv[1])
            s["dismissed"] = dict(keep[-MAX_KEYS:])
        _save(s)


def restore(key):
    with locked(STORE):
        s = _load()
        if s["dismissed"].pop(key, None) is not None:
            _save(s)


def dismissed_keys():
    return set(_load()["dismissed"])
