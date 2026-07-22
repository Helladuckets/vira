"""Email attachments into the same local semantic-search index as iMessage.

The "all in one place" half that the media index was missing: every file
the owner has ever emailed or been emailed — Outlook/M365 (via the Graph API,
same token as the mail watcher) AND Gmail/generic IMAP (walking the
mailbox's MIME parts) — is downloaded to data/mail-attachments/ and
inserted into media-index.sqlite as a normal item (source='email'). From
there the existing content stages pick it up untouched: pdftotext/textutil
extract document text, Apple Vision OCRs photos, SigLIP embeds scenes,
InsightFace matches identities, nomic embeds the composed text. So an
Outlook deck or a Gmail PDF is searchable by the same query box as a photo
someone iMessaged.

Nothing leaves the machine — the attachment bytes land in a local cache
and the index is the same on-device sidecar as before.

Design notes:
  - Item identity is a stable synthetic id (ID_BASE | hash(account,
    message-id, attachment-key)) so INSERT OR IGNORE dedupes across runs
    and the value never collides with a chat.db ROWID (those are < 1e7;
    ours are > 4.5e15).
  - date_ns is stored in the Apple-epoch nanoseconds the rest of the index
    uses, so search.apple_dt renders email items the same as iMessage ones.
  - chat_pid is the counterpart (sender for received mail, the first known
    recipient for sent mail) so person-scoped search catches both
    directions; sender_pid is 'me' for outbound.
  - Watermarks live in the index state table: mail_wm:<account> is an ISO
    receivedDateTime for Graph accounts, the last All-Mail UID for IMAP.

CLI:  .venv/bin/python -m server.mailindex backfill
          [--account ADDR] [--since YYYY-MM-DD] [--limit N]
      .venv/bin/python -m server.mailindex status
  (run the content stages afterward: python -m server.mediaindex backfill)
"""
import base64
import email
import email.utils
import hashlib
import imaplib
import mimetypes
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import channels
from . import data as crm
from . import mail as mailmod
from . import mediaindex
from .imessage import APPLE_EPOCH

_DATA = Path(__file__).resolve().parent.parent / "data"
ATTACH_DIR = _DATA / "mail-attachments"

ID_BASE = 1 << 52                 # keeps synthetic ids clear of chat.db ROWIDs
MIN_IMAGE_BYTES = 4000            # below this an "image" is a signature/pixel
MAIL_SKIP_EXT = (".ics", ".vcs", ".p7s", ".asc")   # invites / crypto sig noise
GRAPH_PAGE = 25


# ---------- helpers ----------

def _accounts():
    return channels.mail_accounts(mailmod.ACCOUNTS)


def _my_addresses():
    return {a["email"].lower() for a in _accounts() if a.get("email")}


def _apple_ns(dt):
    """Aware datetime -> Apple-epoch nanoseconds (index convention)."""
    return int((dt.timestamp() - APPLE_EPOCH) * 1e9)


def _synth_id(account, msg_id, key):
    h = hashlib.sha1(f"{account}|{msg_id}|{key}".encode()).hexdigest()
    return ID_BASE | (int(h[:12], 16) & ((1 << 48) - 1))


def _classify(mime, name):
    return mediaindex._classify(mime or "", name or "")


def _ext_for(name, mime):
    ext = Path(name or "").suffix
    if ext:
        return ext
    return mimetypes.guess_extension(mime or "") or ".bin"


def _safe(account):
    return re.sub(r"[^A-Za-z0-9._@-]", "_", account or "acct")


def _counterpart(from_me, from_addr, to_addrs):
    """chat_pid for person-scoped search: the resolved other party."""
    if not from_me:
        return crm.resolve_handle(from_addr)
    for a in to_addrs:
        pid = crm.resolve_handle(a)
        if pid:
            return pid
    return None


def _skip(kind, name, size, inline):
    if inline:
        return True
    if Path(name or "").suffix.lower() in MAIL_SKIP_EXT:
        return True
    if (name or "").lower().endswith(mediaindex.SKIP_NAMES):
        return True
    if kind == "photo" and (size or 0) < MIN_IMAGE_BYTES:
        return True
    return False


