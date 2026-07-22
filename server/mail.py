"""Email in the feed: IMAP inbox watcher. Deterministic — polls INBOX for new
messages, joins senders to CRM people, and merges items into the same live
feed as iMessage.

Dormant until an account is configured. Setup (one time, per account):
  1. Store the password in the secrets store (server/secrets.py — the
     macOS Keychain here; Credential Manager on Windows). On a Mac:
       security add-generic-password -a you@yourdomain.com -s vira-mail -w
     (Gmail: use an app password from myaccount.google.com/apppasswords;
      Outlook/M365: an app password if the tenant allows IMAP, else IMAP is
      blocked and that account stays on the connector path.)
  2. Add the account to data/mail-accounts.json:
       [{"email": "you@yourdomain.com", "host": "outlook.office365.com"},
        {"email": "you@gmail.com", "host": "imap.gmail.com"}]
The watcher picks up the file within one poll cycle; no restart needed.
"""
import email
import email.header
import email.message
import email.utils
import imaplib
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import data as crm
from . import secrets, settings

ACCOUNTS = Path(__file__).resolve().parent.parent / "data" / "mail-accounts.json"
STATE = Path(__file__).resolve().parent.parent / "data" / "mail-state.json"


def keychain_service():
    return settings.keychain_service("vira-mail")


def keychain_password(account_email):
    return secrets.get(keychain_service(), account_email) or None


