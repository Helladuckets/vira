"""Message and mail BODIES as a searchable corpus — the fourth database.

The media index already covers everything shared as a file or a link.
What nobody could search was the conversation itself: 134,698 iMessage
rows and every email body, readable only by opening the thread they
live in. "What did we actually say about the lease in June" had no
answer anywhere in the app.

Two facts shape this module:

  - iMessage text is not in `message.text`. Modern macOS writes it into
    the `attributedBody` typedstream blob; on this corpus only 74 rows
    of 134,698 have a usable `text` column. Every row therefore pays a
    blob decode (imessage.msg_text), which is why the backfill is
    batched, watermarked and resumable rather than a single pass.
  - Keyword first, vectors later (the owner's call). FTS5 ships now and
    answers exact recall and every date-filtered question; `vec_text`
    and the `pending` flag are in the schema from the start so the
    embedding pass is purely additive and needs no migration.

Everything else is mediaindex's shape at a different scale: one sqlite
sidecar, deterministic filters in SQL before ranking, the shared
retrieval primitives for bm25 and fusion.
"""
import email
import email.utils
import imaplib
import json
import re
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import channels, crmindex
from . import data as crm
from . import mail as mailmod
from . import mediaindex, retrieval, settings
from .imessage import apple_ns, msg_text

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "text-index.sqlite"

BATCH = 2000          # rows per commit on the iMessage backfill
MIN_CHARS = 2
MAIL_BODY_MAX = 20000
GRAPH_PAGE = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT UNIQUE,
  source TEXT, account TEXT,
  chat_id INTEGER, is_group INTEGER, from_me INTEGER,
  sender_pid TEXT, chat_pid TEXT, sender_handle TEXT,
  date_ns INTEGER, subject TEXT, text TEXT,
  pending INTEGER DEFAULT 1);
