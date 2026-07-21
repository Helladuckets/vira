"""Android companion link: pairing, message ingest, and phone pings.

The phone that is not an iPhone still has to reach Vira somehow. The
companion app (android/) pairs with this hub over the tailnet, uploads
SMS history and live SMS/RCS/WhatsApp notification captures in batches,
and long-polls for pings — no Google push, no cloud relay. Message
content lands here and STAYS here (data/companion.sqlite), the same
privacy boundary as chat.db: the model only ever sees it through the
existing suggest paths.

Pairing (one QR, one trust decision): the hub mints a device id + a
long-lived token, stores the token through the secrets ladder
(server/secrets.py — Keychain / Credential Manager / locked file), and
shows a QR carrying {url, device_id, token}. The app scans it, stores
the credentials, and sends them with every request (X-Vira-Device +
Authorization: Bearer). An unclaimed pairing expires in 15 minutes.

Dedupe: the same message can arrive twice — the SMS-history backfill
and the live notification capture see the same text with timestamps a
few seconds apart. Exact re-uploads are dropped on a unique key of
(sender, timestamp, text-hash); near-dupes are dropped when the same
sender+text lands within NEAR_DUPE_S of a stored copy.

Passive instances refuse every companion write (the send.py precedent):
a test clone must never mint tokens into the machine-wide Keychain or
swallow a real phone's upload into a disposable snapshot.
"""
import hashlib
import hmac
import json
import re
import secrets as pysecrets
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import data as crm
from . import secrets, settings

STORE = Path(__file__).resolve().parent.parent / "data" / "companion.json"
DB = Path(__file__).resolve().parent.parent / "data" / "companion.sqlite"

PAIR_TTL_S = 15 * 60          # unclaimed pairing lives this long
NEAR_DUPE_S = 120             # same sender+text within this window = dupe
FEED_FRESH_S = 48 * 3600      # only messages this recent enter the live feed
PING_KEEP = 100

_lock = threading.Lock()
_ping_event = threading.Event()


def keychain_service():
    return settings.keychain_service("vira-companion")


def assert_active():
    """The write gate, same shape as send.send_imessage's."""
    import os
    if os.environ.get("VIRA_PASSIVE"):
        raise RuntimeError(
            "passive test instance: companion pairing and ingest are blocked")


# ---------- device + ping store (data/companion.json) ----------

def _load():
    try:
        return json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"devices": [], "pings": [], "next_ping_id": 1}


def _save(data):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(STORE)


def _sweep_pending(data):
    """Expire unclaimed pairings and delete their ladder tokens."""
    now = time.time()
    keep, dropped = [], []
    for d in data["devices"]:
        if d.get("pending") and now - d.get("created_ts", 0) > PAIR_TTL_S:
            dropped.append(d["id"])
        else:
            keep.append(d)
    data["devices"] = keep
    for did in dropped:
        secrets.delete(keychain_service(), did)
    return data


def devices():
    """Paired devices for the owner UI — never includes tokens."""
    with _lock:
        data = _sweep_pending(_load())
        _save(data)
    return [{k: v for k, v in d.items() if k != "created_ts"}
            for d in data["devices"]]


def has_devices():
    return any(not d.get("pending") for d in _load()["devices"])


def pair_start(url=None):
    """Mint a pairing: device id + token (secrets ladder), return the QR
    payload. The token appears exactly once, in this response — after the
    claim it lives only in the ladder and on the phone."""
    assert_active()
    device_id = "cd_" + uuid.uuid4().hex[:12]
    token = pysecrets.token_urlsafe(32)
    secrets.set(keychain_service(), device_id, token)
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        data = _sweep_pending(_load())
        data["devices"].append({
            "id": device_id,
            "name": "",
            "platform": "",
            "pending": True,
            "created": now,
            "created_ts": time.time(),
        })
        _save(data)
    hub = url or hub_url()
    payload = {"v": 1, "kind": "vira-pair", "url": hub,
               "device_id": device_id, "token": token}
    return {"device_id": device_id, "token": token, "url": hub,
            "payload": json.dumps(payload),
            "expires_s": PAIR_TTL_S,
            "qr_svg": _qr_svg(json.dumps(payload))}