def load_accounts():
    """The configured accounts, tolerating both the bare-list and
    {"accounts": [...]} shapes the file has carried."""
    try:
        raw = json.loads(ACCOUNTS.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return raw if isinstance(raw, list) else raw.get("accounts", [])


def _save_accounts(accts):
    ACCOUNTS.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNTS.with_name(ACCOUNTS.name + ".tmp")
    tmp.write_text(json.dumps(accts, indent=1))
    tmp.replace(ACCOUNTS)


def add_imap_account(account_email, host, password):
    """Wire a Gmail/IMAP mailbox from the app: the password lands in the
    secrets ladder (never the JSON), the {email, host} row in
    mail-accounts.json. The watcher picks it up within one poll — no
    restart. Re-adding the same address updates its host in place."""
    account_email = (account_email or "").strip().lower()
    host = (host or "").strip()
    password = password or ""
    if "@" not in account_email:
        raise ValueError("a valid email address is required")
    if not host:
        raise ValueError("an IMAP host is required (e.g. imap.gmail.com)")
    if not password:
        raise ValueError("a password is required")
    secrets.set(keychain_service(), account_email, password)
    accts = load_accounts()
    for a in accts:
        if (a.get("email") or "").strip().lower() == account_email \
                and a.get("type") != "graph":
            a["host"] = host
            _save_accounts(accts)
            return {"email": account_email, "host": host, "added": False}
    accts.append({"email": account_email, "host": host})
    _save_accounts(accts)
    return {"email": account_email, "host": host, "added": True}


def _decode_header(raw):
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _body_preview(msg, limit=400):
    """Plain-text preview of the message body."""
    part = msg
    if msg.is_multipart():
        part = next((p for p in msg.walk()
                     if p.get_content_type() == "text/plain"
                     and "attachment" not in str(p.get("Content-Disposition", ""))),
                    None)
        if part is None:
            part = next((p for p in msg.walk()
                         if p.get_content_type() == "text/html"), None)
        if part is None:
            return ""
    try:
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8",
                              errors="replace")
    except Exception:  # noqa: BLE001 — malformed MIME; skip preview
        return ""
    if part.get_content_type() == "text/html":
        text = re.sub(r"<style.*?</style>|<script.*?</script>", " ", text,
                      flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _drafts_folder(con):
    """Find the mailbox flagged \\Drafts (RFC 6154); Gmail's is [Gmail]/Drafts."""
    status, boxes = con.list()
    if status == "OK":
        for raw in boxes or []:
            line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            if "\\Drafts" in line:
                m = re.findall(r'"([^"]+)"', line)
                if m:
                    return m[-1]
    return "Drafts"


def create_draft(account, to, subject, body, in_reply_to=None, references=None):
    """Ready-to-send draft in the account's Drafts folder. Gmail/IMAP path is
    an APPEND with the \\Draft flag (shows up everywhere Gmail does); Graph
    accounts go through the Graph API."""
    try:
        accounts = json.loads(ACCOUNTS.read_text())
    except (OSError, json.JSONDecodeError):
        accounts = []
    acct = next((a for a in accounts if a["email"] == account), None) \
        or (accounts[0] if accounts else None)
    if not acct:
        raise RuntimeError("no mail account configured")
    addr = acct["email"]
    if acct.get("type") == "graph":
        from . import msgraph
        return msgraph.create_draft(addr, to, subject, body)

    password = keychain_password(addr)
    if not password:
        raise RuntimeError(f"no password in keychain for {addr} "
                           f"(service {keychain_service()})")
    msg = email.message.EmailMessage()
    msg["From"] = addr
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = ((references + " ") if references else "") + in_reply_to
    msg.set_content(body)
    con = imaplib.IMAP4_SSL(acct["host"])
    try:
        con.login(addr, password)
        folder = _drafts_folder(con)
        status, data = con.append(
            f'"{folder}"', r"(\Draft)",
            imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        if status != "OK":
            raise RuntimeError(f"IMAP append failed: {data}")
    finally:
        try:
            con.logout()
        except Exception:  # noqa: BLE001
            pass
    return {"saved": True, "account": addr, "folder": folder}


class MailWatcher:
    """Polls each configured account's INBOX for messages newer than the last
    seen UID. Pushes feed items shaped like the iMessage ones (channel=email)."""

    def __init__(self, imessage_watcher, poll_seconds=60):
        self.watcher = imessage_watcher   # shared feed + listeners
        self.poll = poll_seconds
        self.state = {}
        self.status = {}                  # email -> ok | error text | "no password"
        self._stop = threading.Event()

    def accounts(self):
        try:
            return json.loads(ACCOUNTS.read_text())
        except (OSError, json.JSONDecodeError):
            return []

    def _load_state(self):
        try:
            self.state = json.loads(STATE.read_text())
        except (OSError, json.JSONDecodeError):
            self.state = {}

    def _save_state(self):
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(self.state))

    def start(self):
        self._load_state()
        threading.Thread(target=self._run, daemon=True, name="vira-mail").start()

    def _run(self):
        while not self._stop.is_set():
            for acct in self.accounts():
                try:
                    self._poll_account(acct)
                    self.status[acct["email"]] = "ok"
                except Exception as e:  # noqa: BLE001 — keep polling others
                    self.status[acct["email"]] = str(e)[:200]
            self._stop.wait(self.poll)

    def _poll_account(self, acct):
        if acct.get("type") == "graph":
            self._poll_graph(acct)
            return
        addr, host = acct["email"], acct["host"]
        password = keychain_password(addr)
        if not password:
            self.status[addr] = f"no password in keychain (service {keychain_service()})"
            return
        con = imaplib.IMAP4_SSL(host)
        try:
            con.login(addr, password)
            con.select("INBOX", readonly=True)
            last_uid = self.state.get(addr)
            if last_uid is None:
                # first run: baseline at the newest message, emit nothing old.
                # STATUS UIDNEXT avoids listing a huge mailbox (imaplib caps
                # response lines at 1 MB, which "SEARCH ALL" can exceed).
                _, data = con.status("INBOX", "(UIDNEXT)")
                m = re.search(rb"UIDNEXT (\d+)", data[0])
                self.state[addr] = int(m.group(1)) - 1 if m else 0
                self._save_state()
                return
            _, data = con.uid("search", None, f"UID {last_uid + 1}:*")
            uids = [int(u) for u in data[0].split() if int(u) > last_uid]
            for uid in uids[:20]:
                _, msgdata = con.uid("fetch", str(uid), "(RFC822)")
                if not msgdata or msgdata[0] is None:
                    continue
                msg = email.message_from_bytes(msgdata[0][1])
                self._emit(addr, uid, msg)
                self.state[addr] = uid
            if uids:
                self._save_state()
        finally:
            try:
                con.logout()
            except Exception:  # noqa: BLE001
                pass

    def _poll_graph(self, acct):
        from . import msgraph
        addr = acct["email"]
        if not msgraph.connected(addr):
            self.status[addr] = "not connected — connect Microsoft 365 in settings"
            return
        last = self.state.get("graph:" + addr)
        seen = list(self.state.get("graph_seen:" + addr) or [])
        msgs, watermark = msgraph.fetch_new_messages(addr, last, seen)
        for m in msgs:
            self._emit_graph(addr, m)
            if m.get("id"):
                seen.append(m["id"])
        if msgs or watermark != last:
            self.state["graph:" + addr] = watermark
            self.state["graph_seen:" + addr] = seen[-80:]
            self._save_state()

    def _emit_graph(self, account, m):
        sender = (m.get("from") or {}).get("emailAddress") or {}
        sender_addr = (sender.get("address") or "").lower()
        subject = m.get("subject") or ""
        preview = re.sub(r"\s+", " ", m.get("bodyPreview") or "").strip()
        self._push_item(
            account=account,
            rowid=f"mail-{account}-{m.get('id', '')[-24:]}",
            when=m.get("receivedDateTime"),
            sender_addr=sender_addr,
            sender_name=sender.get("name") or "",
            subject=subject,
            preview=preview,
            message_id=m.get("internetMessageId"),
        )

    def _emit(self, account, uid, msg):
        sender_name, sender_addr = email.utils.parseaddr(msg.get("From", ""))
        try:
            dt = email.utils.parsedate_to_datetime(msg.get("Date", ""))
            when = dt.astimezone().isoformat()
        except (TypeError, ValueError):
            when = datetime.now(timezone.utc).astimezone().isoformat()
        self._push_item(
            account=account,
            rowid=f"mail-{account}-{uid}",
            when=when,
            sender_addr=(sender_addr or "").lower(),
            sender_name=_decode_header(sender_name),
            subject=_decode_header(msg.get("Subject", "")),
            preview=_body_preview(msg),
            message_id=(msg.get("Message-ID") or "").strip() or None,
        )

    def _push_item(self, account, rowid, when, sender_addr, sender_name,
                   subject, preview, message_id):
        from . import photos
        pid = crm.resolve_handle(sender_addr)
        person = crm._load()["by_id"].get(pid) if pid else None
        item = {
            "rowid": rowid,
            "when": when,
            "channel": "email",
            "account": account,
            "subject": subject,
            "message_id": message_id,
            "text": (subject + " — " + preview).strip(" —")[:500],
            "handle": sender_addr,
            "group": False,
            "group_name": None,
            "person_id": pid,
            "person_name": person["name"] if person else (
                sender_name or sender_addr),
            "known": pid is not None,
            "has_photo": bool(pid and photos.photo_path(pid)),
        }
        w = self.watcher
        with w.lock:
            # backstop against any refetch echo: one rowid, one feed item
            if any(x.get("rowid") == item["rowid"] for x in w.feed):
                return
            w.feed.append(item)
            w.feed.sort(key=lambda i: i.get("when") or "")
            w.feed = w.feed[-w.feed_size:]
            for q in list(w.listeners):
                try:
                    q.put_nowait(item)
                except Exception:  # noqa: BLE001
                    w.listeners.remove(q)
        from . import notify
        notify.maybe_notify(item)  # high-value senders ping the owner's phone
