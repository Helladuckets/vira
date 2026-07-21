"""Local search index over everything ever shared in iMessage — photos,
videos, documents, voice memos, and links, across every conversation.

The index is a sidecar SQLite file (data/media-index.sqlite): fully
regenerable from chat.db + the attachment files, never the source of
truth. Retrieval is hybrid — the layers cover each other's blind spots:

  - metadata + the message each item rode in with (context), exact-match
    searchable via FTS5
  - OCR text from every photo (Apple Vision, on-device)
  - scene embeddings (SigLIP2 running locally on MPS) so plain-English
    descriptions find unlabeled photos
  - doc text (pdftotext / textutil / pptx XML) + content hashes for
    "did I already send this exact file"
  - face identity vectors (InsightFace, local) matched against a gallery
    seeded from the CRM contact-photo cache — "photos of <person>"
  - captions (local VLM via Ollama) and A/V transcripts (mlx-whisper),
    backfilled opportunistically

Everything runs on this machine; nothing leaves it. Vector search is
brute-force numpy over in-memory matrices — at ~12k items that's
milliseconds, no vector-db extension needed.

Backfill is stage-tagged and resumable (state table); the incremental
thread in main.py indexes new items within ~30s of arrival using the
same stage functions past ROWID watermarks.

CLI:  .venv/bin/python -m server.mediaindex backfill [--stage NAME]
      .venv/bin/python -m server.mediaindex status | reconcile
"""
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

from . import data as crm
from .imessage import _connect, apple_dt, msg_text
from .media import (SKIP_NAMES, URL_RE, IMAGE_MIME, VIDEO_MIME,
                    _link_from_payload, _domain, _nearest_context,
                    _text_messages, thumbnail)

_DATA = Path(__file__).resolve().parent.parent / "data"
DB = _DATA / "media-index.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL,          -- photo | video | doc | audio | link
  id         INTEGER NOT NULL,       -- attachment ROWID; message ROWID for links
  chat_id    INTEGER,
  chat_pid   TEXT,                   -- person id when the chat is 1:1
  is_group   INTEGER DEFAULT 0,
  msg_rowid  INTEGER,
  sender_pid TEXT,                   -- 'me' when from_me, else resolved handle
  sender_handle TEXT,
  from_me    INTEGER,
  date_ns    INTEGER,
  name       TEXT,
  mime       TEXT,
  size       INTEGER,
  path       TEXT,
  sha256     TEXT,
  purged     INTEGER DEFAULT 0,
  source     TEXT DEFAULT 'imessage',   -- imessage | email
  account    TEXT,                      -- mailbox address for email items
  UNIQUE(kind, id)
);
CREATE INDEX IF NOT EXISTS idx_items_chat ON items(chat_pid);
CREATE INDEX IF NOT EXISTS idx_items_sender ON items(sender_pid);
CREATE INDEX IF NOT EXISTS idx_items_date ON items(date_ns);

CREATE TABLE IF NOT EXISTS content(
  seq        INTEGER PRIMARY KEY,
  ocr        TEXT DEFAULT '',
  caption    TEXT DEFAULT '',
  context    TEXT DEFAULT '',
  ctx_from_me INTEGER,
  doc_text   TEXT DEFAULT '',
  transcript TEXT DEFAULT '',
  title      TEXT DEFAULT '',
  url        TEXT DEFAULT '',
  domain     TEXT DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  name, title, domain, context, ocr, caption, doc_text, transcript,
  tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS vec_scene(   -- SigLIP image space
  seq INTEGER PRIMARY KEY, v BLOB
);
CREATE TABLE IF NOT EXISTS vec_text(    -- nomic text space, chunked
  seq INTEGER, chunk INTEGER, v BLOB, PRIMARY KEY(seq, chunk)
);
CREATE TABLE IF NOT EXISTS faces(
  face_id INTEGER PRIMARY KEY AUTOINCREMENT,
  seq INTEGER, bbox TEXT, det_score REAL, v BLOB,
  person_id TEXT, match_score REAL
);
CREATE INDEX IF NOT EXISTS idx_faces_seq ON faces(seq);
CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);
CREATE TABLE IF NOT EXISTS face_gallery(
  gid INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id TEXT, src TEXT, v BLOB
);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, val TEXT);
"""

DOC_TEXT_CAP = 200_000
CHUNK_CHARS = 1600
MAX_CHUNKS = 8


def _db():
    _DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con):
    """Idempotent column additions so an existing index picks up new
    fields without a rebuild. items gained source/account when email
    attachments joined the corpus (2026-07-09)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(items)")}
    if "source" not in cols:
        con.execute(
            "ALTER TABLE items ADD COLUMN source TEXT DEFAULT 'imessage'")
    if "account" not in cols:
        con.execute("ALTER TABLE items ADD COLUMN account TEXT")
    con.commit()


