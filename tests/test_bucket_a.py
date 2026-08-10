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

from server import backup, data as crm, judge, msgraph, update, viratools
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


class ProfileBackupTests(CrmBase):
    """Profile writes snapshot first (2026-08-04). Until then loops, facts
    and hooks were rewritten in place with nothing to revert to, while
    people.json beside them was backed up on every touch — the asymmetry
    that made auto-dispatching a CRM-shaped journal instruction the riskier
    half of the feature."""

    def backups(self, pid="p_test00000001"):
        return sorted((self.root / "backups" / "profiles").glob(f"{pid}-*.json"))

    def test_write_snapshots_the_bytes_it_is_about_to_replace(self):
        path = self.root / "profiles" / "p_test00000001.json"
        before = path.read_text(encoding="utf-8")
        crm.save_profile_field("p_test00000001", "hooks", [{"topic": "rowing"}])
        b = self.backups()
        self.assertEqual(len(b), 1)
        # the snapshot is the PRE-write content, not the result
        self.assertEqual(b[0].read_text(encoding="utf-8"), before)
        self.assertNotEqual(path.read_text(encoding="utf-8"), before)

    def test_every_profile_write_path_is_covered(self):
        # one implementation (_load_profile_for_write), so add_loop/add_fact/
        # update_loop/save_profile_refresh all inherit it — and each write
        # leaves exactly ONE snapshot despite reading the file twice
        crm.add_loop("p_test00000001", "a new loop")
        crm.add_fact("p_test00000001", "a durable fact")
        crm.update_loop("p_test00000001", "Return the borrowed ladder", "close")
        crm.save_profile_refresh("p_test00000001", "a refreshed summary")
        self.assertEqual(len(self.backups()), 4)

    def test_no_two_snapshots_hold_identical_bytes(self):
        # the double-read guard: add_loop reads the profile and then
        # _save_field_locked reads it again inside the same lock, so a naive
        # snapshot would store one state twice and halve the window's reach
        for i in range(4):
            crm.add_loop("p_test00000001", f"loop {i}")
        b = [p.read_bytes() for p in self.backups()]
        self.assertEqual(len(b), 4)
        self.assertEqual(len(b), len(set(b)))

    def test_first_touch_of_an_absent_profile_snapshots_nothing(self):
        crm.save_profile_field("p_test00000002", "hooks", [{"topic": "chess"}])
        self.assertEqual(self.backups("p_test00000002"), [])

    def test_a_corrupt_profile_is_quarantined_not_snapshotted(self):
        self.corrupt()
        with self.assertRaises(crm.ProfileCorruptError):
            crm.save_profile_field("p_test00000001", "hooks", [])
        self.assertEqual(self.backups(), [])  # fail-closed writes nothing

    def test_names_sort_chronologically_within_one_second(self):
        # the routinesrc lesson: a bare collision suffix sorts '-1' BEFORE
        # '.', so lexical and chronological order would disagree
        with mock.patch("server.data.time.strftime", return_value="20260804-120000"):
            for i in range(3):
                crm.save_profile_field("p_test00000001", "hooks",
                                       [{"topic": f"h{i}"}])
        b = self.backups()
        self.assertEqual(len(b), 3)
        self.assertEqual([p.name for p in b], sorted(p.name for p in b))
        # oldest first: the original, then each write's input in order
        topics = [json.loads(p.read_text(encoding="utf-8"))["hooks"][0]["topic"]
                  for p in b]
        self.assertEqual(topics, ["sailing", "h0", "h1"])

    def test_retention_keeps_the_newest_and_never_fails_the_write(self):
        keep = crm.PROFILE_BACKUPS_KEEP
        for i in range(keep + 5):
            crm.save_profile_field("p_test00000001", "hooks",
                                   [{"topic": f"h{i}"}])
        self.assertEqual(len(self.backups()), keep)
        # an unwritable backup dir must never take the write down with it
        with mock.patch("server.data.shutil.copy2", side_effect=OSError("nope")):
            prof = crm.save_profile_field("p_test00000001", "hooks",
                                          [{"topic": "survives"}])
        self.assertEqual(prof["hooks"], [{"topic": "survives"}])


