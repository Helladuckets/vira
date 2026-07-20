"""AI-backend health — the deterministic self-check (classify, probe, the
fallback ladder, the store transitions, and the green->red alert edge).

Everything here runs without touching the model: subprocess and the notify
path are stubbed, so the tests exercise the exact code that must work when the
model itself is unreachable.

Run: .venv/bin/python -m unittest tests.test_aihealth
"""
import json
import tempfile
import unittest
from pathlib import Path

from server import aihealth


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self._state = aihealth.STATE
        self._config = aihealth.CONFIG
        aihealth.STATE = d / "ai-health.json"
        aihealth.CONFIG = d / "config.json"
        aihealth.CONFIG.write_text("{}")  # all defaults

    def tearDown(self):
        aihealth.STATE = self._state
        aihealth.CONFIG = self._config
        self._tmp.cleanup()

    def _cfg(self, **kw):
        aihealth.CONFIG.write_text(json.dumps(kw))


class ClassifyTests(Base):
    def test_oauth_expiry_is_auth_needs_reauth(self):
        info = aihealth.classify(
            "Failed to authenticate: OAuth session expired and could not be refreshed")
        self.assertEqual(info["kind"], "auth")
        self.assertTrue(info["needs_reauth"])
        self.assertIn("claude auth login", info["message"])
        self.assertIn("kept", info["message"])  # never-lose-work promise

    def test_credit_is_not_reauth(self):
        info = aihealth.classify("API error: Credit balance is too low")
        self.assertEqual(info["kind"], "credit")
        self.assertFalse(info["needs_reauth"])

    def test_unknown_error_is_other(self):
        info = aihealth.classify("connection reset by peer")
        self.assertEqual(info["kind"], "other")
        self.assertFalse(info["needs_reauth"])

    def test_none_is_safe(self):
        self.assertEqual(aihealth.classify(None)["kind"], "other")

    def test_is_auth_failure_signatures(self):
        self.assertTrue(aihealth.is_auth_failure("Invalid API key"))
        self.assertTrue(aihealth.is_auth_failure("not logged in"))
        self.assertFalse(aihealth.is_auth_failure("rate_limit_error"))


class LadderTests(Base):
    def test_dead_cli_with_key_routes_to_api(self):
        aihealth._record({"state": "red", "backend": "cli"})
        self.assertEqual(aihealth.preferred_backend("cli", "sk-key"), "api")

    def test_dead_cli_without_key_stays_cli(self):
        aihealth._record({"state": "red", "backend": "cli"})
        self.assertEqual(aihealth.preferred_backend("cli", ""), "cli")

    def test_healthy_cli_stays_cli(self):
        aihealth._record({"state": "green", "backend": "cli"})
        self.assertEqual(aihealth.preferred_backend("cli", "sk-key"), "cli")

    def test_absent_store_trusts_configured(self):
        self.assertEqual(aihealth.preferred_backend("cli", "sk-key"), "cli")

    def test_api_configured_is_untouched(self):
        aihealth._record({"state": "red", "backend": "cli"})
        self.assertEqual(aihealth.preferred_backend("api", "sk-key"), "api")


class StoreTests(Base):
    def test_transition_records_history_and_changed_at(self):
        aihealth._record({"state": "green", "checked_at": "t1"})
        r = aihealth._record({"state": "red", "checked_at": "t2"})
        self.assertEqual(r["prev_state"], "green")
        self.assertEqual(r["changed_at"], "t2")
        h = aihealth.history()
        self.assertEqual(len(h), 1)
        self.assertEqual((h[0]["from"], h[0]["to"]), ("green", "red"))

    def test_no_transition_keeps_changed_at(self):
        aihealth._record({"state": "green", "checked_at": "t1"})
        r = aihealth._record({"state": "green", "checked_at": "t2"})
        self.assertEqual(r["changed_at"], "t1")   # unchanged since first green
        self.assertEqual(aihealth.history(), [])   # no transition logged

    def test_summary_defaults_to_unknown(self):
        self.assertEqual(aihealth.summary()["state"], "unknown")


class ProbeTests(Base):
    def _patch_cli(self, state, detail, extra=None):
        aihealth._probe_cli = lambda: (state, detail, extra or {})

    def setUp(self):
        super().setUp()
        self._real_cli = aihealth._probe_cli

    def tearDown(self):
        aihealth._probe_cli = self._real_cli
        super().tearDown()

    def test_probe_green_writes_state_and_no_action(self):
        self._patch_cli("green", "logged in (claude.ai, max)")
        r = aihealth.probe()
        self.assertEqual(r["state"], "green")
        self.assertEqual(r["action"], "")
        self.assertEqual(aihealth.last_state()["state"], "green")

    def test_probe_red_cli_gives_reauth_action(self):
        self._patch_cli("red", "not logged in")
        r = aihealth.probe()
        self.assertEqual(r["state"], "red")
        self.assertIn("claude auth login", r["action"])
        self.assertIsNone(r["fallback"])  # no key in env

    def test_probe_red_cli_with_key_advertises_fallback(self):
        self._cfg(api_key_env="AH_TEST_KEY")
        import os
        os.environ["AH_TEST_KEY"] = "sk-x"
        try:
            self._patch_cli("red", "not logged in")
            r = aihealth.probe()
            self.assertEqual(r["fallback"], "api")
            self.assertIn("falling back", r["action"])
        finally:
            del os.environ["AH_TEST_KEY"]


class AlertTests(Base):
    def setUp(self):
        super().setUp()
        import server.notify as notify
        self._notify = notify
        self._pings = []
        self._real = notify.agent_ping
        notify.agent_ping = lambda text, key=None: self._pings.append((key, text))

    def tearDown(self):
        self._notify.agent_ping = self._real
        super().tearDown()

    def test_alert_only_on_green_to_red_edge(self):
        aihealth.maybe_alert({"state": "red", "prev_state": "green",
                              "action": "reconnect"})
        aihealth.maybe_alert({"state": "red", "prev_state": "red"})  # no re-ping
        self.assertEqual(len(self._pings), 1)
        self.assertEqual(self._pings[0][0], "ai-health-red")

    def test_recovery_pings_once(self):
        aihealth.maybe_alert({"state": "green", "prev_state": "red"})
        self.assertEqual(self._pings[0][0], "ai-health-ok")

    def test_notify_disabled_is_silent(self):
        self._cfg(ai_health_notify=False)
        aihealth.maybe_alert({"state": "red", "prev_state": "green"})
        self.assertEqual(self._pings, [])

    def test_note_failure_flips_red_and_alerts(self):
        info = aihealth.note_failure(
            "OAuth session expired and could not be refreshed", source="reply-draft")
        self.assertEqual(info["kind"], "auth")
        self.assertEqual(aihealth.last_state()["state"], "red")
        self.assertEqual(len(self._pings), 1)

    def test_note_failure_ignores_transient(self):
        aihealth.note_failure("connection reset", source="reply-draft")
        self.assertEqual(aihealth.last_state(), {})  # nothing recorded
        self.assertEqual(self._pings, [])


if __name__ == "__main__":
    unittest.main()
