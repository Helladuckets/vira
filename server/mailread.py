"""Read a full email and reply to it — the feed's email cards become a
small mailbox surface instead of a caption.

The feed item carries everything needed to find the message again:
`account`, `rowid` (mail-<account>-<uid> for IMAP, a truncated Graph id
for M365 — unusable for lookup) and `message_id` (the RFC 5322
Message-ID / Graph internetMessageId, which IS usable everywhere).

Two backends, matching the watcher's split:

- Graph (M365): message looked up by internetMessageId, full body from
  /me/messages. REPLYING needs the Mail.Send delegated scope, which this
  tenant's "Vira" registration may not have consented yet (consent is an
  admin act in Entra — see CLAUDE.md's M365 consent model). When the
  scope is refused, the reply falls back to a ready-to-send reply DRAFT
  in the mailbox (Mail.ReadWrite covers createReply) and the response
  says so honestly — never a silent degrade dressed as a send.
- IMAP/SMTP (Gmail): message fetched from INBOX by UID, falling back to
  an All-Mail Message-ID search when it has been archived; replies go
  out over SMTP with the same app password, threaded via
  In-Reply-To/References. Gmail files SMTP-sent mail into Sent itself.

Every socket here carries a timeout: imaplib/smtplib default to NONE,
and one stalled read otherwise wedges the calling thread forever (the
textindex Indexer lost a week to exactly that).

VIRA_PASSIVE blocks the send outright — the send.py precedent; a test
clone must never act on the world.
"""
import email
import email.message
import email.utils
import html as htmllib
import imaplib
import os
import re
import smtplib
import urllib.error
import urllib.parse

from . import mail as mailmod
from . import msgraph

IMAP_TIMEOUT = 30
SMTP_TIMEOUT = 30
BODY_MAX = 400_000
SCOPE_SEND = "https://graph.microsoft.com/Mail.Send offline_access"

GRAPH_SELECT = ("id,subject,from,toRecipients,ccRecipients,replyTo,"
                "receivedDateTime,body,internetMessageId")


def _accounts():
    return mailmod.load_accounts()


def _account(addr):
    addr = (addr or "").strip().lower()
    for a in _accounts():
        if (a.get("email") or "").strip().lower() == addr:
            return a
    raise ValueError(f"unknown mail account: {addr or '(none)'}")


def _imap_uid(rowid, addr):
    """The INBOX UID out of a feed rowid (mail-<account>-<uid>)."""
    prefix = f"mail-{addr}-"
    if not (rowid or "").startswith(prefix):
        return None
    tail = rowid[len(prefix):]
    return int(tail) if tail.isdigit() else None


def re_subject(subject):
    subject = (subject or "").strip()
    return subject if re.match(r"(?i)^re:", subject) else "Re: " + subject


# ---------- body extraction (reading, not indexing: keep the lines) ----------

_PARA_RE = re.compile(r"(?i)<\s*(?:/p|/h[1-6]|/blockquote)[^>]*>")
_BLOCK_RE = re.compile(r"(?i)<\s*(?:br|/div|/tr|/li|/pre)[^>]*>")


def html_to_text(htm):
    """Email HTML -> readable text. mail.strip_html collapses everything
    to one line, which is right for previews and wrong for reading — the
    block-level closers become newlines first, so paragraphs survive
    (paragraph closers get a blank line; row/line closers a single one)."""
    htm = re.sub(r"(?is)<(style|script|head).*?</\1>", " ", htm or "")
    htm = _PARA_RE.sub("\n\n", htm)
    htm = _BLOCK_RE.sub("\n", htm)
    text = re.sub(r"<[^>]+>", " ", htm)
    text = htmllib.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:BODY_MAX]


def _decode_part(part):
    try:
        payload = part.get_payload(decode=True) or b""
    except Exception:  # noqa: BLE001 — malformed transfer encoding
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:      # a codec Python has no name for (unknown-8bit)
        return payload.decode("latin-1", errors="replace")