def pair_complete(device_id, token, name="", platform=""):
    """The phone claims its pairing. Validates the token against the
    ladder, flips pending off, stamps the device."""
    assert_active()
    if not _token_ok(device_id, token):
        raise PermissionError("unknown device or bad token")
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        data = _sweep_pending(_load())
        dev = next((d for d in data["devices"] if d["id"] == device_id), None)
        if dev is None:
            raise PermissionError("pairing expired — start again from the hub")
        dev.update(pending=False, name=str(name)[:80],
                   platform=str(platform)[:80], paired_at=now,
                   last_seen=now)
        _save(data)
    return {"ok": True, "device_id": device_id,
            "owner_name": settings.get("owner_name") or "",
            "hub": "Vira"}


def unpair(device_id):
    """Owner-side removal: forget the device and its ladder token."""
    assert_active()
    with _lock:
        data = _load()
        before = len(data["devices"])
        data["devices"] = [d for d in data["devices"] if d["id"] != device_id]
        _save(data)
    secrets.delete(keychain_service(), device_id)
    return {"removed": len(_load()["devices"]) < before}


def _token_ok(device_id, token):
    if not device_id or not token:
        return False
    if not re.fullmatch(r"cd_[0-9a-f]{12}", device_id):
        return False
    stored = secrets.get(keychain_service(), device_id)
    return bool(stored) and hmac.compare_digest(stored, str(token))


def auth(device_id, token):
    """Request auth for every phone-facing endpoint: the device must have
    completed pairing and present its exact token."""
    if not _token_ok(device_id, token):
        return None
    with _lock:
        data = _load()
        dev = next((d for d in data["devices"]
                    if d["id"] == device_id and not d.get("pending")), None)
        if dev:
            dev["last_seen"] = datetime.now().isoformat(timespec="seconds")
            _save(data)
    return dev


# ---------- hub URL (what the QR points the phone at) ----------

def hub_url(port=8377):
    """Best reachable URL for the phone: config override, then the
    tailnet (MagicDNS name, then 100.x IP), then the LAN IP."""
    override = settings.raw().get("companion_hub_url") or ""
    if override:
        return override
    dns, ip = _tailscale_self()
    host = dns or ip or _lan_ip()
    return f"http://{host}:{port}" if host else f"http://localhost:{port}"


def _tailscale_binary():
    p = shutil.which("tailscale")
    if p:
        return p
    if settings.IS_MAC:
        app = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        if Path(app).exists():
            return app
    return None


def _tailscale_self():
    binary = _tailscale_binary()
    if not binary:
        return None, None
    try:
        res = subprocess.run([binary, "status", "--json"],
                             capture_output=True, text=True, timeout=5)
        st = json.loads(res.stdout)
        self_node = st.get("Self") or {}
        dns = (self_node.get("DNSName") or "").rstrip(".")
        ips = self_node.get("TailscaleIPs") or []
        ip4 = next((i for i in ips if ":" not in i), None)
        return (dns or None), ip4
    except Exception:  # noqa: BLE001 — no tailscale, no problem
        return None, None


def _lan_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def _qr_svg(text):
    """QR as an SVG string via segno (pure python). None when the
    package is missing — the UI falls back to the copyable payload."""
    try:
        import io

        import segno
    except ImportError:
        return None
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=4, border=2,
                                     dark="#1a1a1a", light=None)
    return buf.getvalue().decode()


# ---------- message store (data/companion.sqlite) ----------

_db_lock = threading.Lock()

CHANNELS = {"sms", "mms", "rcs", "whatsapp", "notification"}


def _connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.execute("""CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY,
        device_id TEXT NOT NULL,
        dedupe_key TEXT NOT NULL UNIQUE,
        sender TEXT NOT NULL,
        sender_raw TEXT,
        person_id TEXT,
        channel TEXT NOT NULL,
        direction TEXT NOT NULL,
        when_epoch INTEGER NOT NULL,
        when_iso TEXT,
        text TEXT NOT NULL,
        source TEXT,
        received_at TEXT)""")
    con.execute("""CREATE INDEX IF NOT EXISTS idx_sender_when
                   ON messages(sender, when_epoch)""")
    return con


def norm_sender(raw):
    """Storage/join key for a sender: emails lowercase, phones as the
    10-digit norm the CRM indexes, short codes as bare digits, app
    display names (WhatsApp) as trimmed text."""
    s = (raw or "").strip()
    if not s:
        return ""
    if "@" in s:
        return s.lower()
    digits = re.sub(r"\D", "", s)
    if digits and len(digits) >= len(s) / 2:
        return crm.norm_digits(s) or digits
    return s.lower()


