"""The secrets ladder: OS store first, locked file as the portable floor.

What must hold on every platform: a secret set is a secret got; reads
never raise; the file fallback is owner-only where the filesystem can
say so; and the macOS backend keeps the argv-safety contract (the value
rides `security -i` stdin, never argv — audit P1-1).
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import secrets


class FileFallbackTests(unittest.TestCase):
    """Force the ladder past the OS stores: this is every Linux box and
    any machine whose store call breaks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "data").mkdir()
        self.patches = [
            mock.patch.object(secrets.settings, "ROOT", root),
            mock.patch.object(secrets, "_mac_available", return_value=False),
            mock.patch.object(secrets, "IS_WIN", False),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_roundtrip(self):
        where = secrets.set("vira-mercury", None, "token-1")
        self.assertEqual(where, "file")
        self.assertEqual(secrets.get("vira-mercury"), "token-1")

    def test_account_scoping(self):
        secrets.set("vira-mail", "a@example.com", "pw-a")
        secrets.set("vira-mail", "b@example.com", "pw-b")
        self.assertEqual(secrets.get("vira-mail", "a@example.com"), "pw-a")
        self.assertEqual(secrets.get("vira-mail", "b@example.com"), "pw-b")
        self.assertEqual(secrets.get("vira-mail", "c@example.com"), "")

    def test_upsert_replaces(self):
        secrets.set("svc", "acct", "old")
        secrets.set("svc", "acct", "new")
        self.assertEqual(secrets.get("svc", "acct"), "new")

    def test_delete_removes(self):
        secrets.set("svc", "acct", "v")
        secrets.delete("svc", "acct")
        self.assertEqual(secrets.get("svc", "acct"), "")

    def test_missing_is_empty_never_raises(self):
        self.assertEqual(secrets.get("nowhere", "nobody"), "")

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_file_is_owner_only(self):
        secrets.set("svc", None, "v")
        mode = stat.S_IMODE(os.stat(secrets._file_path()).st_mode)
        self.assertEqual(mode, 0o600)

    def test_corrupt_file_reads_as_empty(self):
        secrets._file_path().write_text("{not json")
        self.assertEqual(secrets.get("svc"), "")
        # and a write repairs it rather than raising
        secrets.set("svc", None, "v2")
        self.assertEqual(secrets.get("svc"), "v2")


class MacBackendTests(unittest.TestCase):
    """The Keychain path, with `security` mocked — behavior must not
    depend on the test host actually being a Mac."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "data").mkdir()
        for p in (mock.patch.object(secrets.settings, "ROOT", root),
                  mock.patch.object(secrets, "_mac_available",
                                    return_value=True),
                  mock.patch.object(secrets, "IS_WIN", False)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_keychain_hit_wins(self):
        with mock.patch.object(secrets.subprocess, "run",
                               return_value=mock.Mock(returncode=0,
                                                      stdout="kc-val\n")):
            self.assertEqual(secrets.get("svc", "acct"), "kc-val")

    def test_keychain_miss_falls_to_file(self):
        secrets._file_set("svc", "acct", "file-val")
        with mock.patch.object(secrets.subprocess, "run",
                               return_value=mock.Mock(returncode=44,
                                                      stdout="")):
            self.assertEqual(secrets.get("svc", "acct"), "file-val")

    def test_broken_security_never_raises(self):
        with mock.patch.object(secrets.subprocess, "run",
                               side_effect=OSError("boom")):
            self.assertEqual(secrets.get("svc", "acct"), "")

    def test_set_prefers_keychain_and_skips_file(self):
        with mock.patch.object(secrets.subprocess, "run",
                               return_value=mock.Mock(returncode=0)):
            where = secrets.set("svc", "acct", "v")
        self.assertEqual(where, "keychain")
        self.assertFalse(secrets._file_path().exists())

    def test_set_falls_to_file_when_keychain_refuses(self):
        with mock.patch.object(secrets.subprocess, "run",
                               return_value=mock.Mock(returncode=1,
                                                      stderr="denied")):
            where = secrets.set("svc", "acct", "v")
        self.assertEqual(where, "file")
        self.assertEqual(secrets._file_get("svc", "acct"), "v")

    def test_write_is_argv_safe(self):
        with mock.patch.object(secrets.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            secrets._mac_set("svc", "acct", 'pa"ss\\word')
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["security", "-i"])
        self.assertNotIn("pass", " ".join(argv))
        stdin = run.call_args.kwargs["input"]
        # quoting per security(1): backslash then double-quote escapes
        self.assertIn('pa\\"ss\\\\word', stdin)

    def test_newlines_stripped_from_written_value(self):
        with mock.patch.object(secrets.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            secrets._mac_set("svc", "acct", "tok\nen\r")
        self.assertIn("token", run.call_args.kwargs["input"])


class LadderDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "data").mkdir()
        p = mock.patch.object(secrets.settings, "ROOT", root)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_windows_branch_dispatches_to_credential_manager(self):
        with mock.patch.object(secrets, "_mac_available", return_value=False), \
             mock.patch.object(secrets, "IS_WIN", True), \
             mock.patch.object(secrets, "_win_get",
                               return_value="cm-val") as wg:
            self.assertEqual(secrets.get("svc", "acct"), "cm-val")
        wg.assert_called_once_with("svc", "acct")
        with mock.patch.object(secrets, "IS_WIN", True), \
             mock.patch.object(secrets, "_mac_available", return_value=False), \
             mock.patch.object(secrets, "_win_set") as ws:
            self.assertEqual(secrets.set("svc", "acct", "v"),
                             "credential-manager")
        ws.assert_called_once_with("svc", "acct", "v")

    def test_windows_store_failure_falls_to_file(self):
        with mock.patch.object(secrets, "_mac_available", return_value=False), \
             mock.patch.object(secrets, "IS_WIN", True), \
             mock.patch.object(secrets, "_win_set",
                               side_effect=RuntimeError("no")):
            self.assertEqual(secrets.set("svc", "acct", "v"), "file")
        with mock.patch.object(secrets, "_mac_available", return_value=False), \
             mock.patch.object(secrets, "IS_WIN", True), \
             mock.patch.object(secrets, "_win_get",
                               side_effect=OSError("no")):
            self.assertEqual(secrets.get("svc", "acct"), "v")

    def test_win_target_folds_service_and_account(self):
        self.assertEqual(secrets._win_target("svc", "acct"), "svc/acct")
        self.assertEqual(secrets._win_target("svc", None), "svc")


if __name__ == "__main__":
    unittest.main()
