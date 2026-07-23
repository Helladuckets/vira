"""Send messages from the app via Messages.app (AppleScript). Deterministic —
no AI involved; the text is whatever the user approved in the UI.

iMessage vs SMS/text — the silent-failure this module fixes: sending to an
Android (or otherwise iMessage-less) recipient over the iMessage service
"succeeds" from AppleScript's point of view, then the balloon quietly turns
red "Not Delivered" and nothing the app can see ever reports it. Two guards:

  - proactive: a contact the owner marked SMS, or one chat.db shows has
    only ever been reachable over SMS, is sent on the SMS service from the
    start (best case — no failed attempt at all);
  - reactive: an iMessage send is verified against chat.db for a few
    seconds; if the row errors, Vira re-sends it as a text AND remembers
    (sendpref, source="inferred") so the next send is proactive.

Sending SMS needs Text Message Forwarding to the paired iPhone (Messages
must own an SMS-service account). When it doesn't, we say so plainly rather
than failing quietly — the whole point here is to never fail silently.
"""
import datetime
import os
import subprocess
import time

from . import data as crm
from . import imessage, sendpref, settings

# {service} is substituted with "iMessage" or "SMS" — the account Messages
# routes the send through. A missing SMS account (Text Message Forwarding
# off) makes the `1st account` lookup raise, which surfaces as a clear error.
SCRIPT = '''
on run {targetHandle, msgText}
    tell application "Messages"
        set targetService to 1st account whose service type = %s
        set targetBuddy to participant targetHandle of targetService
        send msgText to targetBuddy
    end tell
end run
'''

_SERVICES = {"imessage": "iMessage", "sms": "SMS"}


def best_handle(pid):
    """Pick the handle the owner actually texts this person on: the one from the
    most recent direct-thread message, falling back to the first known."""
    msgs = imessage.thread_for_person(pid, limit=5)
    for m in reversed(msgs):
        if m.get("handle"):
            return m["handle"]
    p = crm._load()["by_id"].get(pid)
    if not p:
        return None
    handles = p.get("handles", {})
    ims = handles.get("imessage", [])
    if ims:
        return ims[0]
    phones = handles.get("phones10", [])
    return "+1" + phones[0] if phones else None


def imessage_capable(handle):
    """Has this handle ever been reachable over iMessage? Reads chat.db's
    handle table, which carries a separate row per (id, service). Returns
    True if an iMessage row exists, False if only SMS rows exist, and None
    when unknown (no rows, or chat.db unreadable) — an unknown handle
    defaults to an iMessage attempt with the reactive guard behind it."""
    if not handle or not settings.IS_MAC or settings.fixture_mode():
        return None
    try:
        con = imessage._connect()
        try:
            rows = con.execute(
                "SELECT DISTINCT service FROM handle WHERE id = ?",
                (handle,)).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — chat.db unreadable -> unknown
        return None
    services = {(r[0] or "").lower() for r in rows}
    if not services:
        return None
    # iMessage and iMessageLite both mean "reachable over iMessage".
    if any(s.startswith("imessage") for s in services):
        return True
    return False


def resolve_channel(pid, handle, override=None):
    """Which service to send on: an explicit override wins, then a stored
    preference (owner mark or a past inference), then live chat.db
    inference, defaulting to iMessage."""
    if override in _SERVICES:
        return override
    pref = sendpref.get(pid)
    if pref and pref.get("channel") in _SERVICES:
        return pref["channel"]
    if imessage_capable(handle) is False:
        return "sms"
    return "imessage"


def _osa_send(target, text, channel, timeout=20):
    """One raw AppleScript send on the given service. Raises RuntimeError with
    the osascript stderr on failure (e.g. no SMS account, or automation
    permission not granted)."""
    res = subprocess.run(
        ["osascript", "-", target, text],
        input=SCRIPT % _SERVICES[channel],
        capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[-400:] or "osascript failed")


