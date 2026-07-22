"""Shared channel-mechanics tests: the feed-push contract (dedupe /
sort / cap / dead-queue drop), the first-run baseline helper and its
IMAP + Graph newest probes, the tolerant mail-accounts reader, and the
RFC 6154 special-use folder finder — all on synthetic fixtures.

Run: .venv/bin/python -m unittest tests.test_channels
"""
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from server import channels


class FakeShared:
    """The four attributes push_feed_item touches, nothing else."""

    def __init__(self, feed_size=200):
        self.feed = []
        self.feed_size = feed_size
        self.listeners = []
        self.lock = threading.Lock()


def _item(rowid, when):
    return {"rowid": rowid, "when": when, "text": f"t-{rowid}"}


class PushFeedItemTests(unittest.TestCase):
    def test_append_and_return_value(self):
        s = FakeShared()
        self.assertTrue(channels.push_feed_item(s, _item("a", "2026-01-01")))
        self.assertEqual([x["rowid"] for x in s.feed], ["a"])

    def test_rowid_dedupe(self):
        s = FakeShared()
        channels.push_feed_item(s, _item("a", "2026-01-01"))
        self.assertFalse(channels.push_feed_item(s, _item("a", "2026-02-02")))
        self.assertEqual(len(s.feed), 1)
        self.assertEqual(s.feed[0]["when"], "2026-01-01")   # original kept

    def test_sorted_by_when(self):
        s = FakeShared()
        channels.push_feed_item(s, _item("b", "2026-03-01"))
        channels.push_feed_item(s, _item("a", "2026-01-01"))
        channels.push_feed_item(s, _item("c", "2026-02-01"))
        self.assertEqual([x["rowid"] for x in s.feed], ["a", "c", "b"])

    def test_none_when_sorts_first(self):
        s = FakeShared()
        channels.push_feed_item(s, _item("a", "2026-01-01"))
        channels.push_feed_item(s, {"rowid": "n", "when": None})
        self.assertEqual([x["rowid"] for x in s.feed], ["n", "a"])

    def test_cap_drops_oldest(self):
        s = FakeShared(feed_size=2)
        for i, when in enumerate(["2026-01-01", "2026-01-02", "2026-01-03"]):
            channels.push_feed_item(s, _item(f"r{i}", when))
        self.assertEqual([x["rowid"] for x in s.feed], ["r1", "r2"])

    def test_listeners_woken_and_dead_queue_dropped(self):
        s = FakeShared()

        class DeadQueue:
            def put_nowait(self, item):
                raise RuntimeError("client gone")

        live = queue.Queue()
        dead = DeadQueue()
        s.listeners = [dead, live]
        it = _item("a", "2026-01-01")
        channels.push_feed_item(s, it)
        self.assertEqual(live.get_nowait(), it)
        self.assertEqual(s.listeners, [live])     # dead queue removed


class FirstRunBaselineTests(unittest.TestCase):
    def test_absent_watermark_baselines_at_newest(self):
        wm, baselined = channels.first_run_baseline(None, lambda: 41)
        self.assertEqual((wm, baselined), (41, True))

    def test_present_watermark_never_calls_newest(self):
        def boom():
            raise AssertionError("newest() must not run")
        wm, baselined = channels.first_run_baseline(7, boom)
        self.assertEqual((wm, baselined), (7, False))

    def test_zero_watermark_is_a_watermark(self):
        # 0 is a valid baseline (empty mailbox) — only None means unset
        wm, baselined = channels.first_run_baseline(0, lambda: 99)
        self.assertEqual((wm, baselined), (0, False))


class FakeImap:
    def __init__(self, status_lines=None, list_lines=None,
                 status_code="OK", list_code="OK"):
        self._status = (status_code, status_lines or [b""])
        self._list = (list_code, list_lines)
        self.status_calls = []

    def status(self, mailbox, what):
        self.status_calls.append((mailbox, what))
        return self._status

    def list(self):
        return self._list


