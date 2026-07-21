"""Instance isolation: the Keychain namespace.

The login Keychain is machine-wide — the one store a second Vira on the
same Mac cannot isolate by pointing HOME or crm_root elsewhere. Without a
prefix a sandbox install reads the live instance's Mercury token and
overwrites its Graph refresh token in place (security add -U), so these
tests pin both the namespacing and the empty default that keeps an
existing install's secrets exactly where they were.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import mail, mercury, msgraph, settings


class KeychainNamespaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "config.json"
        self.p = mock.patch.object(settings, "CONFIG_PATH", self.cfg)
        self.p.start()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("VIRA_KEYCHAIN_PREFIX", None)

    def tearDown(self):
        self.env.stop()
        self.p.stop()
        self.tmp.cleanup()

    def write_config(self, **kw):
        self.cfg.write_text(json.dumps(kw))

    def test_default_is_the_historical_name(self):
        # An existing install must keep reading the services it already wrote.
        self.assertEqual(settings.keychain_service("vira-mercury"), "vira-mercury")
        self.assertEqual(mail.keychain_service(), "vira-mail")
        self.assertEqual(mercury.keychain_service(), "vira-mercury")
        self.assertEqual(settings.keychain_service(msgraph.KEYCHAIN_SERVICE),
                         "vira-mail-graph")

    def test_env_prefix_namespaces_every_service(self):
        os.environ["VIRA_KEYCHAIN_PREFIX"] = "sandbox-"
        self.assertEqual(mail.keychain_service(), "sandbox-vira-mail")
        self.assertEqual(mercury.keychain_service(), "sandbox-vira-mercury")
        self.assertEqual(settings.keychain_service(msgraph.KEYCHAIN_SERVICE),
                         "sandbox-vira-mail-graph")

    def test_config_key_prefixes_when_env_is_absent(self):
        self.write_config(keychain_prefix="second-")
        self.assertEqual(mercury.keychain_service(), "second-vira-mercury")

    def test_env_wins_over_config(self):
        self.write_config(keychain_prefix="config-")
        os.environ["VIRA_KEYCHAIN_PREFIX"] = "env-"
        self.assertEqual(mercury.keychain_service(), "env-vira-mercury")

    def test_empty_values_fall_back_to_the_bare_name(self):
        self.write_config(keychain_prefix="")
        os.environ["VIRA_KEYCHAIN_PREFIX"] = ""
        self.assertEqual(mail.keychain_service(), "vira-mail")

    def test_lookup_uses_the_namespaced_service(self):
        os.environ["VIRA_KEYCHAIN_PREFIX"] = "sandbox-"
        with mock.patch.object(mercury.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="")
            mercury.keychain_token()
        self.assertIn("sandbox-vira-mercury", run.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
