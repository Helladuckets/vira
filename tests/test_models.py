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
        self.assertIn("`claude auth login`", r["action"])

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


class CapabilityTest(unittest.TestCase):
    def test_only_anthropic_drives_agent_sessions(self):
        # Circuits, Judge, Agent Loops and the cockpit are Claude Agent SDK.
        # Setup must say so rather than let a run fail later.
        self.assertTrue(models.PROVIDERS["anthropic"]["can"]["sessions"])
        self.assertFalse(models.PROVIDERS["openai"]["can"]["sessions"])
        self.assertTrue(models.PROVIDERS["openai"]["can"]["draft"])

    def test_env_key_wins_over_keychain_lookup(self):
        with mock.patch.dict(os.environ, {"VIRA_ANTHROPIC_KEY": "env-key"}), \
             mock.patch.object(models.subprocess, "run") as run:
            self.assertEqual(models.api_key("anthropic"), "env-key")
        run.assert_not_called()

    def test_keychain_lookup_is_namespaced(self):
        with mock.patch.dict(os.environ, {"VIRA_ANTHROPIC_KEY": ""}), \
             mock.patch.dict(os.environ, {"VIRA_KEYCHAIN_PREFIX": "sandbox-"}), \
             mock.patch.object(models.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="k\n")
            self.assertEqual(models.api_key("anthropic"), "k")
        self.assertIn("sandbox-vira-model-key", run.call_args[0][0])


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