def _store_and_insert(con, *, account, mime, name, data, synth_id, date_dt,
                      from_me, sender_pid, sender_handle, chat_pid,
                      subject, preview):
    """Write the attachment to the local cache and insert one index row.
    Returns 1 if newly inserted, 0 if it was already there."""
    kind = _classify(mime, name)
    dest = ATTACH_DIR / _safe(account) / f"{synth_id}{_ext_for(name, mime)}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_bytes(data)
    cur = con.execute(
        """INSERT OR IGNORE INTO items(kind,id,source,account,is_group,
           sender_pid,sender_handle,from_me,date_ns,name,mime,size,path,
           chat_pid,purged)
           VALUES(?,?,'email',?,0,?,?,?,?,?,?,?,?,?,0)""",
        (kind, synth_id, account, sender_pid, sender_handle, int(from_me),
         _apple_ns(date_dt), name, mime or "", len(data), str(dest),
         chat_pid))
    if not cur.rowcount:
        return 0
    seq = cur.lastrowid
    ctx = ((subject or "").strip() + " — " + (preview or "").strip())
    ctx = ctx.strip(" —")[:600]
    con.execute(
        "INSERT OR REPLACE INTO content(seq,context,ctx_from_me,title) "
        "VALUES(?,?,?,?)",
        (seq, ctx, int(from_me), (subject or "").strip()[:140]))
    mediaindex.refresh_fts(con, seq)
    return 1


# ---------- Graph (Outlook / M365) ----------

def _graph_messages(email_addr, since_iso=None, log=print):
    from . import msgraph
    flt = "hasAttachments eq true"
    if since_iso:
        flt += f" and receivedDateTime ge {since_iso}"
    path = ("/me/messages?$filter=" + urllib.parse.quote(flt)
            + f"&$top={GRAPH_PAGE}&$select=id,subject,from,toRecipients,"
              "receivedDateTime,bodyPreview,internetMessageId")
    while path:
        out = msgraph._graph_request(email_addr, path)
        for m in out.get("value", []):
            yield m
        nxt = out.get("@odata.nextLink")
        if nxt and nxt.startswith(msgraph.GRAPH):
            path = nxt[len(msgraph.GRAPH):]
        else:
            path = nxt or None


def _graph_attachment_bytes(email_addr, msg_id, att):
    from . import msgraph
    cb = att.get("contentBytes")
    if cb:
        return base64.b64decode(cb)
    # large attachments omit contentBytes — fetch the raw stream
    return msgraph.get_bytes(
        email_addr, f"/me/messages/{msg_id}/attachments/{att['id']}/$value")


