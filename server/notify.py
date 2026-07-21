"""Push notifications on high-value inbound, delivered over iMessage.

Channel decision (2026-07-07): Vira texts the owner via Messages.app — the
same AppleScript path as /api/send — instead of ntfy/APNs/Tailscale Serve.
Zero new infrastructure, lands on every Apple device, and the send path is
already proven. Inbound iMessages are deliberately NOT notified (the phone
already surfaces those natively); the gap this closes is email — mail is
only seen when the inbox is open, so a note from an active-tier contact
can sit for hours.

Rule (deterministic): notify when an inbound email's sender resolves to a
CRM person whose tier is "active". Throttles: one notification per sender
per 6h window, max 20/day, so a busy thread can't storm the phone.

Config lives in data/config.json (notify_enabled, notify_handle) and is
editable from the settings sheet. Dormant until notify_handle is set —
use your own iMessage self-thread number (mind carrier quirks: a handle
that exists only as a message-less RCS row can time out AppleScript
sends; use the handle your self-thread actually lives on). State + a
rolling log live in data/notify-log.json (surfaced in the Jobs window).
"""
import json
import threading
import time
from datetime import datetime
from pathlib import Path

from . import data as crm

_DATA = Path(__file__).resolve().parent.parent / "data"
CONFIG = _DATA / "config.json"
LOG = _DATA / "notify-log.json"

SENDER_COOLDOWN = 6 * 3600
DAILY_CAP = 20

_lock = threading.Lock()


def config():
    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        cfg = {}
    return {
        "enabled": bool(cfg.get("notify_enabled", True)),
        "handle": cfg.get("notify_handle") or "",  # empty = dormant
        "tier": cfg.get("notify_tier", "active"),
    }


def save_config(updates):
    with _lock:
        try:
            cfg = json.loads(CONFIG.read_text())
        except (OSError, json.JSONDecodeError):
            cfg = {}
        if "enabled" in updates:
            cfg["notify_enabled"] = bool(updates["enabled"])
        if "handle" in updates and updates["handle"] is not None:
            cfg["notify_handle"] = str(updates["handle"]).strip()
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(cfg, indent=2))
    return config()


def _load_log():
    try:
        return json.loads(LOG.read_text())
    except (OSError, json.JSONDecodeError):
        return {"sent": []}


def _save_log(log):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps(log, indent=1))


def recent(limit=40):
    return list(reversed(_load_log().get("sent", [])))[:limit]


def _record(entry):
    with _lock:
        log = _load_log()
        log.setdefault("sent", []).append(entry)
        log["sent"] = log["sent"][-200:]
        _save_log(log)


def _throttled(person_id):
    now = time.time()
    sent = _load_log().get("sent", [])
    today = datetime.now().date().isoformat()
    ok_today = [e for e in sent if e.get("ok") and
                (e.get("at") or "").startswith(today)]
    if len(ok_today) >= DAILY_CAP:
        return "daily cap reached"
    for e in reversed(sent):
        if e.get("person_id") == person_id and e.get("ok"):
            try:
                at = datetime.fromisoformat(e["at"]).timestamp()
            except (KeyError, ValueError):
                continue
            if now - at < SENDER_COOLDOWN:
                return "sender cooldown"
            break
    return None


def _companion_paired():
    """A paired Android companion device is a delivery channel of its own
    — pings work even where iMessage cannot (a Windows hub, no
    notify_handle configured)."""
    try:
        from . import companion
        return companion.has_devices()
    except Exception:  # noqa: BLE001 — never let ping plumbing break notify
        return False


def maybe_notify(item):
    """Called by the mail watcher for every new inbound email feed item.
    Fires the iMessage in a thread so the poll loop never blocks on
    osascript."""
    cfg = config()
    if not cfg["enabled"] or not (cfg["handle"] or _companion_paired()):
        return
    if item.get("channel") != "email" or not item.get("person_id"):
        return
    person = crm._load()["by_id"].get(item["person_id"])
    if not person:
        return
    tier = person.get("profile_tier") or person.get("master_tier")
    if tier != cfg["tier"]:
        return
    why = _throttled(item["person_id"])
    if why:
        return
    subject = item.get("subject") or (item.get("text") or "")[:80]
    text = f"Vira: {person['name']} emailed — {subject[:140]}"
    threading.Thread(target=_send, args=(cfg["handle"], text, item),
                     daemon=True, name="vira-notify").start()


