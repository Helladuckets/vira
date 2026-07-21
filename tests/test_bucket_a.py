"""Bucket-A self-protection fixes (external audit 2026-07-16, EVALUATION.md
triage A; executed 2026-07-20 as decision D5): profile writes fail closed
with quarantine, backup rotation covers every canonical store, spawned
agents inherit no API key, the updater refuses to orphan itself, thread and
search limits clamp, and the Graph refresh token stays off argv.

The read-only gate-ordering half of the bucket lives in test_runner.py
(GateTests), next to the rest of the gate suite.

Run: .venv/bin/python -m unittest discover tests
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import backup, data as crm, judge, msgraph, update
from server.session import READ_ONLY_EXCLUDE, _sdk_env


def _seed_crm(root):
    root = Path(root)
    (root / "profiles").mkdir(parents=True)
    people = {"people": [
        {"id": "p_test00000001", "name": "Casey Example",
         "handles": {"imessage": [], "emails": [], "phones10": []}},
        {"id": "p_test00000002", "name": "Drew Sample",
         "handles": {"imessage": [], "emails": [], "phones10": []}},
    ]}
    (root / "people.json").write_text(json.dumps(people))
    (root / "master.json").write_text("[]")
    prof = {"name": "Casey Example",
            "relationship_class": "friend",
            "hooks": [{"topic": "sailing"}],
            "open_loops": [{"what": "Return the borrowed ladder",
                            "owed_by": "me", "since": "2024-01-01",
                            "status": "open"}]}
    (root / "profiles" / "p_test00000001.json").write_text(json.dumps(prof))
    return root


class CrmBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _seed_crm(self.tmp.name)
        self.patcher = mock.patch("server.data.settings.crm_root",
                                  return_value=self.root)
        self.patcher.start()
        crm.invalidate()

    def tearDown(self):
        self.patcher.stop()
        crm.invalidate()
        self.tmp.cleanup()

    def corrupt(self, pid="p_test00000001"):
        path = self.root / "profiles" / f"{pid}.json"
        path.write_text('{"name": "Casey Ex')  # truncated mid-write
        return path


class ProfileFailClosedTests(CrmBase):
    def test_corrupt_profile_write_refused_and_quarantined(self):
        path = self.corrupt()
        before = path.read_text()
        with self.assertRaises(crm.ProfileCorruptError):
            crm.save_profile_field("p_test00000001", "hooks", [{"topic": "x"}])
        self.assertEqual(path.read_text(), before)  # original untouched
        q = list(path.parent.glob(path.name + ".corrupt-*"))
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0].read_text(), before)  # quarantine = the bytes

    def test_add_loop_and_fact_fail_closed_on_corrupt(self):
        self.corrupt()
        with self.assertRaises(crm.ProfileCorruptError):
            crm.add_loop("p_test00000001", "never lands")
        with self.assertRaises(crm.ProfileCorruptError):
            crm.add_fact("p_test00000001", "never lands")

    def test_update_loop_fails_closed_on_corrupt(self):
        self.corrupt()
        with self.assertRaises(crm.ProfileCorruptError):
            crm.update_loop("p_test00000001", "Return the borrowed ladder",
                            "close")

    def test_missing_profile_still_creates_minimal(self):
        # the documented first-touch path must keep working: no file at all
        # is not corruption
        prof = crm.save_profile_field("p_test00000002", "hooks",
                                      [{"topic": "chess"}])
        self.assertEqual(prof["name"], "Drew Sample")
        self.assertEqual(prof["hooks"], [{"topic": "chess"}])

    def test_intact_profile_write_merges_not_replaces(self):
        prof = crm.save_profile_field("p_test00000001", "hooks",
                                      [{"topic": "rowing"}])
        self.assertEqual(prof["relationship_class"], "friend")  # preserved


class BackupCoverageTests(unittest.TestCase):
    def test_canonical_stores_all_covered(self):
        for name in ("ideas.json", "config.json", "subscriptions.json",
                     "routines.json", "circuit-runs.json",
                     "brief-journal.json", "atlas-groups.json",
                     "jobs-log.json"):
            self.assertIn(name, backup.FILES)


class SdkEnvTests(unittest.TestCase):
    def test_vira_anthropic_key_is_blanked(self):
        env = {"VIRA_ANTHROPIC_KEY": "k", "ANTHROPIC_API_KEY": "a",
               "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
               "PATH": "/usr/bin"}
        with mock.patch.dict(os.environ, env, clear=True):
            out = _sdk_env()
        self.assertEqual(out.get("VIRA_ANTHROPIC_KEY"), "")
        self.assertEqual(out.get("ANTHROPIC_API_KEY"), "")
        self.assertNotIn("CLAUDECODE", out)          # SDK filters it itself
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", out)
        self.assertNotIn("PATH", out)                # untouched

    def test_read_only_exclude_names_the_non_reads(self):
        self.assertEqual(READ_ONLY_EXCLUDE,
                         {"Task", "WebSearch",
                          "mcp__vira__update_module_map"})


class JudgeSymlinkTests(unittest.TestCase):
    def test_untracked_symlink_is_never_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            repo = tmp / "repo"
            repo.mkdir()
            r = subprocess.run(["git", "init", "-q", str(repo)],
                               capture_output=True)
            if r.returncode != 0:
                self.skipTest("git unavailable")
            secret = tmp / "outside.txt"
            secret.write_text("SECRET-OUTSIDE-THE-REPO")
            (repo / "leak.txt").symlink_to(secret)
            (repo / "honest.txt").write_text("honest untracked content")
            out = judge._git_diff(str(repo))
            self.assertNotIn("SECRET-OUTSIDE-THE-REPO", out)
            self.assertIn("honest untracked content", out)


class UpdaterGuardTests(unittest.TestCase):
    def test_apply_refuses_without_supervisor(self):
        with mock.patch("server.update.settings.raw", return_value={}):
            with self.assertRaises(ValueError) as ctx:
                update.apply()
        self.assertIn("launchd_label", str(ctx.exception))


class LimitClampTests(unittest.TestCase):
    def test_thread_group_and_search_limits_clamp(self):
        from server import main
        with mock.patch("server.main.imessage.thread_for_person",
                        return_value=[]) as tfp:
            main.api_thread("p_x", limit=-1)
            self.assertEqual(tfp.call_args.args[1], 1)
            main.api_thread("p_x", limit=10 ** 9)
            self.assertEqual(tfp.call_args.args[1], 500)
        with mock.patch("server.main.imessage.group_thread",
                        return_value=[]) as gt:
            main.api_group_thread("1,2", limit=-5)
            self.assertEqual(gt.call_args.args[1], 1)
        with mock.patch("server.main.msearch.search",
                        return_value=[]) as ms:
            main.api_media_search(q="x", limit=-1)
            self.assertEqual(ms.call_args.kwargs["limit"], 1)
            main.api_media_search(q="x", limit=9999)
            self.assertEqual(ms.call_args.kwargs["limit"], 200)


class MsgraphArgvTests(unittest.TestCase):
    def test_refresh_token_routes_through_the_secrets_ladder(self):
        # The argv-safety contract itself lives in secrets._mac_set (and is
        # tested there); msgraph's job is to hand the token to the ladder
        # under its namespaced service.
        from server import secrets
        with mock.patch.object(secrets, "set") as st:
            msgraph._store_refresh_token("owner@example.com", "tok.SECRET123")
        service, account, value = st.call_args.args
        self.assertTrue(service.endswith("vira-mail-graph"))
        self.assertEqual(account, "owner@example.com")
        self.assertEqual(value, "tok.SECRET123")

    def test_mac_keychain_write_rides_stdin_not_argv(self):
        from server import secrets
        with mock.patch.object(secrets.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            secrets._mac_set("vira-mail-graph", "owner@example.com",
                             "tok.SECRET123")
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["security", "-i"])
        self.assertNotIn("tok.SECRET123", " ".join(argv))
        stdin = run.call_args.kwargs["input"]
        self.assertIn("tok.SECRET123", stdin)
        self.assertIn("add-generic-password", stdin)


if __name__ == "__main__":
    unittest.main()