CREATE INDEX IF NOT EXISTS items_date ON items(date_ns);
CREATE INDEX IF NOT EXISTS items_chat ON items(chat_pid);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, subject);
CREATE TABLE IF NOT EXISTS vec_text(seq INTEGER PRIMARY KEY, v BLOB);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, val TEXT);
"""


def _db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def available():
    if not DB.exists():
        return False
    con = _db()
    try:
        return con.execute("SELECT COUNT(*) FROM items").fetchone()[0] > 0
    finally:
        con.close()


def get_state(con, key, default=None):
    row = con.execute("SELECT val FROM state WHERE key=?", (key,)).fetchone()
    return row["val"] if row else default


def set_state(con, key, val):
    con.execute("INSERT OR REPLACE INTO state(key,val) VALUES(?,?)",
                (key, str(val)))


def _insert(con, *, uid, source, text, date_ns, account=None, chat_id=None,
            is_group=0, from_me=0, sender_pid=None, chat_pid=None,
            sender_handle=None, subject=None):
    cur = con.execute(
        "INSERT OR IGNORE INTO items(uid, source, account, chat_id, is_group,"
        " from_me, sender_pid, chat_pid, sender_handle, date_ns, subject,"
        " text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, source, account, chat_id, is_group, from_me, sender_pid,
         chat_pid, sender_handle, date_ns, subject, text))
    if cur.rowcount:
        con.execute("INSERT INTO fts(rowid, text, subject) VALUES(?,?,?)",
                    (cur.lastrowid, text, subject or ""))
    return cur.rowcount


# ---------- iMessage bodies ----------

def backfill_imessage(limit=None, log=print):
    """Every message body in chat.db, oldest first, resumable. The
    watermark is the message ROWID, so an interrupted run picks up
    exactly where it stopped."""
    con = _db()
    src = mediaindex._connect()
    total = 0
    try:
        chats, chat_pid, handles = mediaindex._chat_maps(src)
        while True:
            wm = int(get_state(con, "wm_message", 0))
            rows = src.execute(
                """SELECT m.ROWID, m.date, m.is_from_me, m.handle_id,
                          cmj.chat_id, m.text, m.attributedBody
                   FROM message m
                   JOIN chat_message_join cmj ON cmj.message_id=m.ROWID
                   WHERE m.ROWID > ? AND m.associated_message_type = 0
                   ORDER BY m.ROWID LIMIT ?""", (wm, BATCH)).fetchall()
            if not rows:
                break
            for (rid, date_ns, from_me, hrow, cid, text, blob) in rows:
                body = (msg_text(text, blob) or "").strip()
                if len(body) < MIN_CHARS:
                    continue
                style = chats.get(cid, (45, ""))[0]
                spid, handle = mediaindex._sender(from_me, hrow, handles)
                total += _insert(
                    con, uid=f"imsg:{rid}", source="imessage", text=body,
                    date_ns=date_ns, chat_id=cid, is_group=int(style == 43),
                    from_me=int(bool(from_me)), sender_pid=spid,
                    chat_pid=chat_pid.get(cid), sender_handle=handle)
            set_state(con, "wm_message", rows[-1][0])
            con.commit()
            log(f"  messages: {total} indexed (rowid {rows[-1][0]})")
            if limit and total >= limit:
                break
    finally:
        src.close()
        con.close()
    return total


# ---------- mail bodies ----------

def _strip_html(text):
    return mailmod.strip_html(text)


def _my_addresses():
    return {a["email"].lower() for a in channels.mail_accounts()}


def backfill_graph(account, since=None, limit=400, log=print):
    """M365/Outlook bodies through Graph. Unlike mailindex this does NOT
    filter on hasAttachments — the body IS the payload here."""
    from . import msgraph
    con = _db()
    n = 0
    try:
        my = _my_addresses()
        flt = f"receivedDateTime ge {since}" if since else ""
        path = ("/me/messages?" + (f"$filter={urllib.parse.quote(flt)}&"
                                   if flt else "")
                + f"$top={GRAPH_PAGE}&$select=id,subject,from,toRecipients,"
                  "receivedDateTime,body,internetMessageId")
        while path and n < limit:
            out = msgraph._graph_request(account, path)
            for m in out.get("value", []):
                frm = (m.get("from") or {}).get("emailAddress") or {}
                from_addr = (frm.get("address") or "").lower()
                body = (m.get("body") or {})
                content = body.get("content") or ""
                if (body.get("contentType") or "").lower() == "html":
                    content = _strip_html(content)
                content = re.sub(r"\s+", " ", content).strip()[:MAIL_BODY_MAX]
                if len(content) < MIN_CHARS:
                    continue
                try:
                    dt = datetime.fromisoformat(
                        (m.get("receivedDateTime") or "").replace("Z",
                                                                  "+00:00"))
                except ValueError:
                    dt = datetime.now(timezone.utc)
                from_me = from_addr in my
                n += _insert(
                    con, uid="mail:" + (m.get("internetMessageId")
                                        or m.get("id") or ""),
                    source="email", account=account, text=content,
                    subject=m.get("subject") or "",
                    date_ns=apple_ns(dt), from_me=int(from_me),
                    sender_pid="me" if from_me else crm.resolve_handle(
                        from_addr),
                    chat_pid=_counterpart(from_me, from_addr, m),
                    sender_handle=from_addr)
            con.commit()
            nxt = out.get("@odata.nextLink")
            path = (nxt[len(msgraph.GRAPH):]
                    if nxt and nxt.startswith(msgraph.GRAPH) else nxt or None)
            log(f"  mail {account}: {n} indexed")
    finally:
        con.close()
    return n


def _counterpart(from_me, from_addr, m):
    """The person this mail is WITH — the sender, or the first recipient
    who is not the owner."""
    if not from_me:
        return crm.resolve_handle(from_addr)
    for r in m.get("toRecipients", []):
        addr = ((r.get("emailAddress") or {}).get("address") or "").lower()
        pid = crm.resolve_handle(addr)
        if pid:
            return pid
    return None


def backfill_imap(acct, limit=400, log=print):
    """IMAP bodies, newest first, windowed by a per-account UID
    watermark. mail._body_preview already knows how to pull plain text
    out of a MIME tree (and to strip HTML when that is all there is), so
    it does the extraction here too, just with a bigger ceiling."""
    addr, host = acct["email"], acct.get("host", "")
    pw = mailmod.keychain_password(addr)
    if not pw:
        log(f"  imap {addr}: no keychain password, skipping")
        return 0
    con = _db()
    key = f"wm_mail:{addr}"
    n = 0
    try:
        wm = int(get_state(con, key, 0) or 0)
        imap = imaplib.IMAP4_SSL(host)
        try:
            imap.login(addr, pw)
            box = channels.imap_special_folder(imap, "\\All", "INBOX")
            imap.select(f'"{box}"', readonly=True)
            typ, data = imap.uid("search", None, "UID", f"{wm + 1}:*")
            if typ != "OK" or not data or data[0] is None:
                return 0
            uids = [int(u) for u in data[0].split()][:limit]
            my = _my_addresses()
            for uid in uids:
                _, md = imap.uid("fetch", str(uid), "(RFC822)")
                if not md or md[0] is None:
                    continue
                msg = email.message_from_bytes(md[0][1])
                body = mailmod._body_preview(msg, limit=MAIL_BODY_MAX)
                if len(body) < MIN_CHARS:
                    wm = max(wm, uid)
                    continue
                _, from_addr = email.utils.parseaddr(msg.get("From", ""))
                from_addr = (from_addr or "").lower()
                from_me = from_addr in my
                to_pid = None
                for _, a in email.utils.getaddresses(
                        [msg.get("To", "") or ""]):
                    to_pid = crm.resolve_handle((a or "").lower())
                    if to_pid:
                        break
                dt = email.utils.parsedate_to_datetime(msg.get("Date")) \
                    if msg.get("Date") else datetime.now(timezone.utc)
                n += _insert(
                    con, uid="mail:" + (msg.get("Message-ID")
                                        or f"{addr}:{uid}").strip(),
                    source="email", account=addr, text=body,
                    subject=mailmod._decode_header(msg.get("Subject") or ""),
                    date_ns=apple_ns(dt), from_me=int(from_me),
                    sender_pid="me" if from_me
                    else crm.resolve_handle(from_addr),
                    chat_pid=to_pid if from_me
                    else crm.resolve_handle(from_addr),
                    sender_handle=from_addr)
                wm = max(wm, uid)
            set_state(con, key, wm)
            con.commit()
            log(f"  mail {addr}: {n} indexed (uid {wm})")
        finally:
            try:
                imap.logout()
            except Exception:      # noqa: BLE001 — best-effort close
                pass
    finally:
        con.close()
    return n


def backfill_mail(limit=400, log=print):
    n = 0
    for acct in channels.mail_accounts():
        try:
            if acct.get("kind") == "graph" or acct.get("provider") == "graph":
                n += backfill_graph(acct["email"], limit=limit, log=log)
            else:
                n += backfill_imap(acct, limit=limit, log=log)
        except Exception as e:      # noqa: BLE001 — one bad mailbox never
            log(f"  mail {acct.get('email')}: {e}")     # stops the others
    return n


def backfill(limit=None, log=print):
    n = backfill_imessage(limit=limit, log=log)
    n += backfill_mail(log=log)
    return n


def incremental(log=lambda *a: None):
    """The background tick: new messages since the watermark, plus a
    bounded mail sweep."""
    n = backfill_imessage(limit=BATCH, log=log)
    if settings.get("mail_body_index"):
        n += backfill_mail(limit=100, log=log)
    return n


# ---------- search ----------

def _candidates(con, person=None, sender=None, direction=None, source=None,
                since=None, until=None):
    where, params = [], []
    if person:
        where.append("(chat_pid=? OR sender_pid=?)")
        params += [person, person]
    if sender:
        where.append("sender_pid=?")
        params.append(sender)
    if direction == "sent":
        where.append("from_me=1")
    elif direction == "received":
        where.append("from_me=0")
    if source:
        where.append("source=?")
        params.append(source)
    if since is not None:
        where.append("date_ns >= ?")
        params.append(since)
    if until is not None:
        where.append("date_ns < ?")
        params.append(until)
    if not where:
        return None
    rows = con.execute("SELECT seq FROM items WHERE " + " AND ".join(where),
                       params).fetchall()
    return {r["seq"] for r in rows}


def search(q=None, limit=20, person=None, sender=None, direction=None,
           source=None, since=None, until=None, order="relevance",
           exact=False, phrases=()):
    """bm25 over message and mail text, inside the deterministic filters.
    No vector layer yet (see the module docstring) — the group reports
    `mode` so the UI can say so rather than implying semantic recall."""
    if not DB.exists():
        return []
    con = _db()
    try:
        lo, hi = _ns(since), _ns(until)
        cand = _candidates(con, person, sender, direction, source, lo, hi)
        q = (q or "").strip()
        if not q:
            where = "" if cand is None else (
                "WHERE seq IN (%s)" % ",".join(map(str, cand))
                if cand else "WHERE 0")
            rows = con.execute(
                f"SELECT * FROM items {where} ORDER BY date_ns "
                + ("ASC" if order == "oldest" else "DESC")
                + " LIMIT ?", (limit,)).fetchall()
            return [_row(r) for r in rows]

        # a recency sort has to see every match, not the bm25 head: the
        # newest message about X is rarely the best-scoring one
        deep = max(limit * 4, 500 if order != "relevance" else 100)
        seqs = retrieval.rank_fts(con, q, cand, limit=deep, phrases=phrases)
        if not seqs:
            return []
        got = {r["seq"]: r for r in con.execute(
            "SELECT * FROM items WHERE seq IN (%s)"
            % ",".join("?" * len(seqs)), seqs)}
        rows = [got[s] for s in seqs if s in got]
        if order in ("recent", "oldest"):
            rows.sort(key=lambda r: r["date_ns"] or 0,
                      reverse=order == "recent")
        return [_row(r) for r in rows[:limit]]
    finally:
        con.close()


def _ns(iso):
    if iso is None:
        return None
    if isinstance(iso, (int, float)):
        return int(iso)
    from datetime import date as _d
    from datetime import time as _t
    try:
        return apple_ns(datetime.combine(_d.fromisoformat(str(iso)[:10]),
                                         _t.min))
    except ValueError:
        return None


def _row(r):
    c = crm._load()["by_id"]
    sender = "you" if r["from_me"] else None
    sp = c.get(r["sender_pid"] or "")
    if sp:
        sender = "you" if r["sender_pid"] == "me" else sp["name"]
    elif not r["from_me"]:
        sender = r["sender_handle"]
    owner = c.get(r["chat_pid"] or "")
    when = mediaindex.apple_dt(r["date_ns"])
    return {
        "seq": r["seq"], "source": r["source"], "account": r["account"],
        "text": (r["text"] or "")[:600], "subject": r["subject"],
        "sender": sender, "person": owner["name"] if owner else None,
        "person_id": r["chat_pid"], "chat_id": r["chat_id"],
        "is_group": bool(r["is_group"]), "from_me": bool(r["from_me"]),
        "when": when.isoformat() if when else None,
    }


def status():
    if not DB.exists():
        return {"available": False, "messages": 0, "emails": 0,
                "vectors": 0, "note": "not indexed yet"}
    con = _db()
    try:
        by_source = dict(con.execute(
            "SELECT source, COUNT(*) FROM items GROUP BY source").fetchall())
        return {
            "available": True,
            "messages": by_source.get("imessage", 0),
            "emails": by_source.get("email", 0),
            "vectors": con.execute(
                "SELECT COUNT(*) FROM vec_text").fetchone()[0],
            "watermark": get_state(con, "wm_message", 0),
            "mode": "fts",
        }
    finally:
        con.close()


class Indexer(threading.Thread):
    """Background maintainer for the two corpora the Find window added:
    new message bodies, and the CRM index's vector top-up. One thread
    rather than two — both are cheap, periodic, and never urgent."""

    def __init__(self, every=180):
        super().__init__(daemon=True, name="vira-text-indexer")
        self.every = every
        self._stop = threading.Event()

    def run(self):
        time.sleep(20)                  # let the server finish booting
        while not self._stop.is_set():
            try:
                incremental()
            except Exception:  # noqa: BLE001 — the indexer never dies
                pass
            try:
                crmindex.refresh()
                crmindex.embed_pending(limit=128)
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(self.every)

    def stop(self):
        self._stop.set()


if __name__ == "__main__":       # python -m server.textindex backfill [mail]
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "backfill":
        which = sys.argv[2] if len(sys.argv) > 2 else "all"
        if which in ("all", "imessage"):
            print("imessage:", backfill_imessage(log=print))
        if which in ("all", "mail"):
            print("mail:", backfill_mail(log=print))
    print(json.dumps(status(), indent=1))
