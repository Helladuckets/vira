"""Shared media for a conversation: the links, photos, and documents exchanged
with a person over iMessage, both directions — the Vira mirror of the
Messages.app conversation-info panel (Photos / Links / Documents tabs).

Deterministic chat.db reads. Attachments are classified by mime type into
photos (images + videos) and documents; links come from URL-balloon messages
(payload_data is an NSKeyedArchiver bplist carrying the URL and the page
title) plus plain-text messages containing a URL. Thumbnails are generated
on demand with macOS-native tools (sips for images, ffmpeg for video first
frames) and cached in data/media-thumbs/; favicons for link rows are fetched
once per domain and cached in data/favicon-cache/ so the client never talks
to a third party directly.
"""
import bisect
import hashlib
import json
import plistlib
import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

from . import data as crm
from .imessage import _connect, apple_dt, msg_text

ATTACH_ROOT = Path.home() / "Library" / "Messages" / "Attachments"
_DATA = Path(__file__).resolve().parent.parent / "data"
THUMBS = _DATA / "media-thumbs"
FAVICONS = _DATA / "favicon-cache"
META = _DATA / "media-meta.json"          # attachment id -> {duration}

URL_RE = re.compile(r"https?://[^\s<>\"\)\]]+")
IMAGE_MIME = ("image/",)
VIDEO_MIME = ("video/",)
SKIP_NAMES = (".pluginpayloadattachment",)

_meta_lock = threading.Lock()


