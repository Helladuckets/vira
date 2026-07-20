"""Live iMessage access: read threads from chat.db and watch for new inbound
messages. Deterministic — direct sqlite reads, no AI.

The typedstream decoder is the proven one from
~/workspace/crm/scripts/export_imessage_content.py.
"""
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import data as crm
from . import fixtures, settings

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
STATE = Path(__file__).resolve().parent.parent / "data" / "watcher-state.json"

APPLE_EPOCH = 978307200  # 2001-01-01 in unix seconds


def attributed_text(blob):
    """Extract the visible string from a typedstream-archived NSAttributedString."""
    if not blob:
        return None
    i = blob.find(b"NSString")
    if i < 0:
        return None
    i += len(b"NSString") + 5  # skip 0x01 0x94 0x84 0x01 0x2B
    if i >= len(blob):
        return None
    length = blob[i]
    if length == 0x81:
        length = int.from_bytes(blob[i + 1:i + 3], "little")
        i += 3
    else:
        i += 1
    return blob[i:i + length].decode("utf-8", errors="replace")


def msg_text(text, blob):
    t = text or attributed_text(blob)
    if not t:
        return None
    t = t.replace("￼", "").replace("�", "").strip()
    return t or None


def apple_dt(ns):
    if not ns:
        return None
    return datetime.fromtimestamp(ns / 1e9 + APPLE_EPOCH, tz=timezone.utc).astimezone()


def _connect():
    return sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)


def thread_for_person(pid, limit=40):
    """Most recent direct-chat messages with this person, both directions."""
    if settings.fixture_mode():
        return fixtures.thread(pid, limit)
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
        rows = con.execute(
            f"""SELECT m.ROWID, m.date, m.is_from_me, m.text, m.attributedBody,
                       h.id, c.style
                FROM message m
                JOIN handle h ON h.ROWID = m.handle_id
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE h.id IN ({qmarks}) AND c.style = 45
                  AND (m.associated_message_type = 0
                       OR m.associated_message_type IS NULL)
                ORDER BY m.date DESC LIMIT ?""",
            (*handles, limit)).fetchall()
    finally:
        con.close()
    out = []
    for rowid, dt, from_me, text, blob, handle, _style in reversed(rows):
        t = msg_text(text, blob)
        if not t:
            continue
        when = apple_dt(dt)
        out.append({
            "rowid": rowid,
            "when": when.isoformat() if when else None,
            "from_me": bool(from_me),
            "text": t,
            "handle": handle,
        })
    return out


def groups_for_person(pid):
    """All group chats this person participates in, live from chat.db.
    chat.db often carries several chat rows for one logical group (SMS vs
    iMessage legs); rows with the same member set and name are merged, and
    the thread endpoint accepts the merged chat-id list."""
    if settings.fixture_mode():
        return []
    c = crm._load()
    p = c["by_id"].get(pid)
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
        chat_ids = [r[0] for r in con.execute(
            f"""SELECT DISTINCT c.ROWID
                FROM chat c
                JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
                JOIN handle h ON h.ROWID = chj.handle_id
                WHERE h.id IN ({qmarks}) AND c.style = 43""",
            tuple(handles)).fetchall()]
        merged = {}
        for cid in chat_ids:
            display_name, n_msgs, last_ns = con.execute(
                """SELECT c.display_name,
                          (SELECT COUNT(*) FROM chat_message_join
                           WHERE chat_id = c.ROWID),
                          (SELECT MAX(m.date) FROM message m
                           JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                           WHERE cmj.chat_id = c.ROWID)
                   FROM chat c WHERE c.ROWID = ?""", (cid,)).fetchone()
            members = sorted(r[0] for r in con.execute(
                """SELECT h.id FROM chat_handle_join chj
                   JOIN handle h ON h.ROWID = chj.handle_id
                   WHERE chj.chat_id = ?""", (cid,)).fetchall())
            last = apple_dt(last_ns)
            last_iso = last.isoformat() if last else None
            key = (tuple(members), display_name or "")
            g = merged.get(key)
            if g:
                g["chat_ids"].append(cid)
                g["messages"] += n_msgs or 0
                g["last"] = max(g["last"] or "", last_iso or "") or None
                continue
            participants = []
            for hd in members:
                mpid = crm.resolve_handle(hd)
                mp = c["by_id"].get(mpid) if mpid else None
                participants.append({"handle": hd, "person_id": mpid,
                                     "name": mp["name"] if mp else hd})
            merged[key] = {
                "chat_ids": [cid],
                "name": display_name or None,
                "participants": participants,
                "messages": n_msgs or 0,
                "last": last_iso,
            }
        out = list(merged.values())
        out.sort(key=lambda g: g["last"] or "", reverse=True)
        return out
    finally:
        con.close()


