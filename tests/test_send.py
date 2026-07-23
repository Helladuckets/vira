"""SMS fallback for non-iMessage (Android) recipients: the channel
preference store, chat.db capability inference, channel resolution, and the
proactive/reactive send behavior in server.send.

No osascript is ever run and no real chat.db is touched — AppleScript sends
are mocked and the capability/error probes read a synthetic sqlite with the
same columns the real chat.db has.

Run: .venv/bin/python -m unittest tests.test_send
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import imessage, send, sendpref, settings


def _chat_db(path):
    """A miniature chat.db: one iMessage-capable handle, one SMS-only
    handle, and outbound rows (one clean, one errored)."""
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE handle(ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
      CREATE TABLE message(ROWID INTEGER PRIMARY KEY, date INT,
                           is_from_me INT, handle_id INT, service TEXT,
                           error INT DEFAULT 0, is_delivered INT DEFAULT 0);
    """)
    # +12025550142 is reachable on iMessage; +12025550143 only over SMS.
    con.execute("INSERT INTO handle VALUES(1, '+12025550142', 'iMessage')")
    con.execute("INSERT INTO handle VALUES(2, '+12025550142', 'SMS')")
    con.execute("INSERT INTO handle VALUES(3, '+12025550143', 'SMS')")
    con.commit()
    con.close()


class ChannelStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "send-channels.json"
        p = mock.patch.object(sendpref, "STATE", self.state)
        p.start()
        self.addCleanup(p.stop)

    def test_set_get_clear(self):
        self.assertIsNone(sendpref.get("p_a"))
        sendpref.set_channel("p_a", "sms")
        self.assertEqual(sendpref.get("p_a")["channel"], "sms")
        self.assertEqual(sendpref.get("p_a")["source"], "owner")
        sendpref.set_channel("p_a", None)          # clear back to auto
        self.assertIsNone(sendpref.get("p_a"))

    def test_owner_mark_beats_later_inference(self):
        sendpref.set_channel("p_a", "imessage", source="owner")
        sendpref.set_channel("p_a", "sms", source="inferred")
        self.assertEqual(sendpref.get("p_a")["channel"], "imessage")

    def test_inference_is_overwritten_by_owner(self):
        sendpref.set_channel("p_a", "sms", source="inferred")
        sendpref.set_channel("p_a", "imessage", source="owner")
        self.assertEqual(sendpref.get("p_a")["channel"], "imessage")
        self.assertEqual(sendpref.get("p_a")["source"], "owner")

    def test_bad_channel_raises(self):
        with self.assertRaises(ValueError):
            sendpref.set_channel("p_a", "carrier-pigeon")


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "chat.db"
        _chat_db(self.db)
        patches = [
            mock.patch.object(settings, "IS_MAC", True),
            mock.patch.object(settings, "fixture_mode", lambda: False),
            mock.patch.object(
                imessage, "_connect",
                lambda: sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_imessage_handle_is_capable(self):
        self.assertIs(send.imessage_capable("+12025550142"), True)

    def test_sms_only_handle_is_not_capable(self):
        self.assertIs(send.imessage_capable("+12025550143"), False)

    def test_unknown_handle_is_none(self):
        self.assertIsNone(send.imessage_capable("+12025550100"))

    def test_fixture_mode_short_circuits(self):
        with mock.patch.object(settings, "fixture_mode", lambda: True):
            self.assertIsNone(send.imessage_capable("+12025550143"))


class ResolveChannelTests(CapabilityTests):
    def setUp(self):
        super().setUp()
        st = Path(self.tmp.name) / "send-channels.json"
        p = mock.patch.object(sendpref, "STATE", st)
        p.start()
        self.addCleanup(p.stop)

    def test_override_wins(self):
        self.assertEqual(
            send.resolve_channel("p_a", "+12025550142", override="sms"), "sms")

    def test_pref_beats_inference(self):
        sendpref.set_channel("p_a", "sms")
        self.assertEqual(
            send.resolve_channel("p_a", "+12025550142"), "sms")

    def test_auto_infers_sms_for_sms_only(self):
        self.assertEqual(
            send.resolve_channel("p_b", "+12025550143"), "sms")

    def test_auto_defaults_imessage(self):
        self.assertEqual(
            send.resolve_channel("p_c", "+12025550142"), "imessage")


class SendMessageTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("VIRA_PASSIVE", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        st = Path(self.tmp.name) / "send-channels.json"
        self.sent = []  # (target, text, channel)
        patches = [
            mock.patch.object(settings, "IS_MAC", True),
            mock.patch.object(settings, "fixture_mode", lambda: False),
            mock.patch.object(sendpref, "STATE", st),
            mock.patch.object(
                send, "_osa_send",
                lambda t, x, ch, timeout=20: self.sent.append((t, x, ch))),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_proactive_sms_from_pref(self):
        sendpref.set_channel("p_a", "sms")
        with mock.patch.object(send, "resolve_channel", return_value="sms"):
            r = send.send_message("hi", person_id="p_a", handle="+1555")
        self.assertEqual(r["channel"], "sms")
        self.assertFalse(r["downgraded"])
        self.assertEqual(self.sent, [("+1555", "hi", "sms")])

    def test_imessage_clean_no_fallback(self):
        with mock.patch.object(send, "resolve_channel", return_value="imessage"), \
             mock.patch.object(send, "_imessage_errored", return_value=False):
            r = send.send_message("hi", person_id="p_a", handle="+1555")
        self.assertEqual(r["channel"], "imessage")
        self.assertFalse(r["downgraded"])
        self.assertEqual([c for _, _, c in self.sent], ["imessage"])

    def test_imessage_error_falls_back_and_remembers(self):
        with mock.patch.object(send, "resolve_channel", return_value="imessage"), \
             mock.patch.object(send, "_imessage_errored", return_value=True):
            r = send.send_message("hi", person_id="p_a", handle="+1555")
        self.assertEqual(r["channel"], "sms")
        self.assertTrue(r["downgraded"])
        # Sent twice: iMessage first, then the SMS fallback.
        self.assertEqual([c for _, _, c in self.sent], ["imessage", "sms"])
        # And it remembered, so next time is proactive.
        pref = sendpref.get("p_a")
        self.assertEqual(pref["channel"], "sms")
        self.assertEqual(pref["source"], "inferred")

    def test_fallback_when_no_sms_route_reports_honestly(self):
        calls = {"n": 0}

        def osa(t, x, ch, timeout=20):
            calls["n"] += 1
            if ch == "sms":
                raise RuntimeError("no SMS account")
            self.sent.append((t, x, ch))

        with mock.patch.object(send, "resolve_channel", return_value="imessage"), \
             mock.patch.object(send, "_imessage_errored", return_value=True), \
             mock.patch.object(send, "_osa_send", osa):
            r = send.send_message("hi", person_id="p_a", handle="+1555")
        self.assertEqual(r["channel"], "imessage")
        self.assertFalse(r["downgraded"])
        self.assertIn("SMS", r["note"])

    def test_explicit_sms_missing_route_raises_clear(self):
        def osa(t, x, ch, timeout=20):
            raise RuntimeError("cant find SMS account")
        with mock.patch.object(send, "_osa_send", osa):
            with self.assertRaises(RuntimeError) as cm:
                send.send_message("hi", handle="+1555", channel="sms")
        self.assertIn("Text Message Forwarding", str(cm.exception))

    def test_empty_text_rejected(self):
        with self.assertRaises(ValueError):
            send.send_message("   ", handle="+1555")

    def test_no_handle_rejected(self):
        with self.assertRaises(ValueError):
            send.send_message("hi")

    def test_passive_blocks(self):
        os.environ["VIRA_PASSIVE"] = "1"
        self.addCleanup(lambda: os.environ.pop("VIRA_PASSIVE", None))
        with self.assertRaises(RuntimeError):
            send.send_message("hi", handle="+1555")

    def test_send_imessage_wrapper_returns_handle(self):
        with mock.patch.object(send, "resolve_channel", return_value="imessage"), \
             mock.patch.object(send, "_imessage_errored", return_value=False):
            used = send.send_imessage("hi", handle="+1555")
        self.assertEqual(used, "+1555")


class ErrorProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "chat.db"
        con = sqlite3.connect(self.db)
        con.executescript("""
          CREATE TABLE handle(ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
          CREATE TABLE message(ROWID INTEGER PRIMARY KEY, date INT,
                               is_from_me INT, handle_id INT, service TEXT,
                               error INT DEFAULT 0, is_delivered INT DEFAULT 0);
        """)
        con.execute("INSERT INTO handle VALUES(1, '+1555', 'iMessage')")
        con.commit()
        con.close()
        patches = [
            mock.patch.object(settings, "IS_MAC", True),
            mock.patch.object(settings, "fixture_mode", lambda: False),
            mock.patch.object(
                imessage, "_connect",
                lambda: sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _add(self, rowid, date, error):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO message VALUES(?,?,?,?,?,?,?)",
                    (rowid, date, 1, 1, "iMessage", error, 0))
        con.commit()
        con.close()

    def test_clean_send_no_error(self):
        self._add(1, 1000, 0)
        self.assertFalse(send._imessage_errored("+1555", 500, 0))

    def test_errored_send_detected(self):
        self._add(1, 1000, 22)
        self.assertTrue(send._imessage_errored("+1555", 500, 0))

    def test_delivery_receipt_returns_early(self):
        # is_delivered set: a clean send with a delivery receipt is a
        # definite success, so the probe returns False without waiting out
        # the (deliberately long) window.
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO message VALUES(1, 1000, 1, 1, 'iMessage', 0, 1)")
        con.commit()
        con.close()
        import time as _t
        t0 = _t.monotonic()
        self.assertFalse(send._imessage_errored("+1555", 500, 30))
        self.assertLess(_t.monotonic() - t0, 5)


if __name__ == "__main__":
    unittest.main()
