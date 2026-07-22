"""The capability probe: does Vira find what's actually on the machine?

The bug this guards against is the one that shipped the spec: a PATH check
reported "OpenAI not installed" on a Mac where the owner was signed in with
a ChatGPT subscription — because the codex binary lives inside ChatGPT.app
and is linked nowhere `which` looks. Discovery has to search real install
locations, and it has to tell "present" apart from "signed in".
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import models


def _fake_bin(dirpath, name):
    p = Path(dirpath) / name
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(0o755)
    return str(p)


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        models._bin_cache.clear()

    def tearDown(self):
        models._bin_cache.clear()
        self.tmp.cleanup()

    def test_found_on_path(self):
        binp = _fake_bin(self.tmp.name, "claude")
        with mock.patch.object(models.shutil, "which",
                               side_effect=lambda n: binp if n == "claude" else None):
            self.assertEqual(models.find_binary("anthropic"), binp)

    def test_found_in_an_app_bundle_when_not_on_path(self):
        # The regression: nothing on PATH, binary present at a known location.
        bundled = _fake_bin(self.tmp.name, "codex")
        with mock.patch.object(models.shutil, "which", return_value=None), \
             mock.patch.dict(models.PROVIDERS["openai"], {"paths": [bundled]}):
            self.assertEqual(models.find_binary("openai"), bundled)

    def test_absent_when_nowhere(self):
        with mock.patch.object(models.shutil, "which", return_value=None), \
             mock.patch.dict(models.PROVIDERS["openai"],
                             {"paths": ["/nope/codex"]}):
            self.assertEqual(models.find_binary("openai"), "")

    def test_unknown_provider_is_empty_not_a_crash(self):
        self.assertEqual(models.find_binary("nosuch"), "")
        self.assertIsNone(models.probe("nosuch"))


class AuthProbeTest(unittest.TestCase):
    def setUp(self):
        models._bin_cache.clear()
        self.addCleanup(models._bin_cache.clear)

    def _probe(self, pid, stdout="", stderr="", code=0, key=""):
        with mock.patch.object(models, "find_binary", return_value="/x/bin"), \
             mock.patch.object(models, "api_key", return_value=key), \
             mock.patch.object(models.subprocess, "run",
                               return_value=mock.Mock(stdout=stdout,
                                                      stderr=stderr,
                                                      returncode=code)):
            return models.probe(pid)

    def test_json_logged_in(self):
        r = self._probe("anthropic",
                        stdout=json.dumps({"loggedIn": True,
                                           "email": "owner@example.com"}))
        self.assertEqual(r["auth"], models.SIGNED_IN)
        self.assertTrue(r["connected"])
        self.assertIn("owner@example.com", r["detail"])

    def test_json_logged_out(self):
        r = self._probe("anthropic", stdout=json.dumps({"loggedIn": False}))
        self.assertEqual(r["auth"], models.LOGGED_OUT)
        self.assertFalse(r["connected"])
        # The action carries the RESOLVED binary (/x/bin is not on PATH),
        # never the bare name — the codex-in-ChatGPT.app lesson.
        self.assertIn("`/x/bin auth login`", r["action"])
        self.assertEqual(r["login_cmd"], "/x/bin auth login")

    def test_plain_text_logged_in(self):
        # codex login status answers in prose, not JSON.
        r = self._probe("openai", stdout="Logged in using ChatGPT")
        self.assertEqual(r["auth"], models.SIGNED_IN)
        self.assertTrue(r["connected"])

    def test_plain_text_logged_out(self):
        r = self._probe("openai", stdout="Not logged in", code=1)
        self.assertEqual(r["auth"], models.LOGGED_OUT)

    def test_logged_out_but_key_on_file_is_still_usable(self):
        r = self._probe("openai", stdout="Not logged in", code=1, key="sk-x")
        self.assertEqual(r["auth"], models.KEY)
        self.assertTrue(r["connected"])

    def test_absent_binary_with_a_key_still_connects(self):
        with mock.patch.object(models, "find_binary", return_value=""), \
             mock.patch.object(models, "api_key", return_value="sk-x"):
            r = models.probe("openai")
        self.assertEqual(r["auth"], models.KEY)
        self.assertTrue(r["connected"])

    def test_absent_and_keyless_is_absent(self):
        with mock.patch.object(models, "find_binary", return_value=""), \
             mock.patch.object(models, "api_key", return_value=""):
            r = models.probe("openai")
        self.assertEqual(r["auth"], models.ABSENT)
        self.assertFalse(r["connected"])
        self.assertIn("install", r["action"])

    def test_probe_never_raises(self):
        with mock.patch.object(models, "find_binary", return_value="/x/bin"), \
             mock.patch.object(models, "api_key", return_value=""), \
             mock.patch.object(models.subprocess, "run",
                               side_effect=OSError("boom")):
            r = models.probe("anthropic")
        self.assertEqual(r["auth"], models.LOGGED_OUT)

    def test_timeout_is_not_signed_in(self):
        with mock.patch.object(models, "find_binary", return_value="/x/bin"), \
             mock.patch.object(models, "api_key", return_value=""), \
             mock.patch.object(models.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("c", 20)):
            r = models.probe("anthropic")
        self.assertEqual(r["auth"], models.LOGGED_OUT)


class LoginCommandTest(unittest.TestCase):
    """The two sandbox-caught login-card bugs: a bare command that fails
    off PATH, and a login instruction that signs in the wrong HOME."""

    def setUp(self):
        models._bin_cache.clear()
        self.addCleanup(models._bin_cache.clear)

    def test_path_resolved_binary_prints_the_bare_name(self):
        with mock.patch.object(models.shutil, "which",
                               return_value="/opt/homebrew/bin/codex"):
            cmd = models.login_command("openai", "/opt/homebrew/bin/codex")
        self.assertEqual(cmd, "codex login")

    def test_bundled_binary_prints_its_absolute_path(self):
        # The regression: codex found inside ChatGPT.app, not on PATH.
        # A card printing bare `codex login` hands over a command that
        # fails with "command not found".
        bundled = "/Applications/ChatGPT.app/Contents/Resources/codex"
        with mock.patch.object(models.shutil, "which", return_value=None):
            cmd = models.login_command("openai", bundled)
        self.assertEqual(cmd, f"{bundled} login")

    def test_absent_binary_means_no_command(self):
        with mock.patch.object(models, "find_binary", return_value=""):
            self.assertEqual(models.login_command("openai"), "")
        self.assertEqual(models.login_command("nosuch"), "")

    def test_sandbox_routes_anthropic_through_sandbox_sh(self):
        # `claude auth login` typed in a normal terminal signs in the REAL
        # home; the sandbox's documented flow is sandbox.sh login.
        with mock.patch.dict(os.environ, {"VIRA_SANDBOX": "1"}):
            cmd = models.login_command("anthropic", "/opt/homebrew/bin/claude")
        # Separator-agnostic: the script path is host-native (and may be
        # quoted), but it must be absolute and end in sandbox.sh login.
        self.assertTrue(cmd.endswith(" login"), cmd)
        script = cmd[:-len(" login")].strip("'\"")
        self.assertEqual(Path(script).name, "sandbox.sh")
        self.assertTrue(Path(script).is_absolute(), cmd)

    def test_sandbox_prefixes_home_for_other_providers(self):
        bundled = "/Applications/ChatGPT.app/Contents/Resources/codex"
        with mock.patch.dict(os.environ, {"VIRA_SANDBOX": "1"}), \
             mock.patch.object(models.shutil, "which", return_value=None):
            cmd = models.login_command("openai", bundled)
        self.assertTrue(cmd.startswith("HOME="), cmd)
        self.assertIn(f"{bundled} login", cmd)

    def test_no_sandbox_no_home_prefix(self):
        env = {k: v for k, v in os.environ.items() if k != "VIRA_SANDBOX"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(models.shutil, "which",
                               return_value="/opt/homebrew/bin/claude"):
            cmd = models.login_command("anthropic", "/opt/homebrew/bin/claude")
        self.assertEqual(cmd, "claude auth login")


class CapabilityTest(unittest.TestCase):
    def test_only_anthropic_drives_agent_sessions(self):
        # Circuits, Judge, Agent Loops and the cockpit are Claude Agent SDK.
        # Setup must say so rather than let a run fail later.
        self.assertTrue(models.PROVIDERS["anthropic"]["can"]["sessions"])
        self.assertFalse(models.PROVIDERS["openai"]["can"]["sessions"])


class CatalogTest(unittest.TestCase):
    """What a model dropdown is allowed to offer. The bug this guards is a
    hardcoded menu: it goes stale the week a model ships, and it offers
    models from a provider the owner never connected."""

    def setUp(self):
        models._models_cache.clear()
        models._options_cache.update(at=0.0, payload=None)
        self.addCleanup(models._models_cache.clear)
        self.addCleanup(models._options_cache.update, at=0.0, payload=None)

    def test_no_key_falls_back_to_the_curated_list(self):
        with mock.patch.object(models, "api_key", return_value=""):
            cat = models.catalog("anthropic")
        self.assertFalse(cat["api_live"])
        self.assertIn("no API key", cat["api_detail"])
        self.assertIn("claude-sonnet-5", [m["id"] for m in cat["api"]])
        # CLI aliases are what the binary accepts — never the API ids.
        self.assertEqual([m["id"] for m in cat["cli"]],
                         ["sonnet", "opus", "haiku", "fable"])

    def test_live_list_wins_and_carries_display_names(self):
        rows = [{"id": "claude-fable-5", "display_name": "Claude Fable 5"},
                {"id": "claude-opus-9", "display_name": "Claude Opus 9"}]
        with mock.patch.object(models, "api_key", return_value="sk-x"), \
             mock.patch.object(models, "_fetch_models",
                               return_value=(models._shape_models("anthropic", rows),
                                             "live from your API key — 2 models")):
            cat = models.catalog("anthropic")
        self.assertTrue(cat["api_live"])
        # A model that shipped after this code was written is offered.
        self.assertEqual([m["id"] for m in cat["api"]],
                         ["claude-fable-5", "claude-opus-9"])
        self.assertEqual(cat["api"][1]["label"], "Claude Opus 9")

    def test_a_failed_live_call_is_a_fallback_not_a_crash(self):
        with mock.patch.object(models, "api_key", return_value="sk-x"), \
             mock.patch.object(models.urllib.request, "urlopen",
                               side_effect=OSError("network down")):
            cat = models.catalog("anthropic")
        self.assertFalse(cat["api_live"])
        self.assertIn("live list unavailable", cat["api_detail"])
        self.assertTrue(cat["api"])          # the picker still has options

    def test_the_live_answer_is_cached_not_refetched_per_dropdown(self):
        calls = {"n": 0}

        def fetch(pid, key):
            calls["n"] += 1
            return [{"id": "claude-sonnet-5", "label": "Claude Sonnet 5"}], "ok"
        with mock.patch.object(models, "api_key", return_value="sk-x"), \
             mock.patch.object(models, "_fetch_models", side_effect=fetch):
            models.catalog("anthropic")
            models.catalog("anthropic")
            self.assertEqual(calls["n"], 1)
            models.catalog("anthropic", refresh=True)
        self.assertEqual(calls["n"], 2)

    def test_openai_catalog_drops_what_a_text_pipeline_cannot_drive(self):
        rows = [{"id": "gpt-5.1", "created": 20},
                {"id": "text-embedding-3-large", "created": 30},
                {"id": "gpt-4o-audio-preview", "created": 40},
                {"id": "dall-e-3", "created": 50},
                {"id": "gpt-4o", "created": 10}]
        got = models._shape_models("openai", rows)
        self.assertEqual([m["id"] for m in got], ["gpt-5.1", "gpt-4o"])

    def test_options_names_the_config_key_each_dropdown_writes(self):
        with mock.patch.object(models, "probe",
                               return_value={"connected": True,
                                             "auth": models.SIGNED_IN,
                                             "has_key": False}), \
             mock.patch.object(models, "api_key", return_value=""):
            opts = models.options(refresh=True)
        by_id = {p["id"]: p for p in opts["providers"]}
        self.assertEqual(by_id["anthropic"]["config_keys"],
                         {"cli": "cli_model", "api": "api_model"})
        self.assertEqual(by_id["openai"]["config_keys"],
                         {"cli": "openai_cli_model", "api": "openai_api_model"})
        # Only the session-capable provider may feed a circuit stage.
        self.assertTrue(by_id["anthropic"]["sessions"])
        self.assertFalse(by_id["openai"]["sessions"])

    def test_active_is_the_configured_provider_when_it_is_usable(self):
        def probe(pid):
            return {"connected": pid == "openai", "auth": models.SIGNED_IN,
                    "has_key": False}
        with mock.patch.object(models, "probe", side_effect=probe), \
             mock.patch.object(models, "api_key", return_value=""), \
             mock.patch.object(models.settings, "raw",
                               return_value={"ai_provider": "anthropic"}):
            opts = models.options(refresh=True)
        # Configured anthropic isn't connected here, so the ladder falls
        # through to the one that is — same answer a real call would give.
        self.assertEqual(opts["active"], "openai")
        self.assertTrue(models.PROVIDERS["openai"]["can"]["draft"])

    def test_env_key_wins_over_keychain_lookup(self):
        with mock.patch.dict(os.environ, {"VIRA_ANTHROPIC_KEY": "env-key"}), \
             mock.patch.object(models.secrets, "get") as get:
            self.assertEqual(models.api_key("anthropic"), "env-key")
        get.assert_not_called()

    def test_keychain_lookup_is_namespaced(self):
        with mock.patch.dict(os.environ, {"VIRA_ANTHROPIC_KEY": ""}), \
             mock.patch.dict(os.environ, {"VIRA_KEYCHAIN_PREFIX": "sandbox-"}), \
             mock.patch.object(models.secrets, "get",
                               return_value="k") as get:
            self.assertEqual(models.api_key("anthropic"), "k")
        service, account = get.call_args.args
        self.assertEqual(service, "sandbox-vira-model-key")
        self.assertEqual(account, "anthropic")


class ActiveProviderTest(unittest.TestCase):
    def _rec(self, pid, connected):
        return {"id": pid, "connected": connected, "auth":
                models.SIGNED_IN if connected else models.ABSENT,
                "can": {"draft": True, "sessions": pid == "anthropic"}}

    def test_configured_provider_wins_when_usable(self):
        with mock.patch.object(models.settings, "raw",
                               return_value={"ai_provider": "openai"}), \
             mock.patch.object(models, "probe",
                               side_effect=lambda p: self._rec(p, True)):
            self.assertEqual(models.active()["id"], "openai")

    def test_falls_back_to_whatever_is_connected(self):
        def probe(pid):
            return self._rec(pid, pid == "anthropic")
        with mock.patch.object(models.settings, "raw",
                               return_value={"ai_provider": "openai"}), \
             mock.patch.object(models, "probe", side_effect=probe):
            self.assertEqual(models.active()["id"], "anthropic")

    def test_none_connected_is_none(self):
        with mock.patch.object(models.settings, "raw", return_value={}), \
             mock.patch.object(models, "probe",
                               side_effect=lambda p: self._rec(p, False)):
            self.assertIsNone(models.active())
            self.assertEqual(models.auth_mode(), "")

    def test_auth_mode_distinguishes_subscription_from_key(self):
        rec = self._rec("anthropic", True)
        with mock.patch.object(models, "probe", return_value=rec):
            self.assertEqual(models.auth_mode("anthropic"), "subscription")
        rec2 = dict(rec, auth=models.KEY)
        with mock.patch.object(models, "probe", return_value=rec2):
            self.assertEqual(models.auth_mode("anthropic"), "key")


if __name__ == "__main__":
    unittest.main()
