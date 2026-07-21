"""Android companion link: pairing auth, batch dedupe, CRM join, feed
push, pings, triage merge, and the passive-instance write refusal.

All fixtures are synthetic (555-01xx NANP fiction block, example.com).
The secrets ladder is forced onto its temp-file floor so no test ever
touches the machine Keychain.

Run: .venv/bin/python -m unittest tests.test_companion
"""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from server import companion, notify, secrets


PEOPLE = {
    "p_test0000ann1": {"id": "p_test0000ann1", "name": "Ann Example",
                       "handles": {"imessage": ["+12125550123"],
                                   "emails": ["ann@example.com"],
                                   "phones10": ["2125550123"]}},
}
BY_HANDLE = {"2125550123": "p_test0000ann1",
             "ann@example.com": "p_test0000ann1"}


def _resolve(addr):
    if not addr:
        return None
    if "@" in addr:
        return BY_HANDLE.get(addr.lower())
    import re
    d = re.sub(r"\D", "", addr)
    return BY_HANDLE.get(d[-10:] if len(d) >= 10 else d)


class FakeWatcher:
    """The three attributes _push_feed touches, nothing else."""

    def __init__(self):
        self.feed = []
        self.feed_size = 200
        self.listeners = []
        self.lock = threading.Lock()


class CompanionBase(unittest.TestCase):
    """Temp stores + file-floor secrets + synthetic CRM for every test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "data").mkdir()
        patches = [
            mock.patch.object(companion, "STORE",
                              root / "data" / "companion.json"),
            mock.patch.object(companion, "DB",
                              root / "data" / "companion.sqlite"),
            mock.patch.object(secrets.settings, "ROOT", root),
            mock.patch.object(secrets, "_mac_available", return_value=False),
            mock.patch.object(secrets, "IS_WIN", False),
            mock.patch.object(companion.crm, "resolve_handle", _resolve),
            mock.patch.object(
                companion.crm, "_load",
                return_value={"by_id": PEOPLE, "by_handle": BY_HANDLE}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        os.environ.pop("VIRA_PASSIVE", None)

    def pair(self):
        p = companion.pair_start(url="http://hub.example.ts.net:8377")
        companion.pair_complete(p["device_id"], p["token"],
                                name="Test Phone", platform="android 14")
        return p


class PairingTests(CompanionBase):
    def test_pair_start_returns_payload_and_qr(self):
        p = companion.pair_start(url="http://hub.example.ts.net:8377")
        self.assertTrue(p["device_id"].startswith("cd_"))
        self.assertGreaterEqual(len(p["token"]), 32)
        payload = json.loads(p["payload"])
        self.assertEqual(payload["kind"], "vira-pair")
        self.assertEqual(payload["url"], "http://hub.example.ts.net:8377")
        self.assertEqual(payload["device_id"], p["device_id"])

    def test_pending_device_cannot_auth(self):
        p = companion.pair_start(url="http://x")
        self.assertIsNone(companion.auth(p["device_id"], p["token"]))

    def test_claimed_device_auths(self):
        p = self.pair()
        dev = companion.auth(p["device_id"], p["token"])
        self.assertEqual(dev["name"], "Test Phone")
        self.assertFalse(dev["pending"])

    def test_wrong_token_refused(self):
        p = self.pair()
        self.assertIsNone(companion.auth(p["device_id"], "not-the-token"))
        with self.assertRaises(PermissionError):
            companion.pair_complete(p["device_id"], "wrong")

    def test_malformed_device_id_refused(self):
        self.assertIsNone(companion.auth("cd_../../etc", "t"))
        self.assertIsNone(companion.auth("", ""))

    def test_unclaimed_pairing_expires(self):
        p = companion.pair_start(url="http://x")
        with companion._lock:
            data = companion._load()
            data["devices"][0]["created_ts"] = time.time() - 3600
            companion._save(data)
        companion.devices()  # any store read sweeps
        with self.assertRaises(PermissionError):
            companion.pair_complete(p["device_id"], p["token"])
        # the ladder token went with it
        self.assertEqual(
            secrets.get(companion.keychain_service(), p["device_id"]), "")

    def test_unpair_forgets_device_and_token(self):
        p = self.pair()
        companion.unpair(p["device_id"])
        self.assertIsNone(companion.auth(p["device_id"], p["token"]))
        self.assertEqual(companion.devices(), [])

    def test_devices_listing_never_leaks_tokens(self):
        self.pair()
        for d in companion.devices():
            self.assertNotIn("token", json.dumps(d))


class PassiveRefusalTests(CompanionBase):
    """The send.py precedent: a test clone must never pair or ingest."""

    def test_all_writes_refused(self):
        p = self.pair()  # pair while active, then flip passive
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            for call in (
                    lambda: companion.pair_start(url="http://x"),
                    lambda: companion.pair_complete(p["device_id"],
                                                    p["token"]),
                    lambda: companion.ingest(p["device_id"], []),
                    lambda: companion.unpair(p["device_id"])):
                with self.assertRaises(RuntimeError):
                    call()


class IngestTests(CompanionBase):
    def msg(self, **kw):
        base = {"sender": "+12125550123", "text": "lunch tomorrow?",
                "when": 1770000000000, "channel": "sms",
                "direction": "in", "source": "history"}
        base.update(kw)
        return base

    def test_batch_stores_and_counts(self):
        p = self.pair()
        r = companion.ingest(p["device_id"], [
            self.msg(),
            self.msg(text="second", when=1770000500000)])
        self.assertEqual((r["received"], r["new"], r["duplicates"],
                          r["invalid"]), (2, 2, 0, 0))
        self.assertEqual(companion.stats()["messages"], 2)

    def test_exact_reupload_is_duplicate(self):
        p = self.pair()
        companion.ingest(p["device_id"], [self.msg()])
        r = companion.ingest(p["device_id"], [self.msg()])
        self.assertEqual((r["new"], r["duplicates"]), (0, 1))
        self.assertEqual(companion.stats()["messages"], 1)

    def test_near_dupe_seconds_apart_dropped(self):
        # the same SMS seen by the history read and the live notification
        # capture lands twice with timestamps a few seconds apart
        p = self.pair()
        companion.ingest(p["device_id"], [self.msg(when=1770000000000)])
        r = companion.ingest(p["device_id"], [
            self.msg(when=1770000004000, source="live")])
        self.assertEqual((r["new"], r["duplicates"]), (0, 1))

    def test_same_text_later_is_a_new_message(self):
        # "ok" ten minutes apart is two real messages, not a dupe
        p = self.pair()
        companion.ingest(p["device_id"], [self.msg(text="ok")])
        r = companion.ingest(p["device_id"], [
            self.msg(text="ok", when=1770000600000)])
        self.assertEqual(r["new"], 1)

    def test_invalid_rows_counted_not_stored(self):
        p = self.pair()
        r = companion.ingest(p["device_id"], [
            self.msg(text=""), self.msg(sender=""),
            self.msg(channel="carrier-pigeon"), self.msg(when="not-a-date"),
            "not-a-dict"])
        self.assertEqual((r["new"], r["invalid"]), (0, 5))

    def test_iso_and_epoch_seconds_timestamps(self):
        p = self.pair()
        r = companion.ingest(p["device_id"], [
            self.msg(when="2026-01-02T10:00:00"),
            self.msg(text="other", when=1770000000)])
        self.assertEqual(r["new"], 2)

    def test_crm_join_on_ingest(self):
        p = self.pair()
        companion.ingest(p["device_id"], [
            self.msg(),                                   # known: Ann
            self.msg(sender="+13475550188", text="hey")])  # unknown
        self.assertEqual(companion.stats()["unknown_senders"], 1)

    def test_whatsapp_display_name_sender(self):
        p = self.pair()
        r = companion.ingest(p["device_id"], [
            self.msg(sender="Cousin Vera", channel="whatsapp",
                     text="call me back")])
        self.assertEqual(r["new"], 1)
        u = companion.unknown_senders()
        self.assertEqual(u[0]["handle"], "Cousin Vera")


class FeedPushTests(CompanionBase):
    def now_ms(self):
        return int(time.time() * 1000)

    def test_fresh_inbound_reaches_feed_joined(self):
        p = self.pair()
        w = FakeWatcher()
        companion.ingest(p["device_id"], [
            {"sender": "+12125550123", "text": "on my way",
             "when": self.now_ms(), "channel": "sms", "direction": "in",
             "source": "live"}], watcher=w)
        self.assertEqual(len(w.feed), 1)
        item = w.feed[0]
        self.assertEqual(item["channel"], "sms")
        self.assertEqual(item["person_id"], "p_test0000ann1")
        self.assertEqual(item["person_name"], "Ann Example")
        self.assertTrue(item["known"])
        self.assertEqual(item["via"], "companion")

    def test_whatsapp_maps_to_companion_channel(self):
        p = self.pair()
        w = FakeWatcher()
        companion.ingest(p["device_id"], [
            {"sender": "Cousin Vera", "text": "hi",
             "when": self.now_ms(), "channel": "whatsapp",
             "direction": "in", "source": "live"}], watcher=w)
        self.assertEqual(w.feed[0]["channel"], "companion")
        self.assertFalse(w.feed[0]["known"])

    def test_old_history_stays_out_of_feed(self):
        p = self.pair()
        w = FakeWatcher()
        companion.ingest(p["device_id"], [
            {"sender": "+12125550123", "text": "ancient history",
             "when": 1600000000000, "channel": "sms", "direction": "in",
             "source": "history"}], watcher=w)
        self.assertEqual(w.feed, [])
        self.assertEqual(companion.stats()["messages"], 1)

    def test_outbound_stays_out_of_feed(self):
        p = self.pair()
        w = FakeWatcher()
        companion.ingest(p["device_id"], [
            {"sender": "+12125550123", "text": "sent from the phone",
             "when": self.now_ms(), "channel": "sms", "direction": "out",
             "source": "live"}], watcher=w)
        self.assertEqual(w.feed, [])

    def test_listener_gets_the_item(self):
        import queue
        p = self.pair()
        w = FakeWatcher()
        q = queue.Queue()
        w.listeners.append(q)
        companion.ingest(p["device_id"], [
            {"sender": "+12125550123", "text": "ping", "when": self.now_ms(),
             "channel": "sms", "direction": "in", "source": "live"}],
            watcher=w)
        self.assertEqual(q.get_nowait()["text"], "ping")


class UnknownSenderTests(CompanionBase):
    def test_unknowns_grouped_with_texts(self):
        p = self.pair()
        companion.ingest(p["device_id"], [
            {"sender": "+13475550188", "text": "first", "when": 1770000000000,
             "channel": "sms", "direction": "in"},
            {"sender": "+13475550188", "text": "second",
             "when": 1770000600000, "channel": "sms", "direction": "in"}])
        u = companion.unknown_senders()
        self.assertEqual(len(u), 1)
        self.assertEqual(u[0]["msgs"], 2)
        self.assertIn("second", u[0]["texts"])

    def test_naming_in_crm_clears_and_backfills(self):
        p = self.pair()
        companion.ingest(p["device_id"], [
            {"sender": "+13475550188", "text": "hi", "when": 1770000000000,
             "channel": "sms", "direction": "in"}])
        self.assertEqual(len(companion.unknown_senders()), 1)
        # triage names them: the resolver now knows the handle
        BY_HANDLE["3475550188"] = "p_test0000ann1"
        try:
            self.assertEqual(companion.unknown_senders(), [])
            self.assertEqual(companion.stats()["unknown_senders"], 0)
        finally:
            del BY_HANDLE["3475550188"]


class TriageMergeTests(CompanionBase):
    def test_companion_unknowns_join_triage(self):
        from server import triage
        p = self.pair()
        companion.ingest(p["device_id"], [
            {"sender": "+13475550188", "text": "see you at 6",
             "when": 1770000000000, "channel": "sms", "direction": "in"},
            {"sender": "29900",
             "text": "Example Bank Alerts: 111222 is your code.",
             "when": 1770000000000, "channel": "sms", "direction": "in"}])
        with mock.patch.object(triage, "_verdicts", return_value=[]), \
             mock.patch.object(triage, "_dismissed", return_value=set()), \
             mock.patch.object(triage.crm, "_load",
                               return_value={"people": [],
                                             "by_id": PEOPLE,
                                             "by_handle": BY_HANDLE}), \
             mock.patch.object(triage.crm, "resolve_handle", _resolve), \
             mock.patch.object(triage, "_recent_inbound", return_value=[]):
            cands = triage.candidates()
        handles = {c["handle"] for c in cands}
        self.assertIn("+13475550188", handles)
        self.assertIn("29900", handles)
        person = next(c for c in cands if c["handle"] == "+13475550188")
        self.assertFalse(person["business"])
        self.assertEqual(person["action"], "needs_name")
        bank = next(c for c in cands if c["handle"] == "29900")
        self.assertTrue(bank["business"])          # short code + OTP wording
        self.assertEqual(bank["company_guess"], "Example Bank")
        # businesses band after people
        self.assertGreater(cands.index(bank), cands.index(person))

    def test_dismissed_companion_handle_stays_out(self):
        from server import triage
        p = self.pair()
        companion.ingest(p["device_id"], [
            {"sender": "+13475550188", "text": "hello", "when": 1770000000000,
             "channel": "sms", "direction": "in"}])
        with mock.patch.object(triage, "_verdicts", return_value=[]), \
             mock.patch.object(triage, "_dismissed",
                               return_value={"+13475550188"}), \
             mock.patch.object(triage.crm, "_load",
                               return_value={"people": [],
                                             "by_id": PEOPLE,
                                             "by_handle": BY_HANDLE}), \
             mock.patch.object(triage.crm, "resolve_handle", _resolve), \
             mock.patch.object(triage, "_recent_inbound", return_value=[]):
            self.assertEqual(triage.candidates(), [])


class PingTests(CompanionBase):
    def test_no_devices_no_ping(self):
        self.assertFalse(companion.queue_ping("hello"))

    def test_queue_and_fetch(self):
        self.pair()
        self.assertTrue(companion.queue_ping("Vira: Ann emailed — lunch"))
        pings = companion.pings_since(0)
        self.assertEqual(len(pings), 1)
        self.assertEqual(pings[0]["id"], 1)
        # after-id filtering
        companion.queue_ping("second")
        self.assertEqual([p["text"] for p in companion.pings_since(1)],
                         ["second"])

    def test_wait_returns_immediately_when_ready(self):
        self.pair()
        companion.queue_ping("already here")
        t0 = time.time()
        got = companion.wait_for_pings(0, timeout_s=10)
        self.assertLess(time.time() - t0, 1)
        self.assertEqual(len(got), 1)

    def test_wait_times_out_empty(self):
        self.pair()
        t0 = time.time()
        got = companion.wait_for_pings(0, timeout_s=1)
        self.assertGreaterEqual(time.time() - t0, 0.9)
        self.assertEqual(got, [])

    def test_ping_log_capped(self):
        self.pair()
        for i in range(companion.PING_KEEP + 10):
            companion.queue_ping(f"ping {i}")
        self.assertLessEqual(len(companion.pings_since(0)),
                             companion.PING_KEEP)


class NotifyChannelTests(CompanionBase):
    """The companion phone is a notification channel: pings deliver where
    iMessage is unconfigured (or not a Mac at all)."""

    def test_send_delivers_via_companion_without_handle(self):
        self.pair()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(notify, "LOG", Path(d) / "log.json"):
                notify._send("", "Vira: test ping", {"channel": "email"})
                entry = notify.recent(1)[0]
        self.assertTrue(entry["ok"])
        self.assertEqual(entry["via"], ["companion"])
        self.assertEqual(companion.pings_since(0)[0]["text"],
                         "Vira: test ping")

    def test_send_without_any_channel_fails_honestly(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(notify, "LOG", Path(d) / "log.json"):
                notify._send("", "nowhere to go", {"channel": "email"})
                entry = notify.recent(1)[0]
        self.assertFalse(entry["ok"])

    def test_agent_ping_active_with_paired_device_only(self):
        self.pair()
        with tempfile.TemporaryDirectory() as d:
            cfg = {"enabled": True, "handle": "", "tier": "active"}
            with mock.patch.object(notify, "LOG", Path(d) / "log.json"), \
                 mock.patch.object(notify, "config", return_value=cfg), \
                 mock.patch.object(notify, "_throttled", return_value=None):
                self.assertTrue(notify.agent_ping("circuit finished"))
                time.sleep(0.3)  # _send runs on a daemon thread
        self.assertTrue(any(p["text"] == "circuit finished"
                            for p in companion.pings_since(0)))

    def test_agent_ping_dormant_with_no_channel(self):
        cfg = {"enabled": True, "handle": "", "tier": "active"}
        with mock.patch.object(notify, "config", return_value=cfg):
            self.assertFalse(notify.agent_ping("nobody to tell"))


class RouteTests(CompanionBase):
    """The thin HTTP layer: header auth, Bearer extraction, passive 403.
    TestClient without the context manager runs no lifespan — no watchers
    start."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from server import main
        cls.client = TestClient(main.app)

    def test_happy_path_pair_push_pings(self):
        r = self.client.post("/api/companion/pair/start")
        self.assertEqual(r.status_code, 200)
        p = r.json()
        r = self.client.post("/api/companion/pair", json={
            "device_id": p["device_id"], "token": p["token"],
            "name": "Route Phone", "platform": "android 14"})
        self.assertEqual(r.status_code, 200)
        headers = {"X-Vira-Device": p["device_id"],
                   "Authorization": "Bearer " + p["token"]}
        r = self.client.post("/api/companion/messages", headers=headers,
                             json={"messages": [
                                 {"sender": "+12125550123", "text": "hi",
                                  "when": 1770000000000, "channel": "sms",
                                  "direction": "in", "source": "history"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["new"], 1)
        r = self.client.get("/api/companion/pings?after=0&wait=0",
                            headers=headers)
        self.assertEqual(r.status_code, 200)
        st = self.client.get("/api/companion/status").json()
        self.assertEqual(st["messages"], 1)
        self.assertTrue(any(d["name"] == "Route Phone"
                            for d in st["devices"]))

    def test_bad_auth_is_401(self):
        p = self.pair()
        for headers in (
                {},
                {"X-Vira-Device": p["device_id"],
                 "Authorization": "Bearer wrong"},
                {"X-Vira-Device": p["device_id"],
                 "Authorization": p["token"]}):     # missing Bearer scheme
            r = self.client.post("/api/companion/messages",
                                 headers=headers, json={"messages": []})
            self.assertEqual(r.status_code, 401)

    def test_passive_routes_403(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            r = self.client.post("/api/companion/pair/start")
            self.assertEqual(r.status_code, 403)

    def test_oversize_batch_413(self):
        p = self.pair()
        headers = {"X-Vira-Device": p["device_id"],
                   "Authorization": "Bearer " + p["token"]}
        msgs = [{"sender": "s", "text": "x", "when": 1770000000000,
                 "channel": "sms", "direction": "in"}] * 501
        r = self.client.post("/api/companion/messages", headers=headers,
                             json={"messages": msgs})
        self.assertEqual(r.status_code, 413)


class HubUrlTests(CompanionBase):
    def test_config_override_wins(self):
        with mock.patch.object(companion.settings, "raw", return_value={
                "companion_hub_url": "http://hub.example.ts.net:8377"}):
            self.assertEqual(companion.hub_url(),
                             "http://hub.example.ts.net:8377")

    def test_norm_sender_shapes(self):
        self.assertEqual(companion.norm_sender("+1 (212) 555-0123"),
                         "2125550123")
        self.assertEqual(companion.norm_sender("Ann@Example.COM"),
                         "ann@example.com")
        self.assertEqual(companion.norm_sender("29900"), "29900")
        self.assertEqual(companion.norm_sender("Cousin Vera"), "cousin vera")


if __name__ == "__main__":
    unittest.main()