def agent_ping(text, key=None):
    """Agentic-OS completion pings (muse proposals, circuit finishes,
    routine outcomes) on the same iMessage path. `key` rides the throttle
    as a pseudo person id — a unique key per event means the 6h sender
    cooldown dedupes retries of the SAME event while distinct events still
    ping; the daily cap always applies."""
    cfg = config()
    if not cfg["enabled"] or not (cfg["handle"] or _companion_paired()):
        return False
    key = key or "agent"
    if _throttled(f"agent:{key}"):
        return False
    threading.Thread(
        target=_send, args=(cfg["handle"], text[:300],
                            {"person_id": f"agent:{key}",
                             "person_name": "Vira", "channel": "agent"}),
        daemon=True, name="vira-agent-ping").start()
    return True


def _send(handle, text, item):
    """Deliver on every channel that exists: iMessage when a handle is
    configured, a companion ping when an Android phone is paired. ok =
    at least one landed."""
    from . import send
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "person_id": item.get("person_id"),
        "person_name": item.get("person_name"),
        "channel": item.get("channel"),
        "text": text,
        "ok": False,
    }
    via = []
    if handle:
        try:
            send.send_imessage(text, handle=handle)
            via.append("imessage")
        except Exception as e:  # noqa: BLE001 — log the failure, never crash
            entry["error"] = str(e)[:200]
    try:
        from . import companion
        if companion.queue_ping(text, kind=item.get("channel") or "notify"):
            via.append("companion")
    except Exception as e:  # noqa: BLE001
        entry.setdefault("error", str(e)[:200])
    entry["ok"] = bool(via)
    entry["via"] = via
    _record(entry)


def subs_renewals():
    """Renewal radar rule (subscriptions phase 4): ping for a renewal
    within 7 days when the per-cycle amount clears
    `subs_notify_threshold_usd` (default 100) — or for ANY annual renewal
    (a yearly hit is always worth a heads-up). Deduped per
    (merchant, renewal date) in the subs ledger meta, so the 6h Mercury
    poll cycle can call this freely; the shared daily cap and per-sender
    cooldown still apply on top. Returns the number of pings sent."""
    cfg = config()
    if not cfg["enabled"] or not cfg["handle"]:
        return 0
    from . import settings, subscriptions
    threshold = float(settings.get("subs_notify_threshold_usd") or 100)
    cycles = {"monthly": 12, "quarterly": 4, "semi-annual": 2, "annual": 1}
    r = subscriptions.reconcile()
    conn = subscriptions.ledger_connect()
    try:
        try:
            seen = json.loads(
                subscriptions.meta_get(conn, "subs_notified") or "{}")
        except json.JSONDecodeError:
            seen = {}
        sent = 0
        today = datetime.now().date()
        for m in r["merchants"]:
            if m["status"] in ("canceled", "ignored") or not m["next_renewal"]:
                continue
            days = (datetime.fromisoformat(m["next_renewal"]).date()
                    - today).days
            cycle_amt = (m["yearly"] / cycles[m["cadence"]]
                         if m["cadence"] in cycles and m["yearly"] else 0)
            if not 0 <= days <= 7:
                continue
            if cycle_amt < threshold and m["cadence"] != "annual":
                continue
            if seen.get(m["id"]) == m["next_renewal"]:
                continue
            if _throttled("subs:" + m["id"]):
                continue
            label = "today" if days == 0 else f"in {days}d"
            src = " (receipt-confirmed)" if m.get("renewal_source") == "receipt" else ""
            text = (f"Vira: {m['display_name']} renews {label} "
                    f"({m['next_renewal']}) — ${cycle_amt:,.2f} per "
                    f"{m['cadence'].replace('-', ' ')} cycle{src}")
            _send(cfg["handle"], text,
                  {"person_id": "subs:" + m["id"],
                   "person_name": m["display_name"], "channel": "subs"})
            seen[m["id"]] = m["next_renewal"]
            sent += 1
        subscriptions.meta_set(conn, "subs_notified", json.dumps(seen))
        conn.commit()
        return sent
    finally:
        conn.close()


def send_test(handle=None):
    """Settings-sheet test button: sends one message synchronously."""
    from . import send
    target = (handle or config()["handle"]).strip()
    if not target:
        raise ValueError("no notify handle configured (settings > Notifications)")
    text = "Vira: test notification — the iMessage channel works."
    send.send_imessage(text, handle=target)
    _record({
        "at": datetime.now().isoformat(timespec="seconds"),
        "person_id": None,
        "person_name": "(test)",
        "channel": "test",
        "text": text,
        "ok": True,
    })
    return {"sent": True, "handle": target}
