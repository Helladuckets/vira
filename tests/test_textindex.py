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


class MailBacklogTests(unittest.TestCase):
    """The mail-side plumbing the 2026-07-28 backlog run exposed: account
    routing keys on "type" (the key rows actually carry), the Graph walk
    is watermarked, and the full backlog loops until a pass is dry."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(textindex, "DB",
                              Path(self.tmp.name) / "text-index.sqlite")
        p.start()
        self.addCleanup(p.stop)

    def test_graph_account_routes_on_type(self):
        # a row shaped exactly like data/mail-accounts.json: "type", not
        # the "kind"/"provider" keys the old check looked for
        accts = [{"email": "gm@example.test", "host": "imap.example.test"},
                 {"email": "gr@example.test", "type": "graph"}]
        with mock.patch.object(textindex.channels, "mail_accounts",
                               return_value=accts), \
             mock.patch.object(textindex, "backfill_graph",
                               return_value=2) as graph, \
             mock.patch.object(textindex, "backfill_imap",
                               return_value=1) as imap:
            n = textindex.backfill_mail(log=lambda *a: None)
        self.assertEqual(n, 3)
        graph.assert_called_once()
        self.assertEqual(graph.call_args[0][0], "gr@example.test")
        imap.assert_called_once()
        self.assertEqual(imap.call_args[0][0]["email"], "gm@example.test")

    def test_full_backlog_loops_until_a_pass_is_dry(self):
        passes = iter([400, 400, 37, 0])
        with mock.patch.object(textindex, "backfill_mail",
                               side_effect=lambda **kw: next(passes)):
            total = textindex.backfill_mail_full(log=lambda *a: None)
        self.assertEqual(total, 837)

    def test_an_empty_stretch_does_not_end_the_backlog(self):
        # pass 2 inserts nothing but ADVANCES the watermark (400 straight
        # empty bodies) — the loop must keep going; only a pass that moves
        # no watermark and inserts nothing is done
        wm = {"n": 0}
        script = iter([(300, True), (0, True), (250, True), (0, False)])

        def fake_pass(**kw):
            inserted, moved = next(script)
            if moved:
                wm["n"] += 1
                con = textindex._db()
                textindex.set_state(con, "wm_mail:a@example.test", wm["n"])
                con.commit()
                con.close()
            return inserted
        with mock.patch.object(textindex, "backfill_mail",
                               side_effect=fake_pass):
            total = textindex.backfill_mail_full(log=lambda *a: None)
        self.assertEqual(total, 550)

    def _graph_pages(self, calls):
        """A two-page mailbox; `calls` records each request path."""
        def fake_request(account, path):
            calls.append(path)
            if "$skip" in path:
                return {"value": [_graph_msg("m1", "2026-03-01T10:00:00Z")]}
            return {"value": [_graph_msg("m2", "2026-03-05T10:00:00Z")],
                    "@odata.nextLink": "/me/messages?$skip=25"}
        return fake_request

    def test_graph_walk_stamps_and_reuses_a_watermark(self):
        from server import msgraph
        calls = []
        with mock.patch.object(crm, "resolve_handle", lambda h: None), \
             mock.patch.object(textindex.channels, "mail_accounts",
                               return_value=[{"email": "me@example.test"}]), \
             mock.patch.object(msgraph, "_graph_request",
                               side_effect=self._graph_pages(calls)):
            n = textindex.backfill_graph("gr@example.test",
                                         log=lambda *a: None)
            self.assertEqual(n, 2)
            con = textindex._db()
            wm = textindex.get_state(con, "wm_mailg:gr@example.test")
            con.close()
            self.assertEqual(wm, "2026-03-05T10:00:00Z")
            # the next run filters from the stamp instead of re-walking
            calls.clear()
            textindex.backfill_graph("gr@example.test", log=lambda *a: None)
        self.assertIn("receivedDateTime%20ge%202026-03-05T10%3A00%3A00Z",
                      calls[0])

    def test_an_interrupted_graph_walk_leaves_the_watermark_alone(self):
        from server import msgraph

        def one_page_forever(account, path):
            return {"value": [_graph_msg("m9", "2026-03-09T10:00:00Z")],
                    "@odata.nextLink": "/me/messages?$skip=25"}
        with mock.patch.object(crm, "resolve_handle", lambda h: None), \
             mock.patch.object(textindex.channels, "mail_accounts",
                               return_value=[{"email": "me@example.test"}]), \
             mock.patch.object(msgraph, "_graph_request",
                               side_effect=one_page_forever):
            textindex.backfill_graph("gr@example.test", limit=1,
                                     log=lambda *a: None)
        con = textindex._db()
        wm = textindex.get_state(con, "wm_mailg:gr@example.test")
        con.close()
        self.assertIsNone(wm)


def _rfc822(subject, body="hello there, a real body"):
    return (f"From: ann@example.test\r\nTo: me@example.test\r\n"
            f"Subject: {subject}\r\n"
            f"Date: Mon, 15 Jun 2015 10:00:00 +0000\r\n\r\n"
            f"{body}").encode()


class FakeIMAP:
    """Just enough imaplib for backfill_imap: a uid-keyed mailbox."""

    def __init__(self, mailbox):
        self.mailbox = mailbox      # uid -> raw rfc822 bytes

    def login(self, *a):
        return "OK", []

    def select(self, *a, **kw):
        return "OK", []

    def uid(self, cmd, *args):
        if cmd == "search":
            lo = int(args[-1].split(":")[0])
            hits = sorted(u for u in self.mailbox if u >= lo)
            return "OK", [" ".join(map(str, hits)).encode()]
        u = int(args[0])
        return "OK", [(b"h", self.mailbox[u])]

    def logout(self):
        return "OK", []


class PoisonResilienceTests(unittest.TestCase):
    """A malformed message steps the walk past itself; a dying connection
    aborts the batch instead of skipping the rest of the mailbox."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.acct = {"email": "me@example.test", "host": "imap.example.test"}
        patches = [
            mock.patch.object(textindex, "DB",
                              Path(self.tmp.name) / "text-index.sqlite"),
            mock.patch.object(textindex.channels, "mail_accounts",
                              return_value=[self.acct]),
            mock.patch.object(textindex.channels, "imap_special_folder",
                              return_value="INBOX"),
            mock.patch.object(textindex.mailmod, "keychain_password",
                              return_value="pw"),
            mock.patch.object(crm, "resolve_handle", lambda h: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _wm(self):
        con = textindex._db()
        try:
            return int(textindex.get_state(
                con, "wm_mail:me@example.test", 0) or 0)
        finally:
            con.close()

    def _run(self, mailbox, preview):
        with mock.patch.object(textindex.imaplib, "IMAP4_SSL",
                               return_value=FakeIMAP(mailbox)), \
             mock.patch.object(textindex.mailmod, "_body_preview",
                               side_effect=preview):
            return textindex.backfill_imap(self.acct, log=lambda *a: None)

    def test_a_lone_poison_message_is_stepped_past(self):
        mailbox = {1: _rfc822("one"), 2: _rfc822("poison"),
                   3: _rfc822("three")}

        def preview(msg, limit=400):
            if msg.get("Subject") == "poison":
                raise LookupError("unknown encoding: unknown-8bit")
            return "a real body long enough to index"
        n = self._run(mailbox, preview)
        self.assertEqual(n, 2)               # 1 and 3 landed
        self.assertEqual(self._wm(), 3)      # the walk moved past the poison

    def test_repeated_failures_abort_the_batch(self):
        mailbox = {u: _rfc822(f"m{u}") for u in range(1, 8)}

        def preview(msg, limit=400):
            if msg.get("Subject") == "m1":
                return "a real body long enough to index"
            raise OSError("connection reset")
        n = self._run(mailbox, preview)
        self.assertEqual(n, 1)
        # three skips are tolerated, the fourth consecutive failure
        # aborts — the watermark holds there instead of skipping the
        # rest of the mailbox
        self.assertEqual(self._wm(), 4)

    def test_an_unknown_header_charset_no_longer_raises(self):
        from server import mail as mailmod
        raw = "=?unknown-8bit?B?aGVsbG8gd29ybGQ=?="
        self.assertEqual(mailmod._decode_header(raw), "hello world")


class IncrementalSettingTests(unittest.TestCase):
    def test_the_sweep_setting_has_a_default(self):
        # settings.get raises KeyError for a key absent from BOTH config
        # and DEFAULTS — which silently killed the Indexer's mail sweep
        # on every tick before this default existed
        from server import settings
        with mock.patch.object(settings, "raw", return_value={}):
            self.assertFalse(settings.get("mail_body_index"))


def _graph_msg(mid, received):
    return {"id": mid, "internetMessageId": f"<{mid}@x>",
            "subject": "hello", "receivedDateTime": received,
            "from": {"emailAddress": {"address": "ann@example.test"}},
            "toRecipients": [],
            "body": {"contentType": "text", "content": "a real body here"}}


if __name__ == "__main__":
    unittest.main()
