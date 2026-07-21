"""WhatsApp connector: watcher logic against a stubbed sidecar.

What must hold: inbound rows become feed items joined to CRM people by
phone digits; own sends and protocol noise are skipped; the first run
baselines and emits nothing old; a re-served row never duplicates in the
feed; and a passive instance never starts the sidecar, never touches the
on-disk cursor, and never runs the watcher thread.

Fixtures use the UK Ofcom fictional mobile range (447700900xxx) — the PII
guard forbids real-shaped +1 numbers.

Run: .venv/bin/python -m unittest tests.test_whatsapp
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from server import main, photos, settings, whatsapp
from server import data as crm


ROW_TEXT = {
    "id": "A1B2C3D4E5", "chat_jid": "447700900123@s.whatsapp.net",
    "sender_jid": "447700900123@s.whatsapp.net", "sender_pn": None,
    "from_me": False, "timestamp": 1753000000, "kind": "text",
    "text": "See you at the match", "push_name": "Fiona Fixture",
    "group": False, "group_subject": None,
}
ROW_FROM_ME = dict(ROW_TEXT, id="B2C3D4E5F6", from_me=True,
                   text="On my way")
ROW_IMAGE = dict(ROW_TEXT, id="C3D4E5F607", kind="image", text="",
                 sender_jid="447700900456:12@s.whatsapp.net")
ROW_GROUP = {
    "id": "D4E5F60718", "chat_jid": "120363000000000001@g.us",
    "sender_jid": "9009000000009@lid",
    "sender_pn": "447700900123@s.whatsapp.net",
    "from_me": False, "timestamp": 1753000100, "kind": "text",
    "text": "Sunday still on?", "push_name": "Fiona Fixture",
    "group": True, "group_subject": "Five-a-side",
}


class FakeShared:
    """The slice of imessage.Watcher the connector touches."""

    def __init__(self):
        self.lock = threading.Lock()
        self.feed = []
        self.feed_size = 200
        self.listeners = []


def _no_passive_env():
    return {k: v for k, v in os.environ.items() if k != "VIRA_PASSIVE"}


class DigitsTests(unittest.TestCase):
    def test_plain_jid(self):
        self.assertEqual(whatsapp._digits("447700900123@s.whatsapp.net"),
                         "447700900123")

    def test_device_suffix(self):
        self.assertEqual(whatsapp._digits("447700900456:12@s.whatsapp.net"),
                         "447700900456")

    def test_bare_and_empty(self):
        self.assertEqual(whatsapp._digits("+447700900123"), "447700900123")
        self.assertEqual(whatsapp._digits(None), "")


class ItemShapeTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(crm, "resolve_handle",
                              side_effect=lambda d: "p_fixture1"
                              if d.endswith("447700900123") else None),
            mock.patch.object(crm, "_load", return_value={
                "by_id": {"p_fixture1": {"name": "Fiona Fixture"}}}),
            mock.patch.object(photos, "photo_path", return_value=None),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_known_sender_joins_crm(self):
        it = whatsapp._to_item(ROW_TEXT)
        self.assertEqual(it["channel"], "whatsapp")
        self.assertEqual(it["rowid"], "wa-A1B2C3D4E5")
        self.assertEqual(it["person_id"], "p_fixture1")
        self.assertEqual(it["person_name"], "Fiona Fixture")
        self.assertTrue(it["known"])
        self.assertEqual(it["handle"], "+447700900123")
        self.assertEqual(it["text"], "See you at the match")
        self.assertFalse(it["group"])
        self.assertIn("T", it["when"])   # ISO timestamp

    def test_own_send_skipped(self):
        self.assertIsNone(whatsapp._to_item(ROW_FROM_ME))

    def test_media_placeholder_and_unknown_sender(self):
        it = whatsapp._to_item(ROW_IMAGE)
        self.assertEqual(it["text"], "[photo]")
        self.assertIsNone(it["person_id"])
        self.assertFalse(it["known"])
        self.assertEqual(it["person_name"], "Fiona Fixture")  # push name

    def test_group_prefers_real_number_over_lid(self):
        it = whatsapp._to_item(ROW_GROUP)
        self.assertEqual(it["person_id"], "p_fixture1")   # via sender_pn
        self.assertTrue(it["group"])
        self.assertEqual(it["group_name"], "Five-a-side")


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "whatsapp-state.json"
        self.bridge = {"/status": {"connected": True, "inbox_bytes": 0,
                                   "jid": "447700900999@s.whatsapp.net"}}
        self.patches = [
            mock.patch.dict(os.environ, _no_passive_env(), clear=True),
            mock.patch.object(whatsapp, "STATE", self.state),
            mock.patch.object(whatsapp, "_bridge_get",
                              side_effect=lambda p, timeout=4:
                              self.bridge.get(p.split("?")[0])),
            mock.patch.object(crm, "resolve_handle", return_value=None),
            mock.patch.object(crm, "_load", return_value={"by_id": {}}),
            mock.patch.object(photos, "photo_path", return_value=None),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        whatsapp._mem_cursor["cursor"] = None

    def test_first_run_baselines_then_ingests_then_dedups(self):
        shared = FakeShared()
        self.bridge["/status"]["inbox_bytes"] = 120

        first = whatsapp.ingest(shared)
        self.assertTrue(first.get("baselined"))
        self.assertEqual(first["cursor"], 120)
        self.assertEqual(shared.feed, [])

        self.bridge["/messages"] = {
            "messages": [ROW_TEXT, ROW_FROM_ME, ROW_IMAGE], "cursor": 480}
        second = whatsapp.ingest(shared)
        self.assertEqual(second["ingested"], 2)   # from_me skipped
        self.assertEqual(second["cursor"], 480)
        self.assertEqual([i["rowid"] for i in shared.feed],
                         ["wa-A1B2C3D4E5", "wa-C3D4E5F607"])
        self.assertEqual(json.loads(self.state.read_text())["cursor"], 480)

        third = whatsapp.ingest(shared)          # sidecar re-serves the rows
        self.assertEqual(third["ingested"], 0)
        self.assertEqual(len(shared.feed), 2)

    def test_listeners_get_new_items(self):
        import queue
        shared = FakeShared()
        q = queue.Queue()
        shared.listeners.append(q)
        self.bridge["/status"]["inbox_bytes"] = 0
        whatsapp.ingest(shared)                  # baseline
        self.bridge["/messages"] = {"messages": [ROW_TEXT], "cursor": 90}
        whatsapp.ingest(shared)
        self.assertEqual(q.get_nowait()["rowid"], "wa-A1B2C3D4E5")

    def test_unreachable_sidecar_raises(self):
        self.bridge.clear()
        with self.assertRaises(RuntimeError):
            whatsapp.ingest(FakeShared())


class PassiveTests(unittest.TestCase):
    """A test copy must never act on the world: no sidecar spawn, no
    watcher thread, no on-disk cursor writes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "whatsapp-state.json"
        self.patches = [
            mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}),
            mock.patch.object(whatsapp, "STATE", self.state),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        whatsapp._mem_cursor["cursor"] = None

    def test_never_spawns_sidecar(self):
        with mock.patch.object(whatsapp, "_bridge_get", return_value=None), \
             mock.patch.object(whatsapp.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "passive"):
                whatsapp.ensure_sidecar()
            popen.assert_not_called()

    def test_reads_running_sidecar_without_spawning(self):
        st = {"connected": False, "needs_pair": True}
        with mock.patch.object(whatsapp, "_bridge_get", return_value=st), \
             mock.patch.object(whatsapp.subprocess, "Popen") as popen:
            self.assertEqual(whatsapp.ensure_sidecar(), st)
            popen.assert_not_called()

    def test_cursor_stays_in_memory(self):
        bridge = {"/status": {"connected": True, "inbox_bytes": 77}}
        with mock.patch.object(whatsapp, "_bridge_get",
                               side_effect=lambda p, timeout=4:
                               bridge.get(p.split("?")[0])):
            res = whatsapp.ingest(FakeShared())
        self.assertTrue(res.get("baselined"))
        self.assertEqual(whatsapp._mem_cursor["cursor"], 77)
        self.assertFalse(self.state.exists())

    def test_watcher_start_declines(self):
        w = whatsapp.WhatsAppWatcher(FakeShared(), poll_seconds=1)
        w.start()
        self.assertEqual(w.status["state"], "passive")
        self.assertNotIn("vira-whatsapp",
                         [t.name for t in threading.enumerate()])

    def test_pair_route_refuses(self):
        with mock.patch.object(whatsapp, "_bridge_get", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                main.api_whatsapp_pair()
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("passive", ctx.exception.detail)


class SurfaceTests(unittest.TestCase):
    def test_settings_defaults(self):
        self.assertEqual(int(settings.get("whatsapp_bridge_port")), 18377)
        self.assertEqual(int(settings.get("whatsapp_poll_seconds")), 5)

    def test_status_route_shape(self):
        with mock.patch.object(whatsapp, "_bridge_get", return_value=None):
            st = main.api_whatsapp_status()
        for key in ("linked", "installed", "passive", "watcher", "sidecar"):
            self.assertIn(key, st)
        self.assertIsNone(st["sidecar"])

    def test_feed_carries_whatsapp_status(self):
        self.assertIn("whatsapp", main.api_feed(limit=1))


if __name__ == "__main__":
    unittest.main()
