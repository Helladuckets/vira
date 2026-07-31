"""Orphan-work sweeper tests: classification (dirty/unmerged/excluded),
stalest-first ordering, dismiss + self-re-arm, the baseline-then-ping
notify rule, the job-ledger join for the stalled-session signal, the
unpushed-main row, resume_prompt content, the route layer (incl. passive
403s), and the merge/discard action runner against a real (stand-in)
scripts/branch.sh.

Every test builds its own throwaway git repo — no mocked git calls for the
classification logic, because every refusal/inclusion here IS a git
question, and a mocked git would only prove the mock (the same reasoning
tests/test_worktree.py's EnsureAgainstARealRepo/TidyAgainstARealRepo use).

Run: .venv/bin/python -m unittest discover tests
"""
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import orphanwork


def _git(*args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=check)


def _commit_at(cwd, msg, when):
    """Commit everything staged with an explicit author/committer date, so
    ordering tests don't depend on real wall-clock deltas between two git
    commands (the Windows-clock-resolution lesson generalizes: craft the
    timestamp, never rely on a real sleep)."""
    _git("add", "-A", cwd=cwd)
    env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=str(cwd),
                   check=True, capture_output=True, env=env)


class _RepoCase(unittest.TestCase):
    """A throwaway git repo wired as orphanwork's ROOT/STORE, with the
    ledger and the outbound ping stubbed so no test touches anything real
    on this machine."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        _git("init", "-q", "-b", "main", cwd=self.root)
        _git("config", "user.email", "t@example.com", cwd=self.root)
        _git("config", "user.name", "T", cwd=self.root)
        (self.root / "server").mkdir()
        (self.root / "server" / "main.py").write_text("# live\n",
                                                       encoding="utf-8")
        _git("add", "-A", cwd=self.root)
        _git("commit", "-qm", "init", cwd=self.root)

        self.store = self.root / "data" / "orphan-work.json"
        for target, value in (("ROOT", self.root), ("STORE", self.store)):
            p = mock.patch.object(orphanwork, target, value)
            p.start()
            self.addCleanup(p.stop)
        jp = mock.patch("server.joblog.list_records", return_value=[])
        jp.start()
        self.addCleanup(jp.stop)
        # update.status reads the REAL checkout's git (its ROOT is not
        # orphanwork.ROOT), so without this stub the live tree's own
        # unpushed-main state leaks an extra row into every fixture sweep
        # — found by running the suite in the live tree, where main was
        # transiently ahead; the worktree run was green only by luck
        up = mock.patch("server.update.status", return_value={"git": False})
        up.start()
        self.addCleanup(up.stop)
        np = mock.patch("server.notify.agent_ping", return_value=True)
        self.ping = np.start()
        self.addCleanup(np.stop)

    def make_worktree(self, slug, branch=None, dirty=False, commits=0):
        """A linked worktree on claude/<slug> (or `branch`), off main, with
        `commits` new commits and an optional uncommitted file."""
        branch = branch or f"claude/{slug}"
        wt = self.root / ".worktrees" / slug
        _git("worktree", "add", "-b", branch, str(wt), "main", cwd=self.root)
        for i in range(commits):
            (wt / f"file{i}.py").write_text(f"# change {i}\n",
                                            encoding="utf-8")
            _git("add", "-A", cwd=wt)
            _git("commit", "-qm", f"work {i}", cwd=wt)
        if dirty:
            (wt / "dirty.py").write_text("# wip\n", encoding="utf-8")
        return wt


class Classification(_RepoCase):
    def test_dirty_worktree_is_an_item(self):
        self.make_worktree("dirty-one", dirty=True)
        items = orphanwork.sweep()
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["branch"], "claude/dirty-one")
        self.assertEqual(it["kind"], "dirty")
        self.assertEqual(it["dirty"], 1)
        self.assertEqual(it["ahead"], 0)

    def test_clean_unmerged_worktree_is_an_item(self):
        self.make_worktree("has-commits", commits=2)
        items = orphanwork.sweep()
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["kind"], "unmerged")
        self.assertEqual(it["dirty"], 0)
        self.assertEqual(it["ahead"], 2)

    def test_clean_and_merged_is_excluded(self):
        """worktree.tidy() owns removing this case; the sweeper never
        invents work for something already fully landed."""
        self.make_worktree("merged-away", commits=1)
        _git("merge", "--no-ff", "-m", "merge it", "claude/merged-away",
            cwd=self.root)
        self.assertEqual(orphanwork.sweep(), [])

    def test_behind_only_branch_with_no_worktree_is_excluded(self):
        _git("branch", "claude/behind-only", "main", cwd=self.root)
        (self.root / "server" / "second.py").write_text("# more\n",
                                                         encoding="utf-8")
        _git("add", "-A", cwd=self.root)
        _git("commit", "-qm", "main moved on", cwd=self.root)
        self.assertEqual(orphanwork.sweep(), [])

    def test_branch_without_a_worktree_still_counts(self):
        wt = self.make_worktree("no-longer-checked-out", commits=1)
        _git("worktree", "remove", "--force", str(wt), cwd=self.root)
        items = orphanwork.sweep()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["worktree"], "")
        self.assertEqual(items[0]["ahead"], 1)

    def test_the_primary_checkout_is_never_an_item(self):
        (self.root / "server" / "scratch.py").write_text("# wip\n",
                                                          encoding="utf-8")
        items = orphanwork.sweep()
        self.assertEqual(items, [])


class Ordering(_RepoCase):
    def test_stalest_first_by_last_activity(self):
        self.make_worktree("old-one", commits=0)
        (self.root / ".worktrees" / "old-one" / "x.py").write_text(
            "# old\n", encoding="utf-8")
        _commit_at(self.root / ".worktrees" / "old-one", "old work",
                  "2020-01-01T00:00:00")

        self.make_worktree("new-one", commits=0)
        (self.root / ".worktrees" / "new-one" / "y.py").write_text(
            "# new\n", encoding="utf-8")
        _commit_at(self.root / ".worktrees" / "new-one", "new work",
                  "2024-06-01T00:00:00")

        orphanwork.refresh()
        branches = [it["branch"] for it in orphanwork.compose()["items"]]
        self.assertEqual(branches, ["claude/old-one", "claude/new-one"])

    def test_unpushed_main_is_pinned_last(self):
        self.make_worktree("some-work", commits=1)
        with mock.patch("server.update.status",
                        return_value={"git": True, "remote": True,
                                     "ahead": 2, "behind": 0, "sha": "deadbeef"}):
            orphanwork.refresh()
            out = orphanwork.compose()
        self.assertEqual(out["items"][-1]["kind"], "unpushed")

    def test_age_from_dirty_mtime_wins_over_an_older_commit(self):
        wt = self.make_worktree("aged", commits=0)
        (wt / "committed.py").write_text("# old\n", encoding="utf-8")
        _commit_at(wt, "old commit", "2020-01-01T00:00:00")
        recent = wt / "dirty.py"
        recent.write_text("# recent edit\n", encoding="utf-8")
        recent_ts = time.time() - 3600
        os.utime(recent, (recent_ts, recent_ts))
        it = orphanwork.sweep()[0]
        self.assertAlmostEqual(it["last_activity"], recent_ts, delta=5)

    def test_age_falls_back_to_the_commit_when_dirty_is_older(self):
        wt = self.make_worktree("aged2", commits=0)
        (wt / "committed.py").write_text("# recent commit\n", encoding="utf-8")
        recent_commit_ts = time.time() - 1800
        when = time.strftime("%Y-%m-%dT%H:%M:%S",
                             time.localtime(recent_commit_ts))
        _commit_at(wt, "recent commit", when)
        old_edit = wt / "dirty.py"
        old_edit.write_text("# stale edit\n", encoding="utf-8")
        old_ts = time.time() - 999999
        os.utime(old_edit, (old_ts, old_ts))
        it = orphanwork.sweep()[0]
        self.assertGreater(it["last_activity"], old_ts + 900000)


class DismissReArm(_RepoCase):
    def test_dismiss_hides_the_row(self):
        self.make_worktree("d1", commits=1)
        orphanwork.refresh()
        self.assertEqual(len(orphanwork.compose()["items"]), 1)
        key = orphanwork.compose()["items"][0]["key"]
        orphanwork.dismiss(key)
        self.assertEqual(orphanwork.compose()["items"], [])

    def test_a_new_commit_mints_a_new_key_and_the_row_returns(self):
        wt = self.make_worktree("d2", commits=1)
        orphanwork.refresh()
        key = orphanwork.compose()["items"][0]["key"]
        orphanwork.dismiss(key)
        self.assertEqual(orphanwork.compose()["items"], [])

        (wt / "more.py").write_text("# more\n", encoding="utf-8")
        _git("add", "-A", cwd=wt)
        _git("commit", "-qm", "more work", cwd=wt)
        orphanwork.refresh()
        items = orphanwork.compose()["items"]
        self.assertEqual(len(items), 1)
        self.assertNotEqual(items[0]["key"], key)

    def test_restore_brings_an_unchanged_row_back(self):
        self.make_worktree("d3", commits=1)
        orphanwork.refresh()
        key = orphanwork.compose()["items"][0]["key"]
        orphanwork.dismiss(key)
        self.assertEqual(orphanwork.compose()["items"], [])
        orphanwork.dismiss(key, restore=True)
        self.assertEqual(len(orphanwork.compose()["items"]), 1)


class BaselineNotify(_RepoCase):
    def test_the_first_ever_sweep_never_pings(self):
        self.make_worktree("w1", commits=1)
        orphanwork.refresh()
        self.ping.assert_not_called()

    def test_a_new_item_after_baseline_pings_once(self):
        self.make_worktree("w1", commits=1)
        orphanwork.refresh()
        self.ping.assert_not_called()
        self.make_worktree("w2", commits=1)
        orphanwork.refresh()
        self.ping.assert_called_once()

    def test_an_unchanged_third_sweep_does_not_re_ping(self):
        self.make_worktree("w1", commits=1)
        orphanwork.refresh()
        self.make_worktree("w2", commits=1)
        orphanwork.refresh()
        self.assertEqual(self.ping.call_count, 1)
        orphanwork.refresh()
        self.assertEqual(self.ping.call_count, 1)

    def test_a_dismissed_new_item_is_stamped_but_not_pinged(self):
        self.make_worktree("w1", commits=1)
        orphanwork.refresh()
        self.make_worktree("w2", commits=1)
        # dismiss the not-yet-swept item pre-emptively is unrealistic, so
        # instead dismiss right after this sweep would have pinged, then
        # confirm a THIRD sweep (nothing new) still does not re-ping
        orphanwork.refresh()
        self.assertEqual(self.ping.call_count, 1)
        key = next(it["key"] for it in orphanwork.compose()["items"]
                  if it["branch"] == "claude/w2")
        orphanwork.dismiss(key)
        orphanwork.refresh()
        self.assertEqual(self.ping.call_count, 1)


class LedgerJoin(_RepoCase):
    def test_an_orphaned_ledger_row_flags_the_item_stalled(self):
        self.make_worktree("stalled-one", commits=1)
        row = {"id": "j1", "branch": "claude/stalled-one", "status": "orphaned",
              "prompt": "do the thing", "idea_id": None, "publish_plan": False,
              "meta": {}, "finished": None, "title": ""}
        with mock.patch("server.joblog.list_records", return_value=[row]):
            it = orphanwork.sweep()[0]
        self.assertTrue(it["stalled"])
        self.assertEqual(it["job"]["status"], "orphaned")
        self.assertEqual(it["job"]["id"], "j1")

    def test_an_errored_ledger_row_also_flags_stalled(self):
        self.make_worktree("errored-one", commits=1)
        row = {"id": "j2", "branch": "claude/errored-one", "status": "error",
              "prompt": "do the thing", "meta": {}, "title": ""}
        with mock.patch("server.joblog.list_records", return_value=[row]):
            it = orphanwork.sweep()[0]
        self.assertTrue(it["stalled"])

    def test_a_running_job_is_not_orphan_work_at_all(self):
        # the judge's high finding: a live session's dirty tree is work in
        # progress — a row here would carry a Resume button that drops a
        # second agent into a tree another agent is writing
        self.make_worktree("running-one", dirty=True, commits=1)
        row = {"id": "j3", "branch": "claude/running-one", "status": "running",
              "prompt": "do the thing", "meta": {}, "title": ""}
        with mock.patch("server.joblog.list_records", return_value=[row]):
            items = orphanwork.sweep()
        self.assertEqual([i for i in items
                          if i["branch"] == "claude/running-one"], [])

    def test_the_row_returns_once_the_session_ends(self):
        self.make_worktree("running-two", dirty=True, commits=1)
        row = {"id": "j4", "branch": "claude/running-two", "status": "done",
              "prompt": "do the thing", "meta": {}, "title": ""}
        with mock.patch("server.joblog.list_records", return_value=[row]):
            items = orphanwork.sweep()
        self.assertEqual(len([i for i in items
                              if i["branch"] == "claude/running-two"]), 1)

    def test_resume_refuses_while_a_session_is_live_on_the_branch(self):
        # checked FRESH at click time, never off the possibly stale item
        wt = self.make_worktree("busy-one", dirty=True, commits=1)
        item = {"branch": "claude/busy-one", "worktree": str(wt)}
        row = {"id": "j5", "branch": "claude/busy-one", "status": "running",
              "prompt": "p", "meta": {}, "title": ""}
        with mock.patch("server.joblog.list_records", return_value=[row]):
            with self.assertRaises(ValueError) as cm:
                orphanwork.resume(item)
        self.assertIn("already live", str(cm.exception))

    def test_resume_refuses_while_an_action_runs_on_the_branch(self):
        wt = self.make_worktree("busy-two", dirty=True, commits=1)
        item = {"branch": "claude/busy-two", "worktree": str(wt)}
        with orphanwork._actions_lock:
            orphanwork._actions["claude/busy-two"] = {
                "name": "merge", "status": "running", "output": "",
                "started": "now", "finished": None}
        self.addCleanup(lambda: orphanwork._actions.pop("claude/busy-two", None))
        with self.assertRaises(ValueError) as cm:
            orphanwork.resume(item)
        self.assertIn("already running", str(cm.exception))

    def test_the_newest_row_wins_when_a_branch_has_several(self):
        self.make_worktree("reused-branch", commits=1)
        rows = [
            {"id": "old", "branch": "claude/reused-branch", "status": "done",
             "prompt": "first attempt", "meta": {}, "title": ""},
            {"id": "new", "branch": "claude/reused-branch", "status": "orphaned",
             "prompt": "second attempt", "meta": {}, "title": ""},
        ]
        with mock.patch("server.joblog.list_records", return_value=rows):
            it = orphanwork.sweep()[0]
        self.assertEqual(it["job"]["id"], "new")
        self.assertTrue(it["stalled"])


class UnpushedMain(_RepoCase):
    def test_ahead_of_upstream_produces_an_item(self):
        with mock.patch("server.update.status",
                        return_value={"git": True, "remote": True, "ahead": 3,
                                      "behind": 0, "sha": "abc123"}):
            items = orphanwork.sweep()
        unpushed = [it for it in items if it["kind"] == "unpushed"]
        self.assertEqual(len(unpushed), 1)
        self.assertEqual(unpushed[0]["ahead"], 3)
        self.assertEqual(unpushed[0]["key"], "unpushed-main:abc123")

    def test_no_remote_produces_nothing(self):
        with mock.patch("server.update.status",
                        return_value={"git": True, "remote": False}):
            items = orphanwork.sweep()
        self.assertFalse(any(it["kind"] == "unpushed" for it in items))

    def test_nothing_ahead_produces_nothing(self):
        with mock.patch("server.update.status",
                        return_value={"git": True, "remote": True, "ahead": 0,
                                      "behind": 2, "sha": "abc123"}):
            items = orphanwork.sweep()
        self.assertFalse(any(it["kind"] == "unpushed" for it in items))


class ResumePromptContent(_RepoCase):
    def test_names_the_worktree_branch_and_decision_menu(self):
        wt = self.make_worktree("p1", commits=1, dirty=True)
        item = {"worktree": str(wt), "branch": "claude/p1", "job": None}
        text = orphanwork.resume_prompt(item)
        self.assertIn(str(wt), text)
        self.assertIn("claude/p1", text)
        self.assertIn("dirty.py", text)          # from git status --porcelain
        self.assertIn("do NOT", text)
        self.assertIn("merge it", text)
        self.assertIn("discard it", text)

    def test_names_the_originating_job_when_known(self):
        wt = self.make_worktree("p2", commits=1)
        item = {"worktree": str(wt), "branch": "claude/p2",
                "job": {"title": "Implement — widget thing", "status": "error"}}
        text = orphanwork.resume_prompt(item)
        self.assertIn("Implement — widget thing", text)
        self.assertIn("error", text)

    def test_no_job_omits_the_block_without_erroring(self):
        wt = self.make_worktree("p3", commits=1)
        item = {"worktree": str(wt), "branch": "claude/p3", "job": None}
        text = orphanwork.resume_prompt(item)
        self.assertNotIn("originating job", text)


class RouteLayer(_RepoCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from server import main
        cls.client = TestClient(main.app)

    def setUp(self):
        super().setUp()
        os.environ.pop("VIRA_PASSIVE", None)
        self.addCleanup(os.environ.pop, "VIRA_PASSIVE", None)

    def test_get_shape(self):
        self.make_worktree("r1", commits=1)
        orphanwork.refresh()
        r = self.client.get("/api/orphanwork")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("items", body)
        self.assertIn("last_sweep", body)
        self.assertIn("stale", body)
        self.assertEqual(len(body["items"]), 1)

    def test_refresh_route(self):
        self.make_worktree("r2", commits=1)
        r = self.client.post("/api/orphanwork/refresh")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["items"]), 1)

    def test_dismiss_route(self):
        self.make_worktree("r3", commits=1)
        orphanwork.refresh()
        key = orphanwork.compose()["items"][0]["key"]
        r = self.client.post("/api/orphanwork/dismiss", json={"key": key})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(orphanwork.compose()["items"], [])

    def test_resume_404_on_unknown_key(self):
        r = self.client.post("/api/orphanwork/resume", json={"key": "nope"})
        self.assertEqual(r.status_code, 404)

    def test_resume_409_when_no_worktree(self):
        wt = self.make_worktree("no-wt-now", commits=1)
        _git("worktree", "remove", "--force", str(wt), cwd=self.root)
        orphanwork.refresh()
        key = orphanwork.compose()["items"][0]["key"]
        r = self.client.post("/api/orphanwork/resume", json={"key": key})
        self.assertEqual(r.status_code, 409)

    def test_resume_403_when_passive(self):
        self.make_worktree("r4", commits=1)
        orphanwork.refresh()
        key = orphanwork.compose()["items"][0]["key"]
        os.environ["VIRA_PASSIVE"] = "1"
        r = self.client.post("/api/orphanwork/resume", json={"key": key})
        self.assertEqual(r.status_code, 403)

    def test_merge_403_when_passive(self):
        self.make_worktree("r5", commits=1)
        orphanwork.refresh()
        key = orphanwork.compose()["items"][0]["key"]
        os.environ["VIRA_PASSIVE"] = "1"
        r = self.client.post("/api/orphanwork/merge", json={"key": key})
        self.assertEqual(r.status_code, 403)

    def test_discard_403_when_passive(self):
        self.make_worktree("r6", commits=1)
        orphanwork.refresh()
        key = orphanwork.compose()["items"][0]["key"]
        os.environ["VIRA_PASSIVE"] = "1"
        r = self.client.post("/api/orphanwork/discard", json={"key": key})
        self.assertEqual(r.status_code, 403)

    def test_resume_prompt_route_has_no_side_effects_and_works_passive(self):
        self.make_worktree("r7", commits=1)
        orphanwork.refresh()
        key = orphanwork.compose()["items"][0]["key"]
        os.environ["VIRA_PASSIVE"] = "1"
        r = self.client.get("/api/orphanwork/resume-prompt",
                            params={"key": key})
        self.assertEqual(r.status_code, 200)
        self.assertIn("prompt", r.json())
        self.assertIn("cwd", r.json())

    def test_resume_prompt_404_on_unknown_key(self):
        r = self.client.get("/api/orphanwork/resume-prompt",
                            params={"key": "nope"})
        self.assertEqual(r.status_code, 404)


@unittest.skipUnless(os.name == "posix",
                     "the branch.sh stand-in is a shell script")
class ActionRunner(_RepoCase):
    """merge()/discard() against a stand-in scripts/branch.sh that just
    echoes its argv and exits 0 — branch.sh itself is not re-tested here
    (its own suite owns that); this proves orphanwork calls it correctly,
    passes its output through, pushes on a successful merge, and guards
    against two actions racing on the same branch."""

    def setUp(self):
        super().setUp()
        sh = self.root / "scripts" / "branch.sh"
        sh.parent.mkdir(parents=True, exist_ok=True)
        sh.write_text('#!/bin/sh\necho "branch.sh $*"\nexit 0\n', encoding="utf-8")
        sh.chmod(0o755)
        self.remote = Path(self.tmp.name) / "remote.git"
        self.remote.mkdir()
        _git("init", "-q", "--bare", cwd=self.remote)
        _git("remote", "add", "origin", str(self.remote), cwd=self.root)
        _git("push", "-q", "-u", "origin", "main", cwd=self.root)
        self.addCleanup(orphanwork._actions.clear)

    def _wait(self, branch, timeout=5):
        t0 = time.time()
        while time.time() - t0 < timeout:
            a = orphanwork._actions.get(branch)
            if a and a.get("status") != "running":
                return a
            time.sleep(0.05)
        self.fail(f"action for {branch} never finished")

    def test_in_flight_action_is_refused(self):
        orphanwork._actions["claude/x"] = {
            "name": "merge", "status": "running", "output": "",
            "started": "now", "finished": None}
        started, detail = orphanwork.merge("x")
        self.assertFalse(started)
        self.assertIn("already running", detail)

    def test_merge_runs_branch_sh_then_pushes(self):
        started, detail = orphanwork.merge("some-slug")
        self.assertTrue(started, detail)
        a = self._wait("claude/some-slug")
        self.assertEqual(a["status"], "ok")
        self.assertIn("branch.sh merge some-slug", a["output"])
        self.assertIn("push:", a["output"])

    def test_discard_passes_the_force_flag(self):
        started, _ = orphanwork.discard("y", force=True)
        self.assertTrue(started)
        a = self._wait("claude/y")
        self.assertIn("branch.sh discard y --force", a["output"])

    def test_discard_without_force_omits_the_flag(self):
        started, _ = orphanwork.discard("z")
        self.assertTrue(started)
        a = self._wait("claude/z")
        self.assertIn("branch.sh discard z", a["output"])
        self.assertNotIn("--force", a["output"])

    def test_a_server_change_names_the_restart(self):
        sh = self.root / "scripts" / "branch.sh"
        sh.write_text('#!/bin/sh\necho "modified server/main.py"\nexit 0\n', encoding="utf-8")
        sh.chmod(0o755)
        started, _ = orphanwork.merge("srv-change")
        self.assertTrue(started)
        a = self._wait("claude/srv-change")
        self.assertIn("restart is the owner's", a["output"])

    def test_a_failing_branch_sh_records_failed(self):
        sh = self.root / "scripts" / "branch.sh"
        sh.write_text('#!/bin/sh\necho "refusing: dirty tree" >&2\nexit 1\n', encoding="utf-8")
        sh.chmod(0o755)
        started, _ = orphanwork.merge("will-fail")
        self.assertTrue(started)
        a = self._wait("claude/will-fail")
        self.assertEqual(a["status"], "failed")
        self.assertIn("refusing", a["output"])


if __name__ == "__main__":
    unittest.main()