def group_thread(chat_ids, limit=60, before=None):
    """Recent messages across the given chat rows (one logical group),
    senders resolved to CRM names. `before` (a message ROWID) pages further
    back: only messages older than it are returned."""
    qmarks = ",".join("?" * len(chat_ids))
    before_sql = "AND m.ROWID < ?" if before else ""
    params = (*chat_ids, *((before,) if before else ()), limit)
    con = _connect()
    try:
        rows = con.execute(
            f"""SELECT m.ROWID, m.date, m.is_from_me, m.text, m.attributedBody,
                       h.id
                FROM message m
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                WHERE cmj.chat_id IN ({qmarks})
                  AND (m.associated_message_type = 0
                       OR m.associated_message_type IS NULL)
                  {before_sql}
                ORDER BY m.date DESC LIMIT ?""",
            params).fetchall()
    finally:
        con.close()
    c = crm._load()
    out = []
    for rowid, dt, from_me, text, blob, handle in reversed(rows):
        t = msg_text(text, blob)
        if not t:
            continue
        pid = crm.resolve_handle(handle) if handle else None
        person = c["by_id"].get(pid) if pid else None
        when = apple_dt(dt)
        out.append({
            "rowid": rowid,
            "when": when.isoformat() if when else None,
            "from_me": bool(from_me),
            "text": t,
            "handle": handle,
            "sender": "me" if from_me else (
                person["name"] if person else (handle or "?")),
        })
    return out


class Watcher:
    """Polls chat.db for new inbound messages past a ROWID watermark and keeps a
    feed of recent items joined to CRM people. Listeners get SSE-ready events."""

    def __init__(self, poll_seconds=3, feed_size=200, backfill=25):
        self.poll = poll_seconds
        self.feed = []
        self.feed_size = feed_size
        self.backfill = backfill
        self.watermark = None
        self.listeners = []
        self.lock = threading.Lock()
        self._stop = threading.Event()

    def _load_state(self):
        try:
            self.watermark = json.loads(STATE.read_text())["watermark"]
        except (OSError, json.JSONDecodeError, KeyError):
            self.watermark = None

    def _save_state(self):
        if os.environ.get("VIRA_PASSIVE"):
            # Passive instance shares data/ with the primary — never advance
            # the primary's watermark from here.
            return
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"watermark": self.watermark}))

    def _fetch_since(self, rowid, limit=500):
        con = _connect()
        try:
            rows = con.execute(
                """SELECT m.ROWID, m.date, m.is_from_me, m.text, m.attributedBody,
                          h.id, c.style, c.display_name
                   FROM message m
                   LEFT JOIN handle h ON h.ROWID = m.handle_id
                   JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                   JOIN chat c ON c.ROWID = cmj.chat_id
                   WHERE m.ROWID > ? AND m.is_from_me = 0
                     AND (m.associated_message_type = 0
                          OR m.associated_message_type IS NULL)
                   ORDER BY m.ROWID ASC LIMIT ?""", (rowid, limit)).fetchall()
        finally:
            con.close()
        return rows

    def _max_rowid(self):
        con = _connect()
        try:
            return con.execute("SELECT MAX(ROWID) FROM message").fetchone()[0] or 0
        finally:
            con.close()

    def _to_item(self, row):
        from . import photos
        rowid, dt, _fm, text, blob, handle, style, display_name = row
        t = msg_text(text, blob)
        if not t:
            return None
        pid = crm.resolve_handle(handle)
        person = crm._load()["by_id"].get(pid) if pid else None
        when = apple_dt(dt)
        return {
            "has_photo": bool(pid and photos.photo_path(pid)),
            "rowid": rowid,
            "when": when.isoformat() if when else None,
            "channel": "imessage",
            "text": t[:500],
            "handle": handle,
            "group": style != 45,
            "group_name": display_name or None,
            "person_id": pid,
            "person_name": person["name"] if person else None,
            "known": pid is not None,
        }

    def start(self):
        """Never raises: if chat.db is unreadable (no Full Disk Access in this
        process), the app still boots and the watcher keeps retrying."""
        self.ok = True
        try:
            self._seed()
        except sqlite3.OperationalError:
            self.ok = False
        threading.Thread(target=self._run, daemon=True, name="vira-watcher").start()

    def _seed(self):
        """Backfill the feed with the most recent inbound messages and set the
        watermark to the current high-water mark."""
        top = self._max_rowid()
        seed_from = max(0, top - 4000)
        rows = self._fetch_since(seed_from, limit=4000)
        items = [i for r in rows if (i := self._to_item(r))]
        with self.lock:
            self.feed = items[-self.backfill:]
        self.watermark = top
        self._save_state()

    def _run(self):
        while not self._stop.is_set():
            try:
                if self.watermark is None:
                    # access arrived after a failed start (e.g. Full Disk
                    # Access granted while running): seed now
                    self._seed()
                rows = self._fetch_since(self.watermark)
                self.ok = True
                new = []
                for r in rows:
                    self.watermark = max(self.watermark, r[0])
                    item = self._to_item(r)
                    if item:
                        new.append(item)
                if rows:
                    self._save_state()
                if new:
                    with self.lock:
                        self.feed.extend(new)
                        self.feed = self.feed[-self.feed_size:]
                        dead = []
                        for q in self.listeners:
                            try:
                                for item in new:
                                    q.put_nowait(item)
                            except Exception:
                                dead.append(q)
                        for q in dead:
                            self.listeners.remove(q)
            except sqlite3.OperationalError:
                self.ok = False  # db locked or unreadable this tick; retry next poll
            self._stop.wait(self.poll)

    def snapshot(self, limit=50):
        with self.lock:
            return list(reversed(self.feed[-limit:]))

    def subscribe(self, q):
        with self.lock:
            self.listeners.append(q)

    def unsubscribe(self, q):
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)