def message_bodies(msg):
    """(text, html) of a parsed message — the first non-attachment
    text/plain part with its line structure intact, plus the first
    text/html part when one exists (the panel's Original view)."""
    text, html_body = "", ""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if "attachment" in str(part.get("Content-Disposition", "")):
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and not text:
            text = _decode_part(part).strip()[:BODY_MAX]
        elif ctype == "text/html" and not html_body:
            html_body = _decode_part(part)[:BODY_MAX]
    if not text and html_body:
        text = html_to_text(html_body)
    return text, html_body


# ---------- fetch ----------

def _addr_list(pairs):
    return [a for _, a in email.utils.getaddresses(pairs) if a]


def _from_imap(msg, acct, uid):
    from_name, from_addr = email.utils.parseaddr(msg.get("From", ""))
    _, reply_to = email.utils.parseaddr(msg.get("Reply-To", ""))
    try:
        when = email.utils.parsedate_to_datetime(
            msg.get("Date", "")).astimezone().isoformat()
    except (TypeError, ValueError):
        when = None
    text, html_body = message_bodies(msg)
    return {
        "account": acct["email"], "kind": "imap", "imap_uid": uid,
        "subject": mailmod._decode_header(msg.get("Subject", "")),
        "from_name": mailmod._decode_header(from_name),
        "from_addr": (from_addr or "").lower(),
        "reply_to": (reply_to or "").lower() or None,
        "to": _addr_list([msg.get("To", "") or ""]),
        "cc": _addr_list([msg.get("Cc", "") or ""]),
        "when": when,
        "text": text, "html": html_body,
        "message_id": (msg.get("Message-ID") or "").strip() or None,
        "references": (msg.get("References") or "").strip() or None,
        "graph_id": None,
    }


def _fetch_imap(acct, rowid, message_id):
    addr, host = acct["email"], acct.get("host", "")
    pw = mailmod.keychain_password(addr)
    if not pw:
        raise RuntimeError(f"no password in keychain for {addr}")
    con = imaplib.IMAP4_SSL(host, timeout=IMAP_TIMEOUT)
    try:
        con.login(addr, pw)
        uid = _imap_uid(rowid, addr)
        boxes = ["INBOX"]
        if message_id:      # archived mail has left INBOX but keeps its id
            boxes.append(mailmod.channels.imap_special_folder(
                con, "\\All", "INBOX"))
        for box in boxes:
            con.select(f'"{box}"', readonly=True)
            if box == "INBOX" and uid:
                _, md = con.uid("fetch", str(uid), "(RFC822)")
                if md and md[0] is not None:
                    return _from_imap(
                        email.message_from_bytes(md[0][1]), acct, uid)
            if message_id:
                _, data = con.uid(
                    "search", None, f'(HEADER Message-ID "{message_id}")')
                uids = (data[0] or b"").split()
                if uids:
                    _, md = con.uid("fetch", uids[-1].decode(), "(RFC822)")
                    if md and md[0] is not None:
                        return _from_imap(
                            email.message_from_bytes(md[0][1]), acct,
                            int(uids[-1]))
        raise RuntimeError("message not found in the mailbox "
                           "(it may have been deleted)")
    finally:
        try:
            con.logout()
        except Exception:  # noqa: BLE001 — best-effort close
            pass


def _graph_lookup(addr, message_id, select=GRAPH_SELECT):
    filt = f"internetMessageId eq '{(message_id or '').replace(chr(39), chr(39) * 2)}'"
    q = ("/me/messages?$filter=" + urllib.parse.quote(filt)
         + "&$select=" + select)
    rows = msgraph._graph_request(addr, q).get("value", [])
    if not rows:
        raise RuntimeError("message not found in the mailbox "
                           "(it may have been deleted)")
    return rows[0]