def _run_graph(email_addr, mode, since, limit, log):
    from . import msgraph
    if not msgraph.connected(email_addr):
        log(f"  graph {email_addr}: not connected, skipping")
        return 0
    con = mediaindex._db()
    key = f"mail_wm:{email_addr}"
    wm = mediaindex.get_state(con, key)
    since_iso = None
    if mode == "incremental":
        if wm is None:                     # baseline: don't walk all history
            mediaindex.set_state(
                con, key,
                channels.graph_newest_received(email_addr, "/me/messages/"))
            con.close()
            return 0
        since_iso = wm
    elif since:
        since_iso = since + "T00:00:00Z"

    my = _my_addresses()
    n = 0
    newest = wm or ""
    for m in _graph_messages(email_addr, since_iso, log=log):
        received = m.get("receivedDateTime") or ""
        if received > newest:
            newest = received
        frm = ((m.get("from") or {}).get("emailAddress") or {})
        from_addr = (frm.get("address") or "").lower()
        to_addrs = [((r.get("emailAddress") or {}).get("address") or "").lower()
                    for r in m.get("toRecipients", [])]
        from_me = from_addr in my
        sender_pid = "me" if from_me else crm.resolve_handle(from_addr)
        chat_pid = _counterpart(from_me, from_addr, to_addrs)
        try:
            dt = datetime.fromisoformat(received.replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.now(timezone.utc)
        subject = m.get("subject") or ""
        preview = re.sub(r"\s+", " ", m.get("bodyPreview") or "").strip()
        atts = msgraph._graph_request(
            email_addr, f"/me/messages/{m['id']}/attachments").get("value", [])
        for att in atts:
            if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue
            name = att.get("name") or ""
            mime = att.get("contentType") or ""
            size = att.get("size") or 0
            if _skip(_classify(mime, name), name, size, att.get("isInline")):
                continue
            synth = _synth_id(email_addr, m.get("internetMessageId") or m["id"],
                              att.get("id") or name)
            if con.execute("SELECT 1 FROM items WHERE id=?",
                           (synth,)).fetchone():
                continue
            try:
                data = _graph_attachment_bytes(email_addr, m["id"], att)
            except Exception as e:  # noqa: BLE001 — one bad attachment
                log(f"  graph att fetch failed ({name}): {e}")
                continue
            n += _store_and_insert(
                con, account=email_addr, mime=mime, name=name, data=data,
                synth_id=synth, date_dt=dt, from_me=from_me,
                sender_pid=sender_pid, sender_handle=from_addr,
                chat_pid=chat_pid, subject=subject, preview=preview)
        con.commit()
        if limit and n >= limit:
            break
    if newest:
        mediaindex.set_state(con, key, newest)
    con.commit()
    con.close()
    log(f"  graph {email_addr}: +{n} attachments")
    return n


# ---------- IMAP (Gmail / generic) ----------

def _all_mail_folder(con):
    """The \\All special-use mailbox (Gmail's "All Mail"); else INBOX."""
    return channels.imap_special_folder(con, "\\All", "INBOX")


def _imap_search(con, gmail, since_uid, since_date):
    """UIDs of messages with attachments, windowed by watermark/date."""
    criteria = []
    if since_uid:
        criteria += ["UID", f"{since_uid + 1}:*"]
    if gmail:
        raw = "has:attachment"
        if since_date and not since_uid:
            raw += " after:" + since_date.replace("-", "/")
        criteria += ["X-GM-RAW", f'"{raw}"']
    elif not criteria:
        criteria = ["ALL"]
    typ, data = con.uid("search", None, *criteria)
    if typ != "OK" or not data or data[0] is None:
        return []
    return [int(u) for u in data[0].split()]


def _msg_attachments(msg):
    """Yield (name, mime, bytes, inline) for each real attachment part."""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        fn = part.get_filename()
        cd = str(part.get("Content-Disposition") or "")
        if not fn and "attachment" not in cd.lower():
            continue
        inline = "inline" in cd.lower() and "attachment" not in cd.lower()
        try:
            data = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001
            data = None
        if not data:
            continue
        name = mailmod._decode_header(fn) if fn else "attachment"
        yield name, (part.get_content_type() or ""), data, inline


def _run_imap(acct, mode, since, limit, log):
    addr, host = acct["email"], acct.get("host", "")
    pw = mailmod.keychain_password(addr)
    if not pw:
        log(f"  imap {addr}: no keychain password, skipping")
        return 0
    gmail = "gmail" in host.lower() or "google" in host.lower()
    idx = mediaindex._db()
    key = f"mail_wm:{addr}"
    wm = mediaindex.get_state(idx, key)
    since_uid = int(wm) if (mode == "incremental" and wm) else None

    con = imaplib.IMAP4_SSL(host)
    n = 0
    max_uid = int(wm) if wm else 0
    try:
        con.login(addr, pw)
        box = _all_mail_folder(con)
        con.select(f'"{box}"', readonly=True)
        if mode == "incremental" and wm is None:      # baseline, no history
            mediaindex.set_state(
                idx, key, channels.imap_newest_uid(con, f'"{box}"'))
            idx.close()
            return 0
        uids = _imap_search(con, gmail, since_uid, since if mode != "incremental"
                            else None)
        my = _my_addresses()
        for uid in uids:
            if uid > max_uid:
                max_uid = uid
            _, md = con.uid("fetch", str(uid), "(RFC822)")
            if not md or md[0] is None:
                continue
            msg = email.message_from_bytes(md[0][1])
            msg_id = (msg.get("Message-ID") or f"uid{uid}").strip()
            from_name, from_addr = email.utils.parseaddr(msg.get("From", ""))
            from_addr = (from_addr or "").lower()
            to_addrs = [a.lower() for _, a in
                        email.utils.getaddresses(msg.get_all("To", []))]
            from_me = from_addr in my
            sender_pid = "me" if from_me else crm.resolve_handle(from_addr)
            chat_pid = _counterpart(from_me, from_addr, to_addrs)
            try:
                dt = email.utils.parsedate_to_datetime(msg.get("Date", ""))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                dt = datetime.now(timezone.utc)
            subject = mailmod._decode_header(msg.get("Subject", ""))
            preview = mailmod._body_preview(msg)
            for idxn, (name, mime, data, inline) in enumerate(
                    _msg_attachments(msg)):
                if _skip(_classify(mime, name), name, len(data), inline):
                    continue
                synth = _synth_id(addr, msg_id, f"{idxn}:{name}")
                if idx.execute("SELECT 1 FROM items WHERE id=?",
                               (synth,)).fetchone():
                    continue
                n += _store_and_insert(
                    idx, account=addr, mime=mime, name=name, data=data,
                    synth_id=synth, date_dt=dt, from_me=from_me,
                    sender_pid=sender_pid, sender_handle=from_addr,
                    chat_pid=chat_pid, subject=subject, preview=preview)
            idx.commit()
            if limit and n >= limit:
                break
    finally:
        try:
            con.logout()
        except Exception:  # noqa: BLE001
            pass
    if max_uid:
        mediaindex.set_state(idx, key, max_uid)
    idx.commit()
    idx.close()
    log(f"  imap {addr}: +{n} attachments")
    return n


# ---------- entry points ----------

def _run_account(acct, mode, since, limit, log):
    if acct.get("type") == "graph":
        return _run_graph(acct["email"], mode, since, limit, log)
    return _run_imap(acct, mode, since, limit, log)


def backfill(account=None, since=None, limit=None, full=True, log=print):
    """Walk mailbox history for attachments and index them. `full`
    ignores the watermark (re-scan everything, deduped by synthetic id);
    `since`/`limit` bound a run for quick verification."""
    mode = "full" if full else "incremental"
    total = 0
    for acct in _accounts():
        if account and acct.get("email") != account:
            continue
        try:
            total += _run_account(acct, mode, since, limit, log)
        except Exception as e:  # noqa: BLE001 — keep other accounts going
            log(f"  {acct.get('email')}: {e}")
    log(f"email: +{total} attachments indexed")
    return total


def incremental(log=lambda *a: None):
    """Fetch only what is new since each account's watermark. First run
    per account baselines to now (history is the CLI backfill's job) so
    the in-server indexer never blocks on a huge first walk."""
    total = 0
    for acct in _accounts():
        try:
            total += _run_account(acct, "incremental", None, None, log)
        except Exception as e:  # noqa: BLE001
            log(f"  incremental {acct.get('email')}: {e}")
    return total


def status():
    con = mediaindex._db()
    out = {"email_items": con.execute(
        "SELECT COUNT(*) FROM items WHERE source='email'").fetchone()[0]}
    for k, in con.execute(
            "SELECT DISTINCT kind FROM items WHERE source='email'"):
        out[f"email_{k}"] = con.execute(
            "SELECT COUNT(*) FROM items WHERE source='email' AND kind=?",
            (k,)).fetchone()[0]
    for acct in _accounts():
        addr = acct["email"]
        out.setdefault("accounts", {})[addr] = {
            "items": con.execute(
                "SELECT COUNT(*) FROM items WHERE source='email' AND account=?",
                (addr,)).fetchone()[0],
            "watermark": mediaindex.get_state(con, f"mail_wm:{addr}"),
        }
    con.close()
    return out


if __name__ == "__main__":
    import json
    args = sys.argv[1:]

    def _opt(flag):
        return args[args.index(flag) + 1] if flag in args else None

    if args and args[0] == "status":
        print(json.dumps(status(), indent=2))
    elif args and args[0] == "backfill":
        limit = _opt("--limit")
        backfill(account=_opt("--account"), since=_opt("--since"),
                 limit=int(limit) if limit else None, full=True)
    else:
        print("usage: python -m server.mailindex "
              "backfill [--account ADDR] [--since YYYY-MM-DD] [--limit N] "
              "| status")