def _load_meta():
    try:
        return json.loads(META.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_meta(meta):
    META.parent.mkdir(parents=True, exist_ok=True)
    META.write_text(json.dumps(meta))


def _person_chat_ids(pid):
    """ROWIDs of this person's direct (1:1) chats."""
    p = crm._load()["by_id"].get(pid)
    if not p:
        return []
    handles = set(p.get("handles", {}).get("imessage", []))
    for ph in p.get("handles", {}).get("phones10", []):
        handles.add("+1" + ph)
    if not handles:
        return []
    qmarks = ",".join("?" * len(handles))
    con = _connect()
    try:
        return [r[0] for r in con.execute(
            f"""SELECT DISTINCT c.ROWID
                FROM chat c
                JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
                JOIN handle h ON h.ROWID = chj.handle_id
                WHERE h.id IN ({qmarks}) AND c.style = 45""",
            tuple(handles)).fetchall()]
    finally:
        con.close()


def _attachment_rows(chat_ids):
    qmarks = ",".join("?" * len(chat_ids))
    con = _connect()
    try:
        return con.execute(
            f"""SELECT a.ROWID, a.filename, a.mime_type, a.transfer_name,
                       a.total_bytes, m.is_from_me, m.date,
                       m.ROWID, m.text, m.attributedBody
                FROM attachment a
                JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
                JOIN message m ON m.ROWID = maj.message_id
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                WHERE cmj.chat_id IN ({qmarks})
                  AND a.filename IS NOT NULL
                  AND a.hide_attachment = 0 AND a.is_sticker = 0
                ORDER BY m.date DESC""",
            tuple(chat_ids)).fetchall()
    finally:
        con.close()


def _link_rows(chat_ids):
    qmarks = ",".join("?" * len(chat_ids))
    con = _connect()
    try:
        return con.execute(
            f"""SELECT m.ROWID, m.date, m.is_from_me, m.text,
                       m.attributedBody, m.payload_data
                FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                WHERE cmj.chat_id IN ({qmarks})
                  AND (m.balloon_bundle_id = 'com.apple.messages.URLBalloonProvider'
                       OR m.text LIKE '%http%')
                ORDER BY m.date DESC""",
            tuple(chat_ids)).fetchall()
    finally:
        con.close()


def _text_messages(chat_ids):
    """Date-sorted (date_ns, msg_rowid, from_me, text) for the person's
    direct threads — the haystack the per-item context search runs over.
    Unbounded: the full history decodes in ~0.3s even at largest-thread scale
    (58k messages), and old items deserve context too."""
    qmarks = ",".join("?" * len(chat_ids))
    con = _connect()
    try:
        rows = con.execute(
            f"""SELECT m.ROWID, m.date, m.is_from_me, m.text, m.attributedBody
                FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                WHERE cmj.chat_id IN ({qmarks})
                  AND (m.associated_message_type = 0
                       OR m.associated_message_type IS NULL)
                ORDER BY m.date DESC""",
            tuple(chat_ids)).fetchall()
    finally:
        con.close()
    out = []
    for rowid, date_ns, fm, text, blob in rows:
        t = msg_text(text, blob)
        if t and date_ns:
            out.append((date_ns, rowid, bool(fm), t))
    out.sort()
    return out


# how far around an item the accompanying message can sit: anything within
# 3 minutes counts; the same sender's words count out to 15 minutes (the
# sender asks at 11:23, the bill lands at 11:41 — different-sender chatter that
# stale would be noise, but the sender's own framing usually still applies)
_CTX_NEAR_NS = int(180 * 1e9)
_CTX_SAME_SENDER_NS = int(900 * 1e9)


def _nearest_context(texts, dates, date_ns, exclude_rowid, from_me):
    """The message sent along with an item: nearest text message around its
    timestamp, preferring the same sender."""
    if not texts or not date_ns:
        return None
    i = bisect.bisect_left(dates, date_ns)
    best = None
    for j in range(max(0, i - 8), min(len(texts), i + 8)):
        d_ns, rowid, fm, t = texts[j]
        if rowid == exclude_rowid:
            continue
        delta = abs(d_ns - date_ns)
        if delta > (_CTX_SAME_SENDER_NS if fm == from_me else _CTX_NEAR_NS):
            continue
        score = delta + (0 if fm == from_me else _CTX_NEAR_NS)
        if best is None or score < best[0]:
            best = (score, fm, t)
    if not best:
        return None
    return {"text": best[2][:220], "from_me": best[1], "own": False}


def _link_from_payload(payload):
    """(url, title) from a URL-balloon's LPLinkMetadata keyed-archive plist."""
    try:
        objs = plistlib.loads(payload).get("$objects", [])
    except Exception:  # noqa: BLE001 — malformed plist; caller falls back
        return None, None
    strs = [o for o in objs if isinstance(o, str) and o != "$null"]
    url = next((s for s in strs if s.startswith("http")), None)
    # the page title is the human string in the archive: not a URL, not a
    # class name, has some length; prefer ones with spaces
    cands = [s for s in strs
             if not s.startswith(("http", "NS", "LP", "$", "RichLink"))
             and len(s) > 3 and not re.fullmatch(r"[\d.:{}\-, ]+", s)]
    title = next((s for s in cands if " " in s.strip()),
                 cands[0] if cands else None)
    return url, title


def _domain(url):
    m = re.match(r"https?://([^/?#]+)", url)
    host = (m.group(1).lower() if m else "").split(":")[0]
    return host.removeprefix("www.")


def person_media(pid):
    """{photos, links, docs} shared in this person's direct conversation.
    Complete — every item in the thread's history, no caps (the client
    previews a slice and lazy-loads thumbnails, so big sets are fine)."""
    chat_ids = _person_chat_ids(pid)
    return media_for_chats(chat_ids)


def media_for_chats(chat_ids):
    """{photos, links, docs} shared across the given chat rows — the same
    pipeline as the direct conversation, scoped to a group thread."""
    if not chat_ids:
        return {"photos": [], "links": [], "docs": []}

    texts = _text_messages(chat_ids)
    dates = [t[0] for t in texts]

    photos, docs = [], []
    meta = _load_meta()
    for rowid, fname, mime, tname, size, from_me, date_ns, msg_rowid, \
            mtext, mblob in _attachment_rows(chat_ids):
        name = (tname or Path(fname).name or "")
        if name.lower().endswith(SKIP_NAMES):
            continue
        path = Path(fname).expanduser()
        if not path.exists():   # purged from the local attachment cache
            continue
        when = apple_dt(date_ns)
        caption = msg_text(mtext, mblob)
        if caption:  # sent in the same message as the attachment
            ctx = {"text": caption[:220], "from_me": bool(from_me), "own": True}
        else:
            ctx = _nearest_context(texts, dates, date_ns, msg_rowid,
                                   bool(from_me))
        entry = {
            "id": rowid,
            "name": name,
            "size": size or 0,
            "from_me": bool(from_me),
            "when": when.isoformat() if when else None,
            "context": ctx,
        }
        mime = mime or ""
        if mime.startswith(IMAGE_MIME):
            photos.append({**entry, "kind": "image"})
        elif mime.startswith(VIDEO_MIME):
            dur = (meta.get(str(rowid)) or {}).get("duration")
            photos.append({**entry, "kind": "video", "duration": dur})
        else:
            ext = Path(name).suffix.lstrip(".").upper() or "FILE"
            kind = "audio" if mime.startswith("audio/") else "doc"
            docs.append({**entry, "kind": kind, "ext": ext[:5]})

    links, seen = [], set()
    for rowid, date_ns, from_me, text, blob, payload in _link_rows(chat_ids):
        url = title = None
        t = msg_text(text, blob) or ""
        if payload:
            url, title = _link_from_payload(payload)
        if not url:
            m = URL_RE.search(t)
            url = m.group(0) if m else None
        if not url:
            continue
        key = url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        # words sent in the same message as the link (the text minus the
        # URL itself) beat neighboring-message context
        leftover = URL_RE.sub("", t).strip(" \n\t-—·|,;:")
        if leftover and len(leftover) > 2:
            ctx = {"text": leftover[:220], "from_me": bool(from_me),
                   "own": True}
        else:
            ctx = _nearest_context(texts, dates, date_ns, rowid,
                                   bool(from_me))
        when = apple_dt(date_ns)
        links.append({
            "id": rowid,
            "url": url,
            "title": (title or "").strip()[:140] or None,
            "domain": _domain(url),
            "from_me": bool(from_me),
            "when": when.isoformat() if when else None,
            "context": ctx,
        })

    return {"photos": photos, "links": links, "docs": docs}


def counts_for_chats(chat_ids):
    """{chat_id: {photos, links, docs}} across the given chat rows — the
    group list's per-thread media tallies. Photos = images + videos; links
    counts link-bearing messages (not deduped URLs)."""
    if not chat_ids:
        return {}
    out = {cid: {"photos": 0, "links": 0, "docs": 0} for cid in chat_ids}
    qmarks = ",".join("?" * len(chat_ids))
    con = _connect()
    try:
        # two cheap index/table scans joined here — the SQL three-way join
        # random-reads the whole message table and costs ~2s at the biggest contact's
        # 587 group chat rows; this shape is ~0.04s
        msg2chat = dict(con.execute(
            f"""SELECT message_id, chat_id FROM chat_message_join
                WHERE chat_id IN ({qmarks})""", tuple(chat_ids)).fetchall())
        for mid, mime, tname, fname in con.execute(
                """SELECT maj.message_id, a.mime_type, a.transfer_name,
                          a.filename
                   FROM message_attachment_join maj
                   JOIN attachment a ON a.ROWID = maj.attachment_id
                   WHERE a.filename IS NOT NULL
                     AND a.hide_attachment = 0 AND a.is_sticker = 0"""):
            cid = msg2chat.get(mid)
            if cid is None:
                continue
            name = (tname or Path(fname).name or "")
            if name.lower().endswith(SKIP_NAMES):
                continue
            mime = mime or ""
            kind = ("photos" if mime.startswith(IMAGE_MIME + VIDEO_MIME)
                    else "docs")
            out[cid][kind] += 1
        for cid, n in con.execute(
                f"""SELECT cmj.chat_id, COUNT(*)
                    FROM message m
                    JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                    WHERE cmj.chat_id IN ({qmarks})
                      AND (m.balloon_bundle_id =
                             'com.apple.messages.URLBalloonProvider'
                           OR m.text LIKE '%http%')
                    GROUP BY cmj.chat_id""",
                tuple(chat_ids)).fetchall():
            out[cid]["links"] = n
    finally:
        con.close()
    return out


# ---------- thread window (the viewer's virtual phone) ----------

def _anchor_for_attachment(att_id):
    """(message_rowid, date_ns) of the message that carried this attachment."""
    con = _connect()
    try:
        return con.execute(
            """SELECT m.ROWID, m.date FROM message m
               JOIN message_attachment_join maj ON maj.message_id = m.ROWID
               WHERE maj.attachment_id = ?""", (att_id,)).fetchone()
    finally:
        con.close()


def _window_messages(con, chat_ids, where, params, order, limit):
    qmarks = ",".join("?" * len(chat_ids))
    return con.execute(
        f"""SELECT m.ROWID, m.date, m.is_from_me, m.text, m.attributedBody,
                   h.id
            FROM message m
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            WHERE cmj.chat_id IN ({qmarks})
              AND (m.associated_message_type = 0
                   OR m.associated_message_type IS NULL)
              AND {where}
            ORDER BY m.date {order}, m.ROWID {order} LIMIT ?""",
        (*chat_ids, *params, limit)).fetchall()


def _msgs_payload(con, rows):
    """Rows -> viewer messages, each with its visible text and any media
    attachments (so photos render inline in the virtual phone)."""
    ids = [r[0] for r in rows]
    atts = {}
    if ids:
        q = ",".join("?" * len(ids))
        for mid, aid, mime, tname, fname in con.execute(
                f"""SELECT maj.message_id, a.ROWID, a.mime_type,
                           a.transfer_name, a.filename
                    FROM message_attachment_join maj
                    JOIN attachment a ON a.ROWID = maj.attachment_id
                    WHERE maj.message_id IN ({q})
                      AND a.filename IS NOT NULL
                      AND a.hide_attachment = 0 AND a.is_sticker = 0""",
                ids).fetchall():
            name = (tname or Path(fname).name or "")
            if name.lower().endswith(SKIP_NAMES):
                continue
            mime = mime or ""
            kind = ("image" if mime.startswith(IMAGE_MIME)
                    else "video" if mime.startswith(VIDEO_MIME) else "file")
            atts.setdefault(mid, []).append(
                {"id": aid, "kind": kind, "name": name})
    out = []
    c = crm._load()
    for rowid, date_ns, fm, text, blob, handle in rows:
        t = msg_text(text, blob)
        a = atts.get(rowid, [])
        if not t and not a:
            continue
        when = apple_dt(date_ns)
        sender = None
        if not fm and handle:
            spid = crm.resolve_handle(handle)
            sp = c["by_id"].get(spid) if spid else None
            sender = sp["name"] if sp else handle
        out.append({
            "rowid": rowid,
            "when": when.isoformat() if when else None,
            "from_me": bool(fm),
            "text": t,
            "sender": sender,
            "attachments": a,
        })
    return out


def _cursor_date(con, chat_ids, rowid):
    row = con.execute("SELECT date FROM message WHERE ROWID = ?",
                      (rowid,)).fetchone()
    return row[0] if row else None


def thread_window(pid, att_id, before=60, after=40,
                  before_rowid=None, after_rowid=None, chat_ids=None):
    """Messages around the attachment's message. Default scope is the
    person's direct conversation; pass chat_ids to scope to a group
    thread instead. Initial call returns the window centered on the
    anchor; before_rowid / after_rowid page further back / forward."""
    if not chat_ids:
        chat_ids = _person_chat_ids(pid)
    if not chat_ids:
        return None
    anchor = _anchor_for_attachment(att_id)
    if not anchor:
        return None
    anchor_rowid, anchor_date = anchor
    con = _connect()
    try:
        if before_rowid is not None:
            d = _cursor_date(con, chat_ids, before_rowid)
            rows = _window_messages(
                con, chat_ids,
                "(m.date < ? OR (m.date = ? AND m.ROWID < ?))",
                (d, d, before_rowid), "DESC", before)
            msgs = _msgs_payload(con, list(reversed(rows)))
            return {"messages": msgs, "has_more_older": len(rows) >= before}
        if after_rowid is not None:
            d = _cursor_date(con, chat_ids, after_rowid)
            rows = _window_messages(
                con, chat_ids,
                "(m.date > ? OR (m.date = ? AND m.ROWID > ?))",
                (d, d, after_rowid), "ASC", after)
            msgs = _msgs_payload(con, rows)
            return {"messages": msgs, "has_more_newer": len(rows) >= after}
        older = _window_messages(
            con, chat_ids,
            "(m.date < ? OR (m.date = ? AND m.ROWID <= ?))",
            (anchor_date, anchor_date, anchor_rowid), "DESC", before)
        newer = _window_messages(
            con, chat_ids,
            "(m.date > ? OR (m.date = ? AND m.ROWID > ?))",
            (anchor_date, anchor_date, anchor_rowid), "ASC", after)
        msgs = _msgs_payload(con, list(reversed(older)) + list(newer))
        return {
            "anchor_rowid": anchor_rowid,
            "anchor_att": att_id,
            "messages": msgs,
            "has_more_older": len(older) >= before,
            "has_more_newer": len(newer) >= after,
        }
    finally:
        con.close()


# ---------- files + thumbnails ----------

def attachment_path(att_id):
    """Resolved on-disk path for an attachment, or None. Only paths under
    ~/Library/Messages/Attachments are ever served."""
    con = _connect()
    try:
        row = con.execute(
            "SELECT filename, mime_type, transfer_name FROM attachment "
            "WHERE ROWID = ?", (att_id,)).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        return None, None, None
    path = Path(row[0]).expanduser().resolve()
    try:
        path.relative_to(ATTACH_ROOT.resolve())
    except ValueError:
        return None, None, None
    if not path.exists():
        return None, None, None
    return path, row[1] or "application/octet-stream", row[2] or path.name


def indexed_file(att_id):
    """(path, mime, name) for an email attachment carried in the media
    index, or (None, None, None). Only files under data/mail-attachments
    are ever served — the same containment check the chat.db path gets."""
    from . import mediaindex
    con = mediaindex._db()
    try:
        row = con.execute(
            "SELECT path, mime, name FROM items "
            "WHERE id=? AND source='email'", (att_id,)).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        return None, None, None
    path = Path(row[0]).expanduser().resolve()
    root = (_DATA / "mail-attachments").resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None, None, None
    if not path.exists():
        return None, None, None
    return path, row[1] or "application/octet-stream", row[2] or path.name


def _resolve_media(att_id):
    """Path/mime/name from chat.db first, then the email index."""
    path, mime, name = attachment_path(att_id)
    if path:
        return path, mime, name
    return indexed_file(att_id)


def _cache_key(path, suffix):
    h = hashlib.md5(f"{path}|{path.stat().st_mtime}|{suffix}".encode()).hexdigest()
    return THUMBS / f"{h}.jpg"


def _probe_duration(path):
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20)
        return round(float(res.stdout.strip()))
    except Exception:  # noqa: BLE001 — no duration is fine
        return None


