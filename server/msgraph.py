"""Microsoft 365 mail via the Graph API, device-code OAuth.

Why not IMAP: Exchange Online disabled basic authentication (which app
passwords ride on) for IMAP tenant-wide in 2023 — the "does the tenant
allow IMAP app passwords" answer is no, nobody's does anymore. The
deterministic replacement is Graph with a one-time device-code login:
the user enters a short code at microsoft.com/devicelogin, Vira stores the
refresh token in the Keychain (service vira-mail-graph) and the mail
watcher polls /me/messages the same way it polls IMAP INBOXes.

Requires a tiny app registration in the tenant ("Vira", public client
flows enabled). Every Microsoft first-party client id is a dead end for
mail scopes now: Graph CLI isn't provisioned in this tenant
(AADSTS700016), and as of 2026-07 the Azure CLI id needs first-party
preauthorization to request Graph mail scopes (AADSTS65002). A
tenant-owned registration is exempt from those rules permanently. Set
"msgraph_client_id" and "msgraph_tenant" in data/config.json.
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import secrets, settings

SCOPE ="https://graph.microsoft.com/Mail.ReadWrite offline_access"
# device login asks for calendar too (brief v2); token refreshes keep
# requesting only the scope they need, so a pre-calendar refresh token
# keeps working for mail and simply can't mint a calendar token until
# the user reconnects once.
SCOPE_CAL = "https://graph.microsoft.com/Calendars.Read offline_access"
SCOPE_LOGIN = ("https://graph.microsoft.com/Mail.ReadWrite "
               "https://graph.microsoft.com/Calendars.Read offline_access")
GRAPH = "https://graph.microsoft.com/v1.0"
KEYCHAIN_SERVICE = "vira-mail-graph"   # namespaced per instance by settings.keychain_service
_DATA = Path(__file__).resolve().parent.parent / "data"
ACCOUNTS = _DATA / "mail-accounts.json"
CONFIG = _DATA / "config.json"


def _auth():
    """(client_id, authority) from config.json, re-read every call so a
    pasted-in registration takes effect without a restart."""
    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        cfg = {}
    client_id = cfg.get("msgraph_client_id", "")
    tenant = cfg.get("msgraph_tenant", "organizations")
    if not client_id:
        raise RuntimeError(
            "no msgraph_client_id in data/config.json — register a public-"
            "client app in Entra and paste its client id (+ tenant id)")
    return client_id, f"https://login.microsoftonline.com/{tenant}"

_flows = {}    # email -> {user_code, verification_uri, expires_at, error, connected}
_tokens = {}   # (email, scope) -> {access_token, expires_at}
_lock = threading.Lock()


def _post_form(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def _graph_request(email, path, method="GET", payload=None, scope=SCOPE,
                   headers=None):
    tok = _access_token(email, scope)
    req = urllib.request.Request(
        GRAPH + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"authorization": "Bearer " + tok,
                 "content-type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def get_bytes(email, path, scope=SCOPE):
    """Raw bytes of a Graph resource (e.g. an attachment's /$value) — used
    for attachments too large to ride inline as contentBytes."""
    tok = _access_token(email, scope)
    req = urllib.request.Request(
        GRAPH + path,
        headers={"authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


# ---------- keychain ----------

def _stored_refresh_token(email):
    return secrets.get(settings.keychain_service(KEYCHAIN_SERVICE), email) or None


def _store_refresh_token(email, token):
    # The rotating refresh token: losing a write logs the account out, so
    # let the ladder land it in whatever store this machine has. The mac
    # backend keeps the argv-safety pattern (`security -i`, audit P1-1).
    try:
        secrets.set(settings.keychain_service(KEYCHAIN_SERVICE), email, token)
    except RuntimeError:
        pass  # same contract as the old best-effort write


def connected(email):
    return _stored_refresh_token(email) is not None


# ---------- tokens ----------

def _accept_tokens(email, payload, scope=SCOPE):
    with _lock:
        _tokens[(email, scope)] = {
            "access_token": payload["access_token"],
            "expires_at": time.time() + int(payload.get("expires_in", 3600)) - 120,
        }
    if payload.get("refresh_token"):  # Microsoft rotates these; keep the newest
        _store_refresh_token(email, payload["refresh_token"])


def _access_token(email, scope=SCOPE):
    with _lock:
        tok = _tokens.get((email, scope))
        if tok and tok["expires_at"] > time.time():
            return tok["access_token"]
    refresh = _stored_refresh_token(email)
    if not refresh:
        raise RuntimeError("not connected — run the device login in settings")
    client_id, authority = _auth()
    payload = _post_form(authority + "/oauth2/v2.0/token", {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "scope": scope,
    })
    if "access_token" not in payload:
        raise RuntimeError("token refresh failed: "
                           + payload.get("error_description", "")[:200])
    _accept_tokens(email, payload, scope)
    return payload["access_token"]


# ---------- device-code flow ----------

def start_device_flow(email):
    client_id, authority = _auth()
    payload = _post_form(authority + "/oauth2/v2.0/devicecode", {
        "client_id": client_id, "scope": SCOPE_LOGIN,
    })
    if "device_code" not in payload:
        raise RuntimeError(payload.get("error_description",
                                       "device code request failed")[:300])
    flow = {
        "user_code": payload["user_code"],
        "verification_uri": payload.get("verification_uri",
                                        "https://microsoft.com/devicelogin"),
        "expires_at": time.time() + int(payload.get("expires_in", 900)),
        "error": None,
        "connected": False,
    }
    _flows[email] = flow
    threading.Thread(
        target=_poll_for_token,
        args=(email, payload["device_code"], int(payload.get("interval", 5)),
              flow["expires_at"]),
        daemon=True, name="vira-graph-devicecode").start()
    return {"user_code": flow["user_code"],
            "verification_uri": flow["verification_uri"],
            "expires_in": int(payload.get("expires_in", 900))}


def _poll_for_token(email, device_code, interval, expires_at):
    client_id, authority = _auth()
    while time.time() < expires_at:
        time.sleep(interval)
        payload = _post_form(authority + "/oauth2/v2.0/token", {
            "client_id": client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        })
        if "access_token" in payload:
            _accept_tokens(email, payload)
            _flows[email]["connected"] = True
            _ensure_account_entry(email)
            return
        err = payload.get("error", "")
        if err in ("authorization_pending", "slow_down"):
            if err == "slow_down":
                interval += 5
            continue
        _flows[email]["error"] = payload.get("error_description", err)[:300]
        return
    _flows[email]["error"] = "device code expired — start again"


def _ensure_account_entry(email):
    """Once connected, register the account so the mail watcher polls it."""
    try:
        accounts = json.loads(ACCOUNTS.read_text())
    except (OSError, json.JSONDecodeError):
        accounts = []
    if not any(a.get("email") == email for a in accounts):
        accounts.append({"email": email, "type": "graph"})
        ACCOUNTS.write_text(json.dumps(accounts, indent=1))


def flow_status(email):
    flow = _flows.get(email, {})
    return {
        "connected": flow.get("connected") or connected(email),
        "pending": bool(flow) and not flow.get("connected")
        and not flow.get("error") and time.time() < flow.get("expires_at", 0),
        "user_code": flow.get("user_code"),
        "verification_uri": flow.get("verification_uri"),
        "error": flow.get("error"),
    }


# ---------- mail ----------

def fetch_new_messages(email, last_iso, seen_ids=None):
    """Inbox messages newer than the watermark. First run baselines at the
    newest message and emits nothing old (same contract as the IMAP path).

    Watermark gotcha: Graph returns receivedDateTime truncated to whole
    seconds, but Exchange stores it with sub-second precision — so a
    `gt <watermark>` filter re-matches the newest message on EVERY poll
    (22:57:11.489 > 22:57:11) and it echoes into the feed once a minute
    until newer mail arrives. Fix: filter `ge` (so same-second siblings
    are never missed either) and let the caller pass the already-emitted
    message ids, which are excluded here."""
    if last_iso is None:
        top = _graph_request(
            email, "/me/mailFolders/inbox/messages"
                   "?$orderby=receivedDateTime%20desc&$top=1"
                   "&$select=receivedDateTime")
        vals = top.get("value", [])
        return [], (vals[0]["receivedDateTime"] if vals else "1970-01-01T00:00:00Z")
    q = ("/me/mailFolders/inbox/messages"
         f"?$filter=receivedDateTime%20ge%20{last_iso}"
         "&$orderby=receivedDateTime%20asc&$top=20"
         "&$select=id,subject,from,receivedDateTime,bodyPreview,internetMessageId")
    raw = _graph_request(email, q).get("value", [])
    watermark = raw[-1]["receivedDateTime"] if raw else last_iso
    seen = set(seen_ids or ())
    return [m for m in raw if m.get("id") not in seen], watermark


def create_draft(email, to, subject, body):
    """Ready-to-send draft in the M365 mailbox."""
    msg = _graph_request(email, "/me/messages", method="POST", payload={
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": to}}],
    })
    return {"saved": True, "account": email, "folder": "Drafts",
            "id": msg.get("id")}


# ---------- calendar + drafts (brief v2) ----------

def calendar_events(email, start_iso, end_iso, tz="America/New_York"):
    """Expanded occurrences from the M365 calendar between two local ISO
    datetimes. Raises RuntimeError (e.g. "needs consent") until the account
    has been reconnected once with the calendar scope."""
    try:
        q = ("/me/calendarView"
             f"?startDateTime={urllib.parse.quote(start_iso)}"
             f"&endDateTime={urllib.parse.quote(end_iso)}"
             "&$orderby=start/dateTime&$top=50"
             "&$select=subject,start,end,isAllDay,location,organizer,"
             "isOnlineMeeting")
        out = _graph_request(email, q, scope=SCOPE_CAL,
                             headers={"Prefer": f'outlook.timezone="{tz}"'})
    except RuntimeError as e:
        if "AADSTS65001" in str(e) or "consent" in str(e).lower():
            raise RuntimeError("needs consent — reconnect M365 in settings")
        raise
    events = []
    for ev in out.get("value", []):
        events.append({
            "title": ev.get("subject") or "(no title)",
            "all_day": bool(ev.get("isAllDay")),
            "start": (ev.get("start") or {}).get("dateTime", "")[:19],
            "end": (ev.get("end") or {}).get("dateTime", "")[:19],
            "online": bool(ev.get("isOnlineMeeting")),
        })
    return events


def list_drafts(email, limit=10):
    """Drafts sitting in the M365 mailbox, newest first."""
    out = _graph_request(
        email, "/me/mailFolders/drafts/messages"
               f"?$orderby=lastModifiedDateTime%20desc&$top={limit}"
               "&$select=subject,toRecipients,lastModifiedDateTime")
    drafts = []
    for d in out.get("value", []):
        tos = [((r.get("emailAddress") or {}).get("name")
                or (r.get("emailAddress") or {}).get("address") or "")
               for r in d.get("toRecipients", [])]
        drafts.append({
            "subject": d.get("subject") or "(no subject)",
            "to": ", ".join(t for t in tos if t) or "(no recipient)",
            "modified": d.get("lastModifiedDateTime"),
            "account": email,
        })
    return drafts
