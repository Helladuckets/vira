"""Message and mail bodies: the chat.db backfill (including the
attributedBody decode that carries almost every message), the
deterministic filters, and the row shape the UI renders.

The chat.db here is a synthetic four-message sqlite with the same
columns the real one has — no Full Disk Access needed, no personal
data in the tree.

Run: .venv/bin/python -m unittest tests.test_textindex
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import data as crm
from server import mediaindex, textindex
from server.imessage import apple_ns

from datetime import datetime

DAY = 86_400 * 1_000_000_000


def _crm_cache():
    return {"loaded_at": 1.0,
            "by_id": {"p_ann": {"id": "p_ann", "name": "Ann Reyes"},
                      "p_raj": {"id": "p_raj", "name": "Raj Patel"}},
            "by_handle": {"ann@example.test": "p_ann",
                          "raj@example.test": "p_raj"}}


def _chat_db(path, base_ns):
    """A miniature chat.db: two 1:1 chats, one group, one tapback."""
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE chat(ROWID INTEGER PRIMARY KEY, style INT,
                        display_name TEXT);
      CREATE TABLE handle(ROWID INTEGER PRIMARY KEY, id TEXT);
      CREATE TABLE chat_handle_join(chat_id INT, handle_id INT);
      CREATE TABLE message(ROWID INTEGER PRIMARY KEY, date INT,
                           is_from_me INT, handle_id INT, text TEXT,
                           attributedBody BLOB,
                           associated_message_type INT DEFAULT 0);
      CREATE TABLE chat_message_join(chat_id INT, message_id INT);
    """)
    con.execute("INSERT INTO chat VALUES(1, 45, '')")       # 1:1 with Ann
    con.execute("INSERT INTO chat VALUES(2, 43, 'Ski trip')")   # group
    con.execute("INSERT INTO handle VALUES(1, 'ann@example.test')")
    con.execute("INSERT INTO handle VALUES(2, 'raj@example.test')")
    con.executemany("INSERT INTO chat_handle_join VALUES(?,?)",
                    [(1, 1), (2, 1), (2, 2)])
    rows = [
        # rowid, date, from_me, handle, text, blob, assoc
        (1, base_ns, 0, 1, "the lease renewal is signed", None, 0),
        (2, base_ns + DAY, 1, None, "sending the deck now", None, 0),
        (3, base_ns + 2 * DAY, 0, 2, None, b"typedstream-blob", 0),
        (4, base_ns + 3 * DAY, 0, 1, "Liked “nice”", None, 2000),
        (5, base_ns + 4 * DAY, 0, 2, "renewal of the lease attached",
         None, 0),
    ]
    con.executemany("INSERT INTO message VALUES(?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO chat_message_join VALUES(?,?)",
                    [(1, 1), (1, 2), (2, 3), (1, 4), (2, 5)])
    con.commit()
    con.close()


class TextIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = apple_ns(datetime(2026, 3, 10, 12, 0))
        self.chat = Path(self.tmp.name) / "chat.db"
        _chat_db(self.chat, self.base)
        self.db = Path(self.tmp.name) / "text-index.sqlite"
        patches = [
            mock.patch.object(textindex, "DB", self.db),
            mock.patch.object(crm, "_load", _crm_cache),
            mock.patch.object(
                crm, "resolve_handle",
                lambda h: _crm_cache()["by_handle"].get(h)),
            mock.patch.object(
                mediaindex, "_connect",
                lambda: sqlite3.connect(f"file:{self.chat}?mode=ro",
                                        uri=True)),
            # every real row carries its text in attributedBody
            mock.patch("server.textindex.msg_text",
                       lambda t, b: t or ("skis are packed" if b else "")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def index(self):
        return textindex.backfill_imessage(log=lambda *a: None)

    def test_backfill_indexes_bodies_and_skips_tapbacks(self):
        self.assertEqual(self.index(), 4)      # the "Liked" row is dropped
        self.assertEqual(textindex.status()["messages"], 4)

    def test_attributed_body_rows_are_decoded(self):
        self.index()
        hits = textindex.search("skis")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["sender"], "Raj Patel")

    def test_backfill_is_idempotent_via_the_watermark(self):
        self.index()
        self.assertEqual(self.index(), 0)
        self.assertEqual(textindex.status()["messages"], 4)

    def test_group_and_direction_flags(self):
        self.index()
        group = textindex.search("skis")[0]
        self.assertTrue(group["is_group"])
        mine = textindex.search("deck")[0]
        self.assertTrue(mine["from_me"])
        self.assertEqual(mine["sender"], "you")

    def test_person_filter_covers_both_sides_of_a_thread(self):
        self.index()
        # chat 1 resolves to Ann, so her thread carries both directions
        rows = textindex.search("", person="p_ann", limit=10)
        self.assertEqual(len(rows), 2)

    def test_direction_filter(self):
        self.index()
        self.assertEqual(len(textindex.search("", direction="sent",
                                              limit=10)), 1)

    def test_date_window_excludes_outside_rows(self):
        self.index()
        self.assertEqual(len(textindex.search("", since="2026-03-12",
                                              limit=10)), 2)
        self.assertEqual(len(textindex.search("", until="2026-03-11",
                                              limit=10)), 1)

    def test_query_and_filter_compose(self):
        self.index()
        self.assertEqual(len(textindex.search("lease", person="p_ann")), 1)
        self.assertEqual(len(textindex.search("lease", direction="sent")), 0)

    def test_recency_order(self):
        self.index()
        rows = textindex.search("", limit=10, order="recent")
        self.assertGreater(rows[0]["when"], rows[-1]["when"])
        rows = textindex.search("", limit=10, order="oldest")
        self.assertLess(rows[0]["when"], rows[-1]["when"])

    def test_a_quoted_phrase_outranks_a_bag_of_the_same_words(self):
        # both rows carry "lease" and "renewal"; only one has them in the
        # order the user typed inside quotes
        self.index()
        hits = textindex.search("lease renewal", phrases=["lease renewal"])
        self.assertEqual(len(hits), 2)
        self.assertIn("lease renewal", hits[0]["text"])

    def test_available_is_false_until_something_is_indexed(self):
        self.assertFalse(textindex.available())
        self.index()
        self.assertTrue(textindex.available())

    def test_status_reports_keyword_only(self):
        self.index()
        st = textindex.status()
        self.assertEqual(st["mode"], "fts")
        self.assertEqual(st["vectors"], 0)


if __name__ == "__main__":
    unittest.main()
