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
from unittest import mock

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

    def test_the_real_spend_limit_string_is_a_limit(self):
        # VERBATIM from the ledger — 21 of 450 jobs died on this exact
        # sentence and every one classified as `other`, i.e. "try again
        # shortly", which is false for a MONTHLY cap. A tidied paraphrase
        # would pass against a reader that cannot parse the real thing.
        info = aihealth.classify(
            "You've hit your monthly spend limit · raise it at "
            "claude.ai/settings/usage")
        self.assertEqual(info["kind"], "limit")
        self.assertFalse(info["needs_reauth"])
        self.assertIn("usage limit", info["message"])

    def test_a_limit_does_not_read_as_a_broken_login(self):
        # The probe agrees the credential is fine throughout a spend limit,
        # so calling it auth would point the owner at a login that works.
        for text in ("Claude usage limit reached|1755100000",
                     "rate_limit_error", "quota exceeded"):
            with self.subTest(text=text):
                info = aihealth.classify(text)
                self.assertEqual(info["kind"], "limit")
                self.assertFalse(info["needs_reauth"])

    def test_credit_still_outranks_a_limit_word(self):
        # "credit balance is too low" means the account needs money, which is
        # a different action from waiting out a cap.
        info = aihealth.classify(
            "Credit balance is too low; your usage limit is unaffected")
        self.assertEqual(info["kind"], "credit")

    def test_a_limit_never_flips_the_health_banner_red(self):
        # note_failure flips red for auth/credit only. A limit leaves a
        # WORKING login, and the 5-minute probe would flip it straight back
        # to green — the documented flapping this must not reintroduce.
        with mock.patch.object(aihealth, "maybe_alert") as alert, \
             mock.patch.object(aihealth, "_record") as record:
            info = aihealth.note_failure("You've hit your monthly spend limit")
        self.assertEqual(info["kind"], "limit")
        record.assert_not_called()
        alert.assert_not_called()
        # control: the kinds that DO flip it still do, so this is not passing
        # because note_failure went inert
        with mock.patch.object(aihealth, "maybe_alert"), \
             mock.patch.object(aihealth, "_record") as record:
            aihealth.note_failure("Failed to authenticate: not logged in")
        record.assert_called_once()


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
    """Every case here is about an install that HAS an AI connected — the
    never-configured fork is FreshInstallIsNotRed's subject, below.

    That precondition has to be pinned, not assumed: `_never_configured`
    asks the world (`models.connected`), so these read the developer's own
    machine unless told otherwise. On a machine with a provider connected
    they passed; on every CI runner probe() short-circuited to "setup" and
    four cases failed. Which machine runs the suite must not decide what
    the suite is testing.
    """

    def _patch_cli(self, state, detail, extra=None):
        aihealth._probe_cli = lambda: (state, detail, extra or {})

    def setUp(self):
        super().setUp()
        self._real_cli = aihealth._probe_cli
        self._conf = mock.patch.object(aihealth, "_never_configured",
                                       return_value=False)
        self._conf.start()

    def tearDown(self):
        self._conf.stop()
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


class FreshInstallIsNotRed(unittest.TestCase):
    """A brand-new install has no AI connected, and that is not a fault.

    Reported 2026-07-30 from a real first boot: the header wore a clay
    "AI is paused — the Claude login is not active. Open a terminal and
    run ... to reconnect" banner before the owner had done anything. Two
    things wrong at once — it called the expected state broken, and it
    handed over a terminal command, which is the exact hand-off the driven
    sign-in was built to remove.
    """

    def test_never_configured_reports_setup_not_red(self):
        with mock.patch.object(aihealth, "_never_configured",
                               return_value=True):
            r = aihealth.probe(write=False)
        self.assertEqual(r["state"], "setup")
        # The banner renders on "red" alone, so anything else hides it — but
        # an empty action is what keeps a terminal command off a first boot
        # even if some future surface starts printing it.
        self.assertEqual(r["action"], "")

    def test_a_configured_install_still_probes(self):
        with mock.patch.object(aihealth, "_never_configured",
                               return_value=False), \
             mock.patch.object(aihealth, "_probe_cli",
                               return_value=("red", "login expired", {})):
            r = aihealth.probe(write=False)
        self.assertEqual(r["state"], "red")

    def test_a_broken_probe_reads_as_configured(self):
        # Never silence a real outage because the capability probe failed.
        #
        # Patched on the models MODULE, not on an `aihealth.models`
        # attribute: _never_configured does its own `from . import models`
        # inside the function, so there is no module-level name to replace
        # and `create=True` invented one nothing ever read. The mock had no
        # effect at all — the assertion was passing on the real
        # models.connected() of whatever machine ran it.
        from server import models
        with mock.patch.object(models, "connected",
                               side_effect=RuntimeError("boom")):
            self.assertFalse(aihealth._never_configured())

    def test_no_provider_connected_reads_as_never_configured(self):
        from server import models
        with mock.patch.object(models, "connected", return_value=[]):
            self.assertTrue(aihealth._never_configured())


