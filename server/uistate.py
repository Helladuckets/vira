"""Server-persisted UI state — the desktop window arrangement.

The desktop layout (floating-window positions/sizes/zoom/open state, dock
icon order) historically lived only in browser localStorage, which is
per-origin: a test instance on 127.0.0.1:83xx, the Tailscale hostname, or
a second browser all opened with default window placement, never the
owner's arrangement. This store mirrors the whitelisted localStorage keys
into data/ui-state.json — which rides the APFS data clone that
`branch.sh serve` makes — so a fresh origin adopts the live look at boot.

Sync model (client side, app.js syncUiState): LOCAL WINS on a browser
that already has its own saved layout — it keeps it and mirrors changes
up (so the store converges to the most recently used desktop browser,
i.e. the owner's). A fresh origin adopts the server copy at boot. No
timestamps, no merge: last writer wins, which is correct for a
single-owner arrangement.

Values are stored as the OPAQUE localStorage strings (validated to be
JSON) — no re-encoding seam between what the browser wrote and what it
reads back.
"""
import hashlib
import json
import threading
from pathlib import Path

from .filelock import locked

STORE = Path(__file__).resolve().parent.parent / "data" / "ui-state.json"
# Only the keys that define the desktop arrangement sync. Everything else
# in localStorage (sort choices, seen-flags) stays per-origin on purpose.
KEYS = ("vira-desktop", "vira-dock-order", "vira-dock-hidden")
MAX_VALUE_BYTES = 262144  # a runaway client never bloats the store

_lock = threading.Lock()


def _load():
    try:
        s = json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        s = {}
    keys = s.get("keys") if isinstance(s, dict) else None
    return {"keys": keys if isinstance(keys, dict) else {}}


def _save(s):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_name(STORE.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
    tmp.replace(STORE)


def instance_id():
    """Identity of THIS instance's data world: "live" for the primary
    instance, a per-clone stamp for test instances (branch.sh writes
    `date` output into data/.test-snapshot at clone time — a new clone
    mints a new id, a restart of the same instance keeps it, --fresh
    re-clones mint again). The client compares this against the id it
    last saw on this origin: test PORTS ARE RECYCLED, and a browser
    remembering a dead instance's layout must adopt the new instance's
    inherited arrangement instead of shadowing it and pushing the stale
    layout into the fresh store (the 2026-07-16 recycled-port clobber)."""
    for marker, prefix in ((".test-snapshot", "test-"),
                           (".instance-stamp", "inst-")):
        try:
            stamp = (STORE.parent / marker).read_text().strip()
        except OSError:
            continue
        if stamp:
            return prefix + hashlib.sha1(stamp.encode()).hexdigest()[:12]
    return "live"


def load():
    """The whole store: {"keys": {<name>: <raw localStorage string>}}."""
    with _lock:
        return _load()


def save(keys):
    """Merge whitelisted key/value pairs in; returns the updated store.

    Unknown keys are ignored (not an error — an older/newer client may
    know keys this server doesn't). Values must be JSON-parseable strings
    under the size cap; a bad value raises ValueError and nothing is
    written.
    """
    if not isinstance(keys, dict):
        raise ValueError("keys must be an object")
    accepted = {}
    for k, v in keys.items():
        if k not in KEYS:
            continue
        if not isinstance(v, str) or len(v.encode()) > MAX_VALUE_BYTES:
            raise ValueError(f"bad value for {k}")
        try:
            json.loads(v)
        except json.JSONDecodeError:
            raise ValueError(f"value for {k} is not JSON")
        accepted[k] = v
    with _lock, locked(STORE):
        s = _load()
        s["keys"].update(accepted)
        _save(s)
        return s
