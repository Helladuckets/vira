"""Per-person send-channel preference: does Vira text this contact over
iMessage or plain SMS?

Kept in Vira's own data (not the CRM profile) so it is refresh-proof by
construction — a CRM profile re-synthesis can never drop it. Two ways an
entry lands:
  - "owner": the owner picked a channel on the person page (an explicit
    "this person is on Android / send them a text" mark);
  - "inferred": a send verified as failed on iMessage and Vira re-sent it
    as a text, remembering the lesson so the NEXT send goes straight to
    SMS (the proactive behavior the backlog item asked for).

Absence of an entry means "auto" — resolve_channel then reads chat.db to
decide. Storage rides the shared jsonstore discipline: fresh read per op,
file-locked mutations, atomic writes.
"""
import time
from pathlib import Path

from . import jsonstore

STATE = Path(__file__).resolve().parent.parent / "data" / "send-channels.json"
MAX_KEYS = 4000
CHANNELS = ("imessage", "sms")


def _norm(s):
    s.setdefault("channels", {})
    return s


def get(pid):
    """Return the stored entry for a person, or None for auto."""
    if not pid:
        return None
    s = _norm(jsonstore.read(STATE, {"channels": {}}))
    return s["channels"].get(pid)


def set_channel(pid, channel, source="owner"):
    """Record a channel preference. channel in {imessage, sms} sets it;
    channel=None (or "auto") clears the entry back to auto. An explicit
    owner mark always wins over a later inference; an inference never
    overwrites an owner mark."""
    if not pid:
        return None
    if channel not in CHANNELS and channel not in (None, "auto"):
        raise ValueError(f"unknown channel {channel!r}")

    def fn(s):
        _norm(s)
        if channel in (None, "auto"):
            s["channels"].pop(pid, None)
            return
        prev = s["channels"].get(pid)
        if prev and prev.get("source") == "owner" and source != "owner":
            return  # never let an inference clobber an owner mark
        s["channels"][pid] = {
            "channel": channel,
            "source": source,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        bucket = s["channels"]
        if len(bucket) > MAX_KEYS:
            oldest = sorted(bucket, key=lambda k: bucket[k].get("updated", ""))
            for k in oldest[:len(bucket) - MAX_KEYS]:
                bucket.pop(k, None)

    jsonstore.mutate(STATE, fn, {"channels": {}})
    return get(pid)