def _fetch_graph(acct, message_id):
    addr = acct["email"]
    if not message_id:
        raise RuntimeError("this email predates message ids in the feed — "
                           "open the person's profile instead")
    m = _graph_lookup(addr, message_id)
    sender = (m.get("from") or {}).get("emailAddress") or {}
    reply_to = [(r.get("emailAddress") or {}).get("address")
                for r in (m.get("replyTo") or [])]
    body = m.get("body") or {}
    is_html = (body.get("contentType") or "").lower() == "html"
    content = body.get("content") or ""
    return {
        "account": addr, "kind": "graph", "imap_uid": None,
        "subject": m.get("subject") or "",
        "from_name": sender.get("name") or "",
        "from_addr": (sender.get("address") or "").lower(),
        "reply_to": (reply_to[0] or "").lower() if reply_to else None,
        "to": [(r.get("emailAddress") or {}).get("address", "")
               for r in (m.get("toRecipients") or [])],
        "cc": [(r.get("emailAddress") or {}).get("address", "")
               for r in (m.get("ccRecipients") or [])],
        "when": m.get("receivedDateTime"),
        "text": html_to_text(content) if is_html else content[:BODY_MAX],
        "html": content[:BODY_MAX] if is_html else "",
        "message_id": m.get("internetMessageId"),
        "references": None,      # Graph reply threads server-side
        "graph_id": m.get("id"),
    }


def get_message(account, rowid=None, message_id=None):
    acct = _account(account)
    if acct.get("type") == "graph":
        return _fetch_graph(acct, message_id)
    return _fetch_imap(acct, rowid, message_id)


# ---------- reply ----------

def smtp_host_for(acct):
    host = acct.get("smtp_host") or ""
    if host:
        return host
    imap_host = acct.get("host", "")
    if imap_host.startswith("imap."):
        return "smtp." + imap_host[len("imap."):]
    raise RuntimeError(
        f"can't derive an SMTP host from {imap_host!r} — add "
        '"smtp_host" to the account in data/mail-accounts.json')


def _comment_html(text):
    return htmllib.escape(text).replace("\n", "<br>")


def _reply_graph(acct, text, graph_id, message_id):
    addr = acct["email"]
    if not graph_id:
        graph_id = _graph_lookup(addr, message_id, select="id").get("id")
    comment = _comment_html(text)
    try:
        msgraph._graph_request(addr, f"/me/messages/{graph_id}/reply",
                               method="POST", payload={"comment": comment},
                               scope=SCOPE_SEND)
        return {"sent": True, "channel": "graph", "account": addr}
    except RuntimeError as e:
        if "AADSTS65001" not in str(e):
            raise
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Graph reply failed: HTTP {e.code}") from e
    # Mail.Send isn't admin-consented in the tenant — the honest degrade:
    # a ready-to-send reply draft (Mail.ReadWrite covers createReply),
    # with the one-time fix named so the owner can enable real sends.
    msgraph._graph_request(addr, f"/me/messages/{graph_id}/createReply",
                           method="POST", payload={"comment": comment})
    return {"sent": False, "drafted": True, "channel": "graph",
            "account": addr,
            "note": ("Sending from this mailbox needs the Mail.Send "
                     "permission, which isn't consented yet — your reply "
                     "was saved as a draft in Outlook instead. One-time "
                     "fix: Entra admin center > App registrations > Vira "
                     "> API permissions > add Microsoft Graph delegated "
                     "Mail.Send > Grant admin consent.")}


def _reply_smtp(acct, text, to, subject, message_id, references):
    addr = acct["email"]
    if not to:
        raise RuntimeError("no reply address on the original message")
    pw = mailmod.keychain_password(addr)
    if not pw:
        raise RuntimeError(f"no password in keychain for {addr}")
    msg = email.message.EmailMessage()
    msg["From"] = addr
    msg["To"] = to
    msg["Subject"] = re_subject(subject)
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid()
    if message_id:
        msg["In-Reply-To"] = message_id
        msg["References"] = ((references + " ") if references else "") + message_id
    msg.set_content(text)
    with smtplib.SMTP_SSL(smtp_host_for(acct), 465,
                          timeout=SMTP_TIMEOUT) as s:
        s.login(addr, pw)
        s.send_message(msg)
    return {"sent": True, "channel": "smtp", "account": addr, "to": to}


def send_reply(account, text, *, to=None, subject=None, message_id=None,
               references=None, graph_id=None):
    if os.environ.get("VIRA_PASSIVE"):
        raise RuntimeError("passive test instance — outbound email is "
                           "blocked here")
    if not (text or "").strip():
        raise RuntimeError("empty reply")
    acct = _account(account)
    if acct.get("type") == "graph":
        return _reply_graph(acct, text, graph_id, message_id)
    return _reply_smtp(acct, text, to, subject, message_id, references)