def _imessage_errored(target, since_ns, wait_seconds):
    """Best-effort: did the just-sent outbound iMessage to `target` record a
    delivery error within the wait window? Polls chat.db for the newest
    outbound row to this handle created after `since_ns` and checks its
    `error` column (nonzero = not delivered). Returns True only on a
    definite error; False if it looks fine, delivered, or unknown. Never
    raises — a chat.db it can't read means "can't tell", i.e. don't retry.

    A delivery error lands a few seconds AFTER the row appears clean, so a
    fresh error=0 row is not yet proof of success — we keep watching to the
    deadline. But a delivery RECEIPT (is_delivered) is proof, so we bail
    early on it: the happy path returns in ~the round-trip, not the whole
    window, which is what keeps a normal send (and every notify ping routed
    through this) snappy."""
    if not settings.IS_MAC or settings.fixture_mode():
        return False
    deadline = time.time() + max(0.0, wait_seconds)
    while True:
        try:
            con = imessage._connect()
            try:
                row = con.execute(
                    """SELECT m.error, m.is_delivered
                       FROM message m
                       JOIN handle h ON h.ROWID = m.handle_id
                       WHERE h.id = ? AND m.is_from_me = 1
                         AND m.service = 'iMessage' AND m.date >= ?
                       ORDER BY m.date DESC LIMIT 1""",
                    (target, since_ns)).fetchone()
            finally:
                con.close()
        except Exception:  # noqa: BLE001
            return False
        if row is not None:
            error, delivered = row
            if error and int(error) != 0:
                return True
            if delivered and int(delivered) != 0:
                return False  # delivery receipt — a definite success
        if time.time() >= deadline:
            return False
        time.sleep(0.75)


def send_message(text, person_id=None, handle=None, channel=None, timeout=20):
    """Send `text` and return a result dict:
        {handle, channel, downgraded, note}
    channel is the service actually used ("imessage" or "sms"); downgraded is
    True when an iMessage attempt failed and Vira re-sent as a text. Proactive
    channel selection + a reactive SMS fallback mean an Android recipient
    never gets a silently-dropped message.

    Raises ValueError for bad input (no handle / empty text) and RuntimeError
    when Messages refuses (automation permission, or SMS requested with no
    Text Message Forwarding account)."""
    if os.environ.get("VIRA_PASSIVE"):
        raise RuntimeError(
            "passive test instance: outbound iMessage is blocked")
    if not settings.IS_MAC:
        raise RuntimeError(
            "iMessage sending needs macOS (Messages.app) — not available "
            "on this platform")
    target = handle or (best_handle(person_id) if person_id else None)
    if not target:
        raise ValueError("no iMessage handle for this person")
    if not text or not text.strip():
        raise ValueError("empty message")

    chosen = resolve_channel(person_id, target,
                             override=channel if channel in _SERVICES else None)

    if chosen == "sms":
        try:
            _osa_send(target, text, "sms", timeout)
        except RuntimeError as e:
            raise RuntimeError(
                "couldn't send as a text — Text Message Forwarding to your "
                "iPhone may be off (Messages needs an SMS account). "
                + str(e))
        return {"handle": target, "channel": "sms", "downgraded": False,
                "note": None}

    # iMessage attempt, then verify + fall back to SMS on a delivery error.
    since_ns = imessage.apple_ns(datetime.datetime.now())
    _osa_send(target, text, "imessage", timeout)
    verify_seconds = float(settings.get("send_verify_seconds"))
    fallback_on = bool(settings.get("sms_fallback"))
    if fallback_on and verify_seconds > 0 and \
            _imessage_errored(target, since_ns, verify_seconds):
        try:
            _osa_send(target, text, "sms", timeout)
        except RuntimeError:
            # iMessage failed AND we can't text — surface the honest state.
            return {"handle": target, "channel": "imessage",
                    "downgraded": False,
                    "note": "iMessage was not delivered and no SMS route is "
                            "set up (Text Message Forwarding may be off)."}
        # Remember, so next time is proactive.
        try:
            sendpref.set_channel(person_id, "sms", source="inferred")
        except Exception:  # noqa: BLE001 — persistence is best-effort
            pass
        return {"handle": target, "channel": "sms", "downgraded": True,
                "note": "iMessage was not delivered, so Vira sent it as a "
                        "text instead."}
    return {"handle": target, "channel": "imessage", "downgraded": False,
            "note": None}


def send_imessage(text, person_id=None, handle=None, timeout=20):
    """Backward-compatible wrapper: sends (with proactive channel choice and
    the reactive SMS fallback) and returns just the handle used, the old
    contract that notify.py and other callers rely on."""
    return send_message(text, person_id, handle, timeout=timeout)["handle"]