class ImapNewestUidTests(unittest.TestCase):
    def test_parses_uidnext(self):
        con = FakeImap(status_lines=[b'"INBOX" (UIDNEXT 4321)'])
        self.assertEqual(channels.imap_newest_uid(con, "INBOX"), 4320)
        self.assertEqual(con.status_calls, [("INBOX", "(UIDNEXT)")])

    def test_unparseable_reads_zero(self):
        con = FakeImap(status_lines=[b"NO UIDNEXT HERE"])
        self.assertEqual(channels.imap_newest_uid(con, "INBOX"), 0)


class GraphNewestReceivedTests(unittest.TestCase):
    def test_newest_message(self):
        with mock.patch("server.msgraph._graph_request",
                        return_value={"value": [
                            {"receivedDateTime": "2026-07-20T09:00:00Z"}]}) \
                as gr:
            got = channels.graph_newest_received(
                "owner@example.com", "/me/messages")
        self.assertEqual(got, "2026-07-20T09:00:00Z")
        path = gr.call_args[0][1]
        self.assertTrue(path.startswith("/me/messages?"))
        self.assertIn("$top=1", path)

    def test_empty_mailbox_reads_epoch(self):
        with mock.patch("server.msgraph._graph_request",
                        return_value={"value": []}):
            got = channels.graph_newest_received(
                "owner@example.com", "/me/mailFolders/inbox/messages")
        self.assertEqual(got, "1970-01-01T00:00:00Z")


class MailAccountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "mail-accounts.json"

    def test_bare_list(self):
        rows = [{"email": "a@example.com", "host": "imap.example.com"}]
        self.path.write_text(json.dumps(rows))
        self.assertEqual(channels.mail_accounts(self.path), rows)

    def test_wrapped_shape(self):
        rows = [{"email": "a@example.com", "type": "graph"}]
        self.path.write_text(json.dumps({"accounts": rows}))
        self.assertEqual(channels.mail_accounts(self.path), rows)

    def test_missing_and_corrupt_read_empty(self):
        self.assertEqual(channels.mail_accounts(self.path), [])
        self.path.write_text("{nope")
        self.assertEqual(channels.mail_accounts(self.path), [])

    def test_graph_filter(self):
        rows = [{"email": "a@example.com", "host": "h.example.com"},
                {"email": "b@example.com", "type": "graph"}]
        self.path.write_text(json.dumps(rows))
        self.assertEqual(channels.graph_accounts(self.path),
                         [{"email": "b@example.com", "type": "graph"}])


GMAIL_LIST = [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
    b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"',
    b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/Drafts"',
]


class ImapSpecialFolderTests(unittest.TestCase):
    def test_finds_drafts_flag(self):
        con = FakeImap(list_lines=GMAIL_LIST)
        self.assertEqual(
            channels.imap_special_folder(con, "\\Drafts", "Drafts"),
            "[Gmail]/Drafts")

    def test_finds_all_flag(self):
        con = FakeImap(list_lines=GMAIL_LIST)
        self.assertEqual(
            channels.imap_special_folder(con, "\\All", "INBOX"),
            "[Gmail]/All Mail")

    def test_no_flag_falls_back(self):
        con = FakeImap(list_lines=[b'(\\HasNoChildren) "/" "INBOX"'])
        self.assertEqual(
            channels.imap_special_folder(con, "\\Drafts", "Drafts"),
            "Drafts")

    def test_bad_status_falls_back(self):
        con = FakeImap(list_lines=GMAIL_LIST, list_code="NO")
        self.assertEqual(
            channels.imap_special_folder(con, "\\All", "INBOX"), "INBOX")

    def test_str_lines_tolerated(self):
        con = FakeImap(list_lines=['(\\Drafts) "/" "Entwuerfe"'])
        self.assertEqual(
            channels.imap_special_folder(con, "\\Drafts", "Drafts"),
            "Entwuerfe")


if __name__ == "__main__":
    unittest.main()