class ApiProbeTests(Base):
    """The api-backend probe must speak to the CONFIGURED provider.

    The 2026-07-31 incident: Grok was the go-to (backend "api", a valid
    xAI key in the Keychain), and _probe_api was hardcoded to
    api.anthropic.com — so the valid key read as rejected (401) and the
    banner told the owner to replace a key x.ai was accepting. Every case
    here pins the endpoint/key pairing; urlopen is stubbed, so none of it
    touches the network or this machine's stored keys.
    """

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def setUp(self):
        super().setUp()
        self._conf = mock.patch.object(aihealth, "_never_configured",
                                       return_value=False)
        self._conf.start()

    def tearDown(self):
        self._conf.stop()
        super().tearDown()

    def _capture_urlopen(self):
        seen = {}

        def fake(req, timeout=0):
            seen["url"] = req.full_url
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            return self._Resp()
        return seen, fake

    def test_probe_api_hits_the_configured_providers_endpoint(self):
        from server import models
        self._cfg(ai_provider="xai", ai_backend="api")
        seen, fake = self._capture_urlopen()
        with mock.patch.object(models, "api_key", return_value="xk-test"), \
             mock.patch("urllib.request.urlopen", side_effect=fake):
            r = aihealth.probe(write=False)
        self.assertIn("api.x.ai", seen["url"])
        self.assertEqual(seen["headers"].get("authorization"),
                         "Bearer xk-test")
        self.assertEqual(r["state"], "green")
        self.assertEqual(r["backend"], "api")
        self.assertEqual(r["provider"], "xai")
        self.assertEqual(r["authMethod"], "key")
        self.assertEqual(r["action"], "")

    def test_anthropic_still_probes_anthropic(self):
        self._cfg(ai_provider="anthropic", ai_backend="api")
        seen, fake = self._capture_urlopen()
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            state, detail, _ = aihealth._probe_api("sk-test", "anthropic")
        self.assertEqual(state, "green")
        self.assertIn("api.anthropic.com", seen["url"])
        self.assertEqual(seen["headers"].get("x-api-key"), "sk-test")

    def test_unknown_provider_falls_back_to_anthropic(self):
        seen, fake = self._capture_urlopen()
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            state, _, _ = aihealth._probe_api("sk-test", "nope")
        self.assertEqual(state, "green")
        self.assertIn("api.anthropic.com", seen["url"])

    def _http_error(self, code):
        import urllib.error
        return urllib.error.HTTPError("u", code, "err", None, None)

    def test_rejected_key_names_the_provider_and_reads_red(self):
        from server import models
        self._cfg(ai_provider="xai", ai_backend="api")
        with mock.patch.object(models, "api_key", return_value="xk-bad"), \
             mock.patch("urllib.request.urlopen",
                        side_effect=self._http_error(401)):
            r = aihealth.probe(write=False)
        self.assertEqual(r["state"], "red")
        self.assertIn("xAI", r["detail"])
        self.assertIn("xAI", r["action"])

    def test_google_bad_key_400_reads_red(self):
        # Gemini answers a bad key with 400 API_KEY_INVALID, not 401. The
        # probe request itself is fixed and known-good, so a 400 there can
        # only be the key.
        with mock.patch("urllib.request.urlopen",
                        side_effect=self._http_error(400)):
            state, detail, _ = aihealth._probe_api("gk-bad", "google")
        self.assertEqual(state, "red")
        self.assertIn("Google", detail)

    def test_other_http_errors_stay_unknown(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=self._http_error(500)):
            state, _, _ = aihealth._probe_api("xk", "xai")
        self.assertEqual(state, "unknown")

    def test_legacy_env_key_never_serves_a_non_anthropic_go_to(self):
        # VIRA_ANTHROPIC_KEY is an Anthropic credential; handing it to a
        # Grok go-to would probe the wrong provider's key.
        import os
        from server import models
        self._cfg(ai_provider="xai", ai_backend="api",
                  api_key_env="AH_TEST_LEGACY")
        os.environ["AH_TEST_LEGACY"] = "sk-anthropic"
        try:
            with mock.patch.object(models, "api_key", return_value=""):
                self.assertEqual(aihealth._api_key(), "")
        finally:
            del os.environ["AH_TEST_LEGACY"]

    def test_anthropic_go_to_keeps_the_legacy_env_fallback(self):
        import os
        from server import models
        self._cfg(ai_provider="anthropic", ai_backend="api",
                  api_key_env="AH_TEST_LEGACY")
        os.environ["AH_TEST_LEGACY"] = "sk-anthropic"
        try:
            with mock.patch.object(models, "api_key", return_value=""):
                self.assertEqual(aihealth._api_key(), "sk-anthropic")
        finally:
            del os.environ["AH_TEST_LEGACY"]