def _when_epoch(when):
    """Client timestamps arrive as epoch millis (Android convention),
    epoch seconds, or ISO strings; normalize to epoch seconds."""
    if isinstance(when, (int, float)):
        w = float(when)
        return w / 1000 if w > 4e10 else w
    if isinstance(when, str) and when:
        try:
            return datetime.fromisoformat(when).timestamp()
        except ValueError:
            return None
    return None


def dedupe_key(sender, when_epoch, text):
    raw = f"{sender}|{int(when_epoch)}|{text}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _near_dupe(con, sender, text, when_epoch):
    row = con.execute(
        """SELECT 1 FROM messages WHERE sender = ? AND text = ?
           AND when_epoch BETWEEN ? AND ? LIMIT 1""",
        (sender, text, when_epoch - NEAR_DUPE_S,
         when_epoch + NEAR_DUPE_S)).fetchone()
    return row is not None


def ingest(device_id, messages, watcher=None):
    """A batch from the phone. Stores new messages, drops dupes, joins
    senders to CRM people, and pushes fresh inbound items into the live
    feed. Returns per-batch counts the app shows the user."""
    assert_active()
    received = len(messages)
    new = dupes = bad = 0
    fresh_items = []
    now_iso = datetime.now().isoformat(timespec="seconds")
    with _db_lock:
        con = _connect()
        try:
            for m in messages:
                if not isinstance(m, dict):
                    bad += 1
                    continue
                text = str(m.get("text") or "").strip()
                sender_raw = str(m.get("sender") or "").strip()
                when = _when_epoch(m.get("when"))
                channel = str(m.get("channel") or "sms").lower()
                direction = "out" if m.get("direction") == "out" else "in"
                source = str(m.get("source") or "live")[:20]
                if not text or not sender_raw or when is None \
                        or channel not in CHANNELS:
                    bad += 1
                    continue
                sender = norm_sender(sender_raw)
                key = dedupe_key(sender, when, text[:2000])
                if _near_dupe(con, sender, text[:2000], when):
                    dupes += 1
                    continue
                pid = crm.resolve_handle(sender_raw) or crm.resolve_handle(sender)
                try:
                    con.execute(
                        """INSERT INTO messages(device_id, dedupe_key, sender,
                           sender_raw, person_id, channel, direction,
                           when_epoch, when_iso, text, source, received_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (device_id, key, sender, sender_raw, pid, channel,
                         direction, int(when),
                         datetime.fromtimestamp(when).isoformat(
                             timespec="seconds"),
                         text[:2000], source, now_iso))
                except sqlite3.IntegrityError:
                    dupes += 1
                    continue
                new += 1
                if direction == "in" and time.time() - when < FEED_FRESH_S:
                    item = _feed_item(key, sender, sender_raw, pid, channel,
                                      when, text)
                    if item:
                        fresh_items.append(item)
            con.commit()
        finally:
            con.close()
    if watcher is not None:
        for item in fresh_items:
            _push_feed(watcher, item)
    return {"received": received, "new": new, "duplicates": dupes,
            "invalid": bad}


def _feed_item(key, sender, sender_raw, pid, channel, when_epoch, text):
    from . import photos
    person = crm._load()["by_id"].get(pid) if pid else None
    feed_channel = "sms" if channel in ("sms", "mms", "rcs") else "companion"
    return {
        "rowid": f"comp-{key[:16]}",
        "when": datetime.fromtimestamp(
            when_epoch, tz=timezone.utc).astimezone().isoformat(),
        "channel": feed_channel,
        "via": "companion",
        "app": channel,
        "text": text[:500],
        "handle": sender,
        "group": False,
        "group_name": None,
        "person_id": pid,
        "person_name": person["name"] if person else (sender_raw or sender),
        "known": pid is not None,
        "has_photo": bool(pid and photos.photo_path(pid)),
    }


def _push_feed(watcher, item):
    """mail.py's _push_item shape: dedupe by rowid, keep the feed sorted
    and capped, wake the SSE listeners."""
    with watcher.lock:
        if any(x.get("rowid") == item["rowid"] for x in watcher.feed):
            return
        watcher.feed.append(item)
        watcher.feed.sort(key=lambda i: i.get("when") or "")
        watcher.feed = watcher.feed[-watcher.feed_size:]
        for q in list(watcher.listeners):
            try:
                q.put_nowait(item)
            except Exception:  # noqa: BLE001
                watcher.listeners.remove(q)


def stats():
    """Counts for the owner UI."""
    if not DB.exists():
        return {"messages": 0, "senders": 0, "unknown_senders": 0,
                "last_received": None}
    with _db_lock:
        con = _connect()
        try:
            msgs = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            senders = con.execute(
                "SELECT COUNT(DISTINCT sender) FROM messages").fetchone()[0]
            unknown = con.execute(
                """SELECT COUNT(DISTINCT sender) FROM messages
                   WHERE person_id IS NULL""").fetchone()[0]
            last = con.execute(
                "SELECT MAX(received_at) FROM messages").fetchone()[0]
        finally:
            con.close()
    return {"messages": msgs, "senders": senders,
            "unknown_senders": unknown, "last_received": last}


def unknown_senders(limit=40):
    """Triage candidates: senders whose person_id is still unresolved.
    Re-resolves at read time, so naming someone in triage clears them on
    the next look (and back-fills person_id in the store)."""
    if not DB.exists():
        return []
    with _db_lock:
        con = _connect()
        try:
            rows = con.execute(
                """SELECT sender, MIN(sender_raw), COUNT(*), MAX(when_iso),
                          MAX(channel)
                   FROM messages
                   WHERE person_id IS NULL AND direction = 'in'
                   GROUP BY sender
                   ORDER BY COUNT(*) DESC LIMIT ?""", (limit,)).fetchall()
            out = []
            for sender, sender_raw, n, last, channel in rows:
                pid = crm.resolve_handle(sender_raw) or crm.resolve_handle(sender)
                if pid:
                    con.execute(
                        "UPDATE messages SET person_id = ? WHERE sender = ?",
                        (pid, sender))
                    continue
                texts = [r[0] for r in con.execute(
                    """SELECT text FROM messages
                       WHERE sender = ? AND direction = 'in'
                       ORDER BY when_epoch DESC LIMIT 6""",
                    (sender,)).fetchall()]
                out.append({"handle": sender_raw or sender,
                            "sender": sender, "msgs": n, "last": last,
                            "channel": channel, "texts": texts})
            con.commit()
        finally:
            con.close()
    return out


def messages_for_person(pid, limit=40):
    """Companion thread for a person — the same shape as
    imessage.thread_for_person, for future thread merges."""
    if not DB.exists():
        return []
    with _db_lock:
        con = _connect()
        try:
            rows = con.execute(
                """SELECT id, when_iso, direction, text, sender, channel
                   FROM messages WHERE person_id = ?
                   ORDER BY when_epoch DESC LIMIT ?""",
                (pid, max(1, min(limit, 500)))).fetchall()
        finally:
            con.close()
    return [{"rowid": f"comp-{rid}", "when": when,
             "from_me": direction == "out", "text": text,
             "handle": sender, "app": channel}
            for rid, when, direction, text, sender, channel in reversed(rows)]


# ---------- pings (hub -> phone) ----------

def queue_ping(text, kind="notify"):
    """Queue a ping for the paired phone(s); wakes any long-poll. Returns
    True when at least one paired device will see it."""
    text = (text or "").strip()
    if not text:
        return False
    with _lock:
        data = _load()
        if not any(not d.get("pending") for d in data["devices"]):
            return False
        pid = data.get("next_ping_id", 1)
        data.setdefault("pings", []).append({
            "id": pid, "text": text[:300], "kind": kind,
            "created": datetime.now().isoformat(timespec="seconds")})
        data["pings"] = data["pings"][-PING_KEEP:]
        data["next_ping_id"] = pid + 1
        _save(data)
    _ping_event.set()
    _ping_event.clear()
    return True


def pings_since(after_id=0):
    return [p for p in _load().get("pings", []) if p["id"] > int(after_id)]


def wait_for_pings(after_id=0, timeout_s=25):
    """Long-poll helper: immediate when there is anything newer than
    after_id, else block (in 1s slices) until a ping lands or the window
    closes. Runs on a worker thread via the route's sync def."""
    deadline = time.time() + max(0, min(timeout_s, 55))
    while True:
        got = pings_since(after_id)
        if got or time.time() >= deadline:
            return got
        _ping_event.wait(timeout=1.0)