def get_state(con, key, default=None):
    row = con.execute("SELECT val FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(con, key, val):
    con.execute("INSERT OR REPLACE INTO state(key,val) VALUES(?,?)",
                (key, str(val)))
    con.commit()


def refresh_fts(con, seq):
    """Rebuild the FTS row for one item from items + content."""
    row = con.execute(
        """SELECT i.name, c.title, c.domain, c.context, c.ocr, c.caption,
                  c.doc_text, c.transcript
           FROM items i LEFT JOIN content c ON c.seq = i.seq
           WHERE i.seq=?""", (seq,)).fetchone()
    if not row:
        return
    con.execute("DELETE FROM fts WHERE rowid=?", (seq,))
    con.execute(
        "INSERT INTO fts(rowid,name,title,domain,context,ocr,caption,"
        "doc_text,transcript) VALUES(?,?,?,?,?,?,?,?,?)",
        (seq, *[x or "" for x in row]))


# ---------- chat / sender resolution ----------

def _chat_maps(src):
    """(chat_id -> (style, label), chat_id -> pid for 1:1 chats,
    handle_rowid -> handle string)."""
    chats = {r[0]: (r[1], r[2] or "") for r in src.execute(
        "SELECT ROWID, style, display_name FROM chat")}
    handles = dict(src.execute("SELECT ROWID, id FROM handle").fetchall())
    chat_pid = {}
    rows = src.execute(
        """SELECT chj.chat_id, h.id FROM chat_handle_join chj
           JOIN handle h ON h.ROWID = chj.handle_id""").fetchall()
    by_chat = {}
    for cid, h in rows:
        by_chat.setdefault(cid, []).append(h)
    for cid, hs in by_chat.items():
        if chats.get(cid, (0,))[0] == 45:      # 1:1
            pid = None
            for h in hs:
                pid = crm.resolve_handle(h)
                if pid:
                    break
            if pid:
                chat_pid[cid] = pid
    return chats, chat_pid, handles


def _sender(from_me, handle_rowid, handles):
    if from_me:
        return "me", None
    h = handles.get(handle_rowid)
    return (crm.resolve_handle(h) if h else None), h


def _classify(mime, name):
    mime = mime or ""
    if mime.startswith(IMAGE_MIME):
        return "photo"
    if mime.startswith(VIDEO_MIME):
        return "video"
    if mime.startswith("audio/") or name.lower().endswith((".caf", ".m4a")):
        return "audio"
    return "doc"


# ---------- stage: metadata (attachments + links + context) ----------

def backfill_metadata(log=print):
    """Enumerate every attachment and link across all chats, resolve
    senders and 1:1 owners, attach the accompanying-message context,
    and populate FTS. Idempotent past ROWID watermarks."""
    con = _db()
    src = _connect()
    try:
        chats, chat_pid, handles = _chat_maps(src)
        wm_att = int(get_state(con, "wm_attachment", 0))
        att_rows = src.execute(
            """SELECT a.ROWID, a.filename, a.mime_type, a.transfer_name,
                      a.total_bytes, m.ROWID, m.date, m.is_from_me,
                      m.handle_id, cmj.chat_id, m.text, m.attributedBody
               FROM attachment a
               JOIN message_attachment_join maj ON maj.attachment_id=a.ROWID
               JOIN message m ON m.ROWID=maj.message_id
               JOIN chat_message_join cmj ON cmj.message_id=m.ROWID
               WHERE a.ROWID > ? AND a.filename IS NOT NULL
                 AND a.hide_attachment=0 AND a.is_sticker=0
               ORDER BY a.ROWID""", (wm_att,)).fetchall()
        wm_link = int(get_state(con, "wm_link", 0))
        link_rows = src.execute(
            """SELECT m.ROWID, m.date, m.is_from_me, m.handle_id,
                      cmj.chat_id, m.text, m.attributedBody, m.payload_data
               FROM message m
               JOIN chat_message_join cmj ON cmj.message_id=m.ROWID
               WHERE m.ROWID > ?
                 AND (m.balloon_bundle_id =
                        'com.apple.messages.URLBalloonProvider'
                      OR m.text LIKE '%http%')
               ORDER BY m.ROWID""", (wm_link,)).fetchall()
    finally:
        src.close()

    by_chat = {}
    for r in att_rows:
        by_chat.setdefault(r[9], {"att": [], "link": []})["att"].append(r)
    for r in link_rows:
        by_chat.setdefault(r[4], {"att": [], "link": []})["link"].append(r)

    n_att = n_link = 0
    for cid, groups in by_chat.items():
        style, label = chats.get(cid, (45, ""))
        texts = _text_messages([cid]) if (groups["att"] or groups["link"]) \
            else []
        dates = [t[0] for t in texts]

        for (aid, fname, mime, tname, size, mrow, date_ns, fm, hrow, _cid,
             mtext, mblob) in groups["att"]:
            name = (tname or Path(fname).name or "")
            if name.lower().endswith(SKIP_NAMES):
                continue
            kind = _classify(mime, name)
            path = Path(fname).expanduser()
            purged = 0 if path.exists() else 1
            spid, shandle = _sender(fm, hrow, handles)
            caption = msg_text(mtext, mblob)
            if caption:
                ctx, cfm = caption[:400], bool(fm)
            else:
                c = _nearest_context(texts, dates, date_ns, mrow, bool(fm))
                ctx, cfm = (c["text"], c["from_me"]) if c else ("", None)
            cur = con.execute(
                """INSERT OR IGNORE INTO items(kind,id,chat_id,chat_pid,
                   is_group,msg_rowid,sender_pid,sender_handle,from_me,
                   date_ns,name,mime,size,path,purged)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (kind, aid, cid, chat_pid.get(cid), int(style != 45), mrow,
                 spid, shandle, int(bool(fm)), date_ns, name, mime or "",
                 size or 0, str(path), purged))
            if not cur.rowcount:
                continue
            seq = cur.lastrowid
            con.execute(
                """INSERT OR REPLACE INTO content(seq,context,ctx_from_me)
                   VALUES(?,?,?)""",
                (seq, ctx, None if cfm is None else int(cfm)))
            refresh_fts(con, seq)
            n_att += 1

        for (mrow, date_ns, fm, hrow, _cid, mtext, mblob,
             payload) in groups["link"]:
            t = msg_text(mtext, mblob) or ""
            url = title = None
            if payload:
                url, title = _link_from_payload(payload)
            if not url:
                m = URL_RE.search(t)
                url = m.group(0) if m else None
            if not url:
                continue
            spid, shandle = _sender(fm, hrow, handles)
            leftover = URL_RE.sub("", t).strip(" \n\t-—·|,;:")
            if leftover and len(leftover) > 2:
                ctx, cfm = leftover[:400], bool(fm)
            else:
                c = _nearest_context(texts, dates, date_ns, mrow, bool(fm))
                ctx, cfm = (c["text"], c["from_me"]) if c else ("", None)
            cur = con.execute(
                """INSERT OR IGNORE INTO items(kind,id,chat_id,chat_pid,
                   is_group,msg_rowid,sender_pid,sender_handle,from_me,
                   date_ns,name,purged)
                   VALUES('link',?,?,?,?,?,?,?,?,?,?,0)""",
                (mrow, cid, chat_pid.get(cid), int(style != 45), mrow,
                 spid, shandle, int(bool(fm)), date_ns,
                 (title or "").strip()[:140]))
            if not cur.rowcount:
                continue
            seq = cur.lastrowid
            con.execute(
                """INSERT OR REPLACE INTO content(seq,context,ctx_from_me,
                   title,url,domain) VALUES(?,?,?,?,?,?)""",
                (seq, ctx, None if cfm is None else int(cfm),
                 (title or "").strip()[:140], url, _domain(url)))
            refresh_fts(con, seq)
            n_link += 1
        con.commit()

    if att_rows:
        set_state(con, "wm_attachment", att_rows[-1][0])
    if link_rows:
        set_state(con, "wm_link", link_rows[-1][0])
    con.commit()
    log(f"metadata: +{n_att} attachments, +{n_link} links")
    con.close()
    return n_att + n_link


# ---------- stage: doc text + hashes ----------

def _extract_doc_text(path, name):
    ext = Path(name).suffix.lower()
    try:
        if ext == ".pdf":
            r = subprocess.run(["pdftotext", "-q", str(path), "-"],
                               capture_output=True, text=True, timeout=60)
            return r.stdout
        if ext in (".docx", ".doc", ".rtf", ".txt", ".html"):
            r = subprocess.run(
                ["textutil", "-convert", "txt", "-stdout", str(path)],
                capture_output=True, text=True, timeout=60)
            return r.stdout
        if ext == ".pptx":
            out = []
            with zipfile.ZipFile(path) as z:
                slides = sorted(n for n in z.namelist()
                                if re.match(r"ppt/slides/slide\d+\.xml", n))
                for s in slides:
                    xml = z.read(s).decode("utf-8", "ignore")
                    out.extend(re.findall(r"<a:t>([^<]*)</a:t>", xml))
            return "\n".join(out)
        if ext == ".vcf":
            return path.read_text(errors="ignore")
    except Exception:  # noqa: BLE001 — extraction is best-effort per file
        return ""
    return ""


def backfill_docs(log=print):
    con = _db()
    rows = con.execute(
        """SELECT i.seq, i.path, i.name FROM items i
           JOIN content c ON c.seq=i.seq
           WHERE i.kind='doc' AND i.purged=0 AND i.sha256 IS NULL"""
    ).fetchall()
    n = 0
    for seq, path, name in rows:
        p = Path(path)
        if not p.exists():
            con.execute("UPDATE items SET purged=1 WHERE seq=?", (seq,))
            continue
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        text = (_extract_doc_text(p, name) or "").strip()[:DOC_TEXT_CAP]
        con.execute("UPDATE items SET sha256=? WHERE seq=?", (sha, seq))
        con.execute("UPDATE content SET doc_text=? WHERE seq=?", (text, seq))
        refresh_fts(con, seq)
        n += 1
        if n % 50 == 0:
            con.commit()
    con.commit()
    con.close()
    log(f"docs: {n} extracted/hashed")
    return n


# ---------- stage: OCR (Apple Vision, on-device) ----------

def backfill_ocr(log=print, batch_commit=25):
    from .localmodels import ocr_available, vision_ocr
    if not ocr_available():
        # Skip resumably (the Ollama-down precedent): items stay ocr=''
        # so a future backend picks them up, never stamped "ran, empty".
        log("ocr: no backend on this platform (Apple Vision) — skipped")
        return 0
    con = _db()
    rows = con.execute(
        """SELECT i.seq, i.path FROM items i JOIN content c ON c.seq=i.seq
           WHERE i.kind='photo' AND i.purged=0 AND c.ocr=''
           ORDER BY i.date_ns DESC""").fetchall()
    n = 0
    t0 = time.time()
    for seq, path in rows:
        if not Path(path).exists():
            con.execute("UPDATE items SET purged=1 WHERE seq=?", (seq,))
            continue
        text = vision_ocr(path)
        # single space = "OCR ran, nothing found" so the stage never re-runs
        con.execute("UPDATE content SET ocr=? WHERE seq=?",
                    (text if text.strip() else " ", seq))
        refresh_fts(con, seq)
        n += 1
        if n % batch_commit == 0:
            con.commit()
        if n % 500 == 0:
            log(f"  ocr {n}/{len(rows)} ({n/(time.time()-t0):.1f}/s)")
    con.commit()
    con.close()
    log(f"ocr: {n} images")
    return n


# ---------- stage: scene embeddings (SigLIP) ----------

def backfill_scene(log=print, batch=16):
    import numpy as np
    from .localmodels import siglip_embed_images
    con = _db()
    rows = con.execute(
        """SELECT i.seq, i.kind, i.id, i.path FROM items i
           LEFT JOIN vec_scene v ON v.seq=i.seq
           WHERE i.kind IN ('photo','video') AND i.purged=0 AND v.seq IS NULL
           ORDER BY i.date_ns DESC""").fetchall()
    n = 0
    t0 = time.time()
    pend = []
    for seq, kind, att_id, path in rows:
        # videos embed their cached first-frame thumbnail
        p = thumbnail(att_id) if kind == "video" else Path(path)
        if not p or not Path(p).exists():
            if kind == "photo" and not Path(path).exists():
                con.execute("UPDATE items SET purged=1 WHERE seq=?", (seq,))
            continue
        pend.append((seq, str(p)))
        if len(pend) >= batch:
            n += _flush_scene(con, pend, siglip_embed_images)
            pend = []
            con.commit()   # every batch: keep write-lock windows short
            if n % 320 < batch:
                log(f"  scene {n}/{len(rows)} ({n/(time.time()-t0):.1f}/s)")
    if pend:
        n += _flush_scene(con, pend, siglip_embed_images)
    con.commit()
    con.close()
    log(f"scene: {n} embedded")
    return n


def _flush_scene(con, pend, embed):
    vecs = embed([p for _, p in pend])   # None per unreadable image
    n = 0
    for (seq, _p), v in zip(pend, vecs):
        if v is None:
            continue
        con.execute("INSERT OR REPLACE INTO vec_scene(seq,v) VALUES(?,?)",
                    (seq, v.astype("float16").tobytes()))
        n += 1
    return n


# ---------- stage: text embeddings (Ollama nomic) ----------

def _compose_text(row):
    """The searchable text of one item, composed for embedding."""
    name, title, domain, context, ocr, caption, doc_text, transcript = row
    head = " · ".join(x for x in (title, name, domain) if x and x.strip())
    body = " ".join(x for x in (context, caption, ocr[:800]) if x and
                    x.strip())
    return (head + "\n" + body).strip(), doc_text, transcript


def backfill_textvec(log=print):
    from .localmodels import ollama_embed
    con = _db()
    rows = con.execute(
        """SELECT i.seq, i.name, c.title, c.domain, c.context, c.ocr,
                  c.caption, c.doc_text, c.transcript
           FROM items i JOIN content c ON c.seq=i.seq
           LEFT JOIN vec_text v ON v.seq=i.seq AND v.chunk=0
           WHERE v.seq IS NULL""").fetchall()
    n = 0
    for row in rows:
        seq = row[0]
        head, doc_text, transcript = _compose_text(row[1:])
        chunks = [head] if head else []
        long_text = (doc_text or "") + "\n" + (transcript or "")
        long_text = long_text.strip()
        for i in range(0, min(len(long_text), CHUNK_CHARS * (MAX_CHUNKS - 1)),
                       CHUNK_CHARS):
            chunks.append(long_text[i:i + CHUNK_CHARS])
        if not chunks:
            continue
        vecs = ollama_embed([f"search_document: {c}" for c in chunks])
        if vecs is None:
            log("  textvec: ollama unavailable, stopping (resumable)")
            break
        for ci, v in enumerate(vecs):
            con.execute(
                "INSERT OR REPLACE INTO vec_text(seq,chunk,v) VALUES(?,?,?)",
                (seq, ci, v.astype("float16").tobytes()))
        n += 1
        if n % 100 == 0:
            con.commit()
            log(f"  textvec {n}/{len(rows)}")
    con.commit()
    con.close()
    log(f"textvec: {n} items")
    return n


# ---------- stage: faces ----------

def enroll_gallery(log=print):
    """Seed the identity gallery from the CRM contact-photo cache: one
    reference face per person who has a contact photo."""
    from .localmodels import face_analyze
    from .photos import CACHE
    con = _db()
    have = {r[0] for r in con.execute(
        "SELECT DISTINCT person_id FROM face_gallery WHERE src='photo-cache'")}
    n = 0
    for f in sorted(CACHE.glob("p_*.jpg")):
        pid = f.stem
        if pid in have:
            continue
        faces = face_analyze(str(f))
        if not faces:
            continue
        best = max(faces, key=lambda x: x["det_score"])
        con.execute(
            "INSERT INTO face_gallery(person_id,src,v) VALUES(?,?,?)",
            (pid, "photo-cache", best["v"].astype("float16").tobytes()))
        n += 1
    con.commit()
    con.close()
    log(f"gallery: +{n} reference faces (photo-cache)")
    return n


FACE_MATCH_T = 0.45     # ArcFace cosine; below this a face stays unnamed


def _match_faces(con):
    """(Re)assign person_id on every stored face from the current gallery."""
    import numpy as np
    gal = con.execute("SELECT person_id, v FROM face_gallery").fetchall()
    if not gal:
        return 0
    pids = [g[0] for g in gal]
    G = np.stack([np.frombuffer(g[1], dtype="float16").astype("float32")
                  for g in gal])
    G /= (np.linalg.norm(G, axis=1, keepdims=True) + 1e-9)
    n = 0
    for fid, blob in con.execute("SELECT face_id, v FROM faces").fetchall():
        v = np.frombuffer(blob, dtype="float16").astype("float32")
        v /= (np.linalg.norm(v) + 1e-9)
        sims = G @ v
        i = int(sims.argmax())
        if sims[i] >= FACE_MATCH_T:
            con.execute(
                "UPDATE faces SET person_id=?, match_score=? WHERE face_id=?",
                (pids[i], float(sims[i]), fid))
            n += 1
    con.commit()
    return n


def backfill_faces(log=print):
    from .localmodels import face_analyze
    con = _db()
    done = {r[0] for r in con.execute("SELECT DISTINCT seq FROM faces")}
    done |= {int(x) for x in json.loads(
        get_state(con, "faces_no_face", "[]"))}
    rows = con.execute(
        """SELECT seq, path FROM items
           WHERE kind='photo' AND purged=0 ORDER BY date_ns DESC""").fetchall()
    rows = [(s, p) for s, p in rows if s not in done]
    no_face = json.loads(get_state(con, "faces_no_face", "[]"))
    n_img = n_face = 0
    t0 = time.time()
    for seq, path in rows:
        if not Path(path).exists():
            con.execute("UPDATE items SET purged=1 WHERE seq=?", (seq,))
            continue
        faces = face_analyze(path)
        if not faces:
            no_face.append(seq)
        for f in faces:
            con.execute(
                """INSERT INTO faces(seq,bbox,det_score,v)
                   VALUES(?,?,?,?)""",
                (seq, json.dumps([round(x, 1) for x in f["bbox"]]),
                 float(f["det_score"]),
                 f["v"].astype("float16").tobytes()))
            n_face += 1
        n_img += 1
        if n_img % 25 == 0:
            set_state(con, "faces_no_face", json.dumps(no_face))
            con.commit()
        if n_img % 500 == 0:
            log(f"  faces {n_img}/{len(rows)} "
                f"({n_img/(time.time()-t0):.1f}/s, {n_face} faces)")
    set_state(con, "faces_no_face", json.dumps(no_face))
    con.commit()
    matched = _match_faces(con)
    con.close()
    log(f"faces: {n_img} images, {n_face} faces, {matched} matched")
    return n_img


def tag_face(face_id, person_id):
    """Manual enrollment from a search result: this face IS this person.
    Adds the face vector to the gallery and re-matches everything."""
    con = _db()
    row = con.execute("SELECT v FROM faces WHERE face_id=?",
                      (face_id,)).fetchone()
    if not row:
        con.close()
        return 0
    con.execute("INSERT INTO face_gallery(person_id,src,v) VALUES(?,?,?)",
                (person_id, f"tagged:{face_id}", row[0]))
    con.commit()
    n = _match_faces(con)
    con.close()
    return n


# ---------- stage: captions (local VLM via Ollama) ----------

CAPTION_PROMPT = (
    "Describe this photo in 1-2 dense sentences for a search index: "
    "the subjects, what they are doing, the setting, and notable objects "
    "or visible text. No preamble.")


def backfill_captions(log=print, limit=None):
    from .localmodels import ollama_caption
    con = _db()
    rows = con.execute(
        """SELECT i.seq, i.path FROM items i JOIN content c ON c.seq=i.seq
           WHERE i.kind='photo' AND i.purged=0 AND c.caption=''
           ORDER BY i.date_ns DESC""").fetchall()
    if limit:
        rows = rows[:limit]
    n = 0
    t0 = time.time()
    for seq, path in rows:
        if not Path(path).exists():
            continue
        cap = ollama_caption(path, CAPTION_PROMPT)
        if cap is None:               # ollama down — stop, resumable
            log("  captions: ollama unavailable, stopping")
            break
        con.execute("UPDATE content SET caption=? WHERE seq=?",
                    (cap.strip()[:600] or " ", seq))
        refresh_fts(con, seq)
        n += 1
        if n % 25 == 0:
            con.commit()
        if n % 200 == 0:
            log(f"  captions {n}/{len(rows)} "
                f"({(time.time()-t0)/max(n,1):.1f}s each)")
    con.commit()
    con.close()
    log(f"captions: {n} written")
    return n


# ---------- stage: transcripts (mlx-whisper) ----------

def backfill_transcripts(log=print, limit=None):
    from .localmodels import whisper_transcribe
    con = _db()
    rows = con.execute(
        """SELECT i.seq, i.path FROM items i JOIN content c ON c.seq=i.seq
           WHERE i.kind IN ('video','audio') AND i.purged=0
             AND c.transcript='' ORDER BY i.date_ns DESC""").fetchall()
    if limit:
        rows = rows[:limit]
    n = 0
    for seq, path in rows:
        if not Path(path).exists():
            continue
        text = whisper_transcribe(path)
        con.execute("UPDATE content SET transcript=? WHERE seq=?",
                    ((text or "").strip()[:DOC_TEXT_CAP] or " ", seq))
        refresh_fts(con, seq)
        n += 1
        if n % 10 == 0:
            con.commit()
            log(f"  transcripts {n}/{len(rows)}")
    con.commit()
    con.close()
    log(f"transcripts: {n} written")
    return n


# ---------- reconcile + status ----------

def reconcile(log=print):
    """Mark purged files; keep index rows (still searchable by context)."""
    con = _db()
    n = 0
    for seq, path in con.execute(
            "SELECT seq, path FROM items WHERE purged=0 AND path IS NOT NULL"
    ).fetchall():
        if not Path(path).exists():
            con.execute("UPDATE items SET purged=1 WHERE seq=?", (seq,))
            n += 1
    con.commit()
    con.close()
    log(f"reconcile: {n} newly purged")
    return n


def status():
    con = _db()
    out = {}
    for k, in con.execute("SELECT DISTINCT kind FROM items"):
        out[k] = con.execute("SELECT COUNT(*) FROM items WHERE kind=?",
                             (k,)).fetchone()[0]
    out["purged"] = con.execute(
        "SELECT COUNT(*) FROM items WHERE purged=1").fetchone()[0]
    out["ocr_done"] = con.execute(
        "SELECT COUNT(*) FROM content WHERE ocr!=''").fetchone()[0]
    out["captions_done"] = con.execute(
        "SELECT COUNT(*) FROM content WHERE caption!=''").fetchone()[0]
    out["transcripts_done"] = con.execute(
        "SELECT COUNT(*) FROM content WHERE transcript!=''").fetchone()[0]
    out["scene_vecs"] = con.execute(
        "SELECT COUNT(*) FROM vec_scene").fetchone()[0]
    out["text_vecs"] = con.execute(
        "SELECT COUNT(DISTINCT seq) FROM vec_text").fetchone()[0]
    out["faces"] = con.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
    out["faces_named"] = con.execute(
        "SELECT COUNT(*) FROM faces WHERE person_id IS NOT NULL").fetchone()[0]
    out["gallery"] = con.execute(
        "SELECT COUNT(*) FROM face_gallery").fetchone()[0]
    con.close()
    return out


# ---------- incremental (called by the indexer thread) ----------

def backfill_email(log=print):
    """Stage wrapper: pull every email attachment (Outlook via Graph,
    Gmail via IMAP) into the index. Content stages (docs/ocr/scene/faces/
    textvec) then process them like any other item."""
    from . import mailindex
    return mailindex.backfill(full=True, log=log)


def run_incremental(log=lambda *a: None):
    """Index whatever is new since the watermarks: metadata + context,
    then the cheap per-item stages. Captions/transcripts ride the
    nightly/backlog runs. Safe to call every ~30s."""
    added = backfill_metadata(log=log)
    try:
        from . import mailindex
        added += mailindex.incremental(log=log)
    except Exception as e:  # noqa: BLE001 — mailbox may be offline/unconfigured
        log(f"incremental email skipped: {e}")
    if not added:
        return 0
    backfill_docs(log=log)
    try:
        backfill_ocr(log=log)
    except Exception as e:  # noqa: BLE001 — models may still be warming
        log(f"incremental ocr skipped: {e}")
    try:
        backfill_scene(log=log)
    except Exception as e:  # noqa: BLE001
        log(f"incremental scene skipped: {e}")
    try:
        backfill_faces(log=log)
    except Exception as e:  # noqa: BLE001
        log(f"incremental faces skipped: {e}")
    backfill_textvec(log=log)
    return added


class Indexer(threading.Thread):
    """In-server incremental indexing: new attachments/links become
    searchable within ~30s of arrival. Gated off while a CLI backfill
    owns the stage scans (state key backfill_running); reconciles
    purged files nightly around 04:00."""

    def __init__(self, interval=30):
        super().__init__(daemon=True)
        self.interval = interval
        self._last_reconcile = 0.0

    def _log(self, msg):
        line = f"{datetime.now().isoformat(timespec='seconds')} {msg}\n"
        try:
            with open(_DATA / "media-index.log", "a") as f:
                f.write(line)
        except OSError:
            pass

    def run(self):
        time.sleep(20)          # let the server settle first
        while True:
            try:
                con = _db()
                gated = get_state(con, "backfill_running") == "1"
                con.close()
                if not gated:
                    added = run_incremental(log=self._log)
                    if added:
                        from . import search
                        search.invalidate()
                        self._log(f"incremental: {added} new items")
                    now = time.time()
                    if (time.localtime().tm_hour == 4
                            and now - self._last_reconcile > 6 * 3600):
                        reconcile(log=self._log)
                        self._last_reconcile = now
            except Exception as e:  # noqa: BLE001 — never kill the thread
                self._log(f"indexer error: {e}")
            time.sleep(self.interval)


def _gallery_photos(log=print):
    from .photosfaces import harvest
    return harvest(log=log)


STAGES = {
    "metadata": backfill_metadata,
    "email": backfill_email,
    "docs": backfill_docs,
    "ocr": backfill_ocr,
    "scene": backfill_scene,
    "textvec": backfill_textvec,
    "gallery": enroll_gallery,
    "gallery-photos": _gallery_photos,
    "faces": backfill_faces,
    "captions": backfill_captions,
    "transcripts": backfill_transcripts,
}

if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "status":
        print(json.dumps(status(), indent=2))
    elif args and args[0] == "reconcile":
        reconcile()
    elif args and args[0] == "backfill":
        names = ([args[args.index("--stage") + 1]] if "--stage" in args
                 else ["metadata", "email", "docs", "ocr", "scene",
                       "textvec", "gallery", "faces"])
        # gate the in-server Indexer off while the CLI owns the stage
        # scans — two writers on the faces stage isn't idempotent
        gate = _db()
        set_state(gate, "backfill_running", "1")
        gate.close()
        try:
            for name in names:
                print(f"== stage: {name}")
                STAGES[name]()
        finally:
            gate = _db()
            set_state(gate, "backfill_running", "0")
            gate.close()
    else:
        print("usage: python -m server.mediaindex "
              "backfill [--stage NAME] | status | reconcile")