def thumbnail(att_id, size=480):
    """Cached JPEG thumbnail for an image or video attachment. Returns the
    cache path or None. Video thumbs also record duration into media-meta."""
    path, mime, _name = _resolve_media(att_id)
    if not path:
        return None
    out = _cache_key(path, f"thumb{size}")
    if out.exists():
        return out
    THUMBS.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.jpg")
    try:
        if (mime or "").startswith("video/"):
            subprocess.run(
                ["ffmpeg", "-y", "-v", "quiet", "-ss", "0.3", "-i", str(path),
                 "-frames:v", "1", "-vf", f"scale={size}:-2", str(tmp)],
                capture_output=True, timeout=30)
            dur = _probe_duration(path)
            if dur is not None:
                with _meta_lock:
                    meta = _load_meta()
                    meta[str(att_id)] = {"duration": dur}
                    _save_meta(meta)
        else:
            subprocess.run(
                ["sips", "-s", "format", "jpeg", "--resampleHeightWidthMax",
                 str(size), str(path), "--out", str(tmp)],
                capture_output=True, timeout=30)
        if tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(out)
            return out
    except Exception:  # noqa: BLE001 — thumbnail is best-effort
        pass
    tmp.unlink(missing_ok=True)
    return None


def preview_file(att_id):
    """(path, media_type, filename) for viewing in a browser. HEIC/TIFF get a
    cached full-size JPEG conversion (Chrome can't render HEIC natively)."""
    path, mime, name = _resolve_media(att_id)
    if not path:
        return None, None, None
    if mime in ("image/heic", "image/heif", "image/tiff"):
        out = _cache_key(path, "full")
        if not out.exists():
            THUMBS.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".tmp.jpg")
            subprocess.run(
                ["sips", "-s", "format", "jpeg", str(path), "--out", str(tmp)],
                capture_output=True, timeout=60)
            if tmp.exists() and tmp.stat().st_size > 0:
                tmp.replace(out)
            else:
                tmp.unlink(missing_ok=True)
                return path, mime, name
        return out, "image/jpeg", Path(name).stem + ".jpg"
    return path, mime, name


# ---------- favicons (server-side fetch + cache; the client never leaves
# localhost for these) ----------

_FAV_SOURCES = (
    "https://icons.duckduckgo.com/ip3/{d}.ico",
    "https://www.google.com/s2/favicons?domain={d}&sz=64",
)


def favicon(domain):
    """Cached favicon bytes path for a domain, or None."""
    domain = re.sub(r"[^a-z0-9.\-]", "", (domain or "").lower())[:80]
    if not domain or "." not in domain:
        return None
    FAVICONS.mkdir(parents=True, exist_ok=True)
    hit = next(FAVICONS.glob(domain + ".*"), None)
    if hit:
        return hit if hit.stat().st_size > 0 else None  # empty = known miss
    for src in _FAV_SOURCES:
        try:
            req = urllib.request.Request(
                src.format(d=domain), headers={"user-agent": "Vira/1.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = r.read()
            if len(data) > 50:  # tiny bodies are placeholder 1x1s
                ext = ".png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ".ico"
                out = FAVICONS / (domain + ext)
                out.write_bytes(data)
                return out
        except Exception:  # noqa: BLE001 — try the next source
            continue
    (FAVICONS / (domain + ".miss")).write_bytes(b"")  # cache the miss
    return None