class BackupCoverageTests(unittest.TestCase):
    def test_canonical_stores_all_covered(self):
        for name in ("ideas.json", "config.json", "subscriptions.json",
                     "routines.json", "circuit-runs.json",
                     "brief-journal.json", "atlas-groups.json",
                     "jobs-log.json", "applications.json",
                     "mail-accounts.json", "circuits.json",
                     # 2026-08-10 data-audit additions
                     "brain-chat.json", "plans.json", "contact-cards.json",
                     "ui-state.json", "modules.json", "orphan-work.json",
                     "doc-index.json", "pii-patterns.txt"):
            self.assertIn(name, backup.FILES)

    def test_sole_copy_directories_all_covered(self):
        for rel in ("whatsapp/session", "blog/posts", "reading/rooms",
                    "genres", "idea-images", "walkthrough-anon"):
            self.assertIn(rel, backup.DIRS)


class BackupDirSnapshotTests(unittest.TestCase):
    """The directory half of the rotation: dated tree copies, same-day
    idempotency, crash-debris sweep, and the 14-deep retention window."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data, self.dest = root / "data", root / "dst"
        (self.data / "whatsapp" / "session").mkdir(parents=True)
        (self.data / "whatsapp" / "session" / "creds.json").write_text(
            "{}", encoding="utf-8")
        self.patches = [mock.patch.object(backup, "DATA", self.data),
                        mock.patch.object(backup, "DEST", self.dest)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def snaps(self):
        return sorted(p for p in self.dest.glob("whatsapp-session-*")
                      if p.is_dir())

    def test_dated_copy_once_per_day_and_missing_dirs_skipped(self):
        backup.snapshot()
        backup.snapshot()  # second same-day run must not duplicate or fail
        s = self.snaps()
        self.assertEqual(len(s), 1)
        self.assertEqual((s[0] / "creds.json").read_text(encoding="utf-8"),
                         "{}")
        # blog/posts etc. don't exist in this fixture — skipped, not created
        self.assertEqual(list(self.dest.glob("blog-posts-*")), [])

    def test_crash_debris_is_swept_not_snapshotted(self):
        debris = self.dest / "whatsapp-session-2026-01-01.tmp"
        debris.mkdir(parents=True)
        (debris / "partial.json").write_text("x", encoding="utf-8")
        backup.snapshot()
        self.assertFalse(debris.exists())
        self.assertEqual(len(self.snaps()), 1)

    def test_retention_keeps_the_newest_and_todays_snapshot(self):
        for i in range(1, 21):
            d = self.dest / f"whatsapp-session-2020-01-{i:02d}"
            d.mkdir(parents=True)
        backup.snapshot()
        s = self.snaps()
        self.assertEqual(len(s), backup.KEEP)
        self.assertNotIn("2020-01", s[-1].name)  # today's survives the prune


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
                         {"Task", "WebSearch"} | viratools.WRITE_TOOLS)

    def test_read_only_excludes_every_vira_write_tool(self):
        """The property, not the list: a write tool added to the native
        server must be denied to read-only sessions the day it ships. The
        frozen-set version of this test passed while two new writes were
        reachable from a judge."""
        for name in viratools.WRITE_TOOLS:
            self.assertIn(name, READ_ONLY_EXCLUDE)
            self.assertIn(name, viratools.TOOL_NAMES)

    def test_write_tools_are_actually_registered(self):
        """A rename that misses WRITE_TOOLS would silently un-exclude the
        tool — the set must name real specs, not historical ones."""
        registered = set(viratools.TOOL_NAMES)
        self.assertTrue(viratools.WRITE_TOOLS <= registered,
                        f"stale: {viratools.WRITE_TOOLS - registered}")


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
        # Pinned to the Mac path; the Windows twin (windows_task_name)
        # lives in test_update.SupervisorSeamTests.
        with mock.patch("server.update.settings.IS_WIN", False), \
             mock.patch("server.update.settings.raw", return_value={}):
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
