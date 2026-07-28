"""Durability tests: boot re-attach vs orphan finalization, the joblog's
cross-process semantics (no cache, external writes visible), and the
history / disk-fallback surfaces that let finished jobs reopen after the
in-memory registry is gone.

Run: .venv/bin/python -m unittest discover tests
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import jobfiles, joblog, session

DEAD_PID = 4194304 + 1717  # above macOS's pid ceiling — never a live process


def write_job_dir(root, jid, status="running", heartbeat=None, pid=None):
    jdir = Path(root) / jid
    jdir.mkdir(parents=True, exist_ok=True)
    spec = {"id": jid, "prompt": "p-" + jid, "cwd": "/tmp", "model": None,
            "model_resolved": "test-model", "permission_mode": None,
            "publish_plan": False, "idea_id": None, "mode": "interactive",
            "started": time.time() - 60, "auto_allow": [],
            "permission_timeout": 600}
    state = {"id": jid, "status": status, "started": spec["started"],
             "finished": None, "session_id": "", "awaiting": None,
             "pending": [], "result_text": "",
             "heartbeat": time.time() if heartbeat is None else heartbeat,
             "pid": os.getpid() if pid is None else pid,
             "mode": "interactive", "live": True, "error": ""}
    (jdir / "job.json").write_text(json.dumps(spec))
    (jdir / "state.json").write_text(json.dumps(state))
    (jdir / "output.log").write_text("[vira] test-model working…\n")
    return jdir


class ReattachTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs_root = Path(self.tmp.name) / "jobs"
        self.store = Path(self.tmp.name) / "jobs-log.json"
        patches = [
            mock.patch.object(jobfiles, "JOBS_DIR", self.jobs_root),
            mock.patch.object(joblog, "STORE", self.store),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_live_runner_is_reattached(self):
        # our own pid + fresh heartbeat = alive
        write_job_dir(self.jobs_root, "alive0000001")
        joblog.record_launch({"id": "alive0000001", "prompt": "p",
                              "cwd": "/tmp", "mode": "interactive"})
        reg = session.Sessions()
        alive = reg._boot_reattach()
        joblog.sweep_orphans(alive)
        self.assertEqual(alive, ["alive0000001"])
        self.assertIn("alive0000001", reg.sessions)
        snap = reg.get("alive0000001")
        self.assertEqual(snap["status"], "running")
        self.assertTrue(snap["live"])
        # the ledger record was NOT swept
        self.assertEqual(joblog.get_record("alive0000001")["status"],
                         "running")

    def test_dead_runner_is_finalized_orphaned(self):
        write_job_dir(self.jobs_root, "dead00000001",
                      heartbeat=time.time() - 3600, pid=DEAD_PID)
        joblog.record_launch({"id": "dead00000001", "prompt": "p",
                              "cwd": "/tmp", "mode": "interactive"})
        reg = session.Sessions()
        alive = reg._boot_reattach()
        joblog.sweep_orphans(alive)
        self.assertEqual(alive, [])
        self.assertNotIn("dead00000001", reg.sessions)
        st = json.loads(
            (self.jobs_root / "dead00000001" / "state.json").read_text())
        self.assertEqual(st["status"], "orphaned")
        self.assertIsNotNone(st["finished"])
        self.assertEqual(joblog.get_record("dead00000001")["status"],
                         "orphaned")

    def test_fresh_heartbeat_counts_as_alive_even_with_dead_pid(self):
        # pid recycled/dead but heartbeat is fresh — trust the heartbeat
        write_job_dir(self.jobs_root, "hbfresh00001", pid=DEAD_PID)
        reg = session.Sessions()
        alive = reg._boot_reattach()
        self.assertEqual(alive, ["hbfresh00001"])

    def test_finished_dirs_are_left_alone(self):
        write_job_dir(self.jobs_root, "done00000001", status="done")
        reg = session.Sessions()
        alive = reg._boot_reattach()
        self.assertEqual(alive, [])
        self.assertNotIn("done00000001", reg.sessions)
        st = json.loads(
            (self.jobs_root / "done00000001" / "state.json").read_text())
        self.assertEqual(st["status"], "done")   # untouched

    def test_ledger_records_without_a_live_runner_are_swept(self):
        # a legacy-era record (no job dir at all) still running -> orphaned
        joblog.record_launch({"id": "legacy000001", "prompt": "p",
                              "cwd": "/tmp", "mode": "interactive"})
        reg = session.Sessions()
        alive = reg._boot_reattach()
        joblog.sweep_orphans(alive)
        self.assertEqual(joblog.get_record("legacy000001")["status"],
                         "orphaned")


class JoblogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "jobs-log.json"
        p = mock.patch.object(joblog, "STORE", self.store)
        p.start()
        self.addCleanup(p.stop)

    def test_external_writes_are_visible(self):
        """No process-lifetime cache: what another process writes, this one
        reads back — the property the detached runners rely on."""
        joblog.record_launch({"id": "j1", "prompt": "p", "cwd": "/tmp",
                              "mode": "interactive"})
        # simulate the runner process finishing the job out-of-band
        s = json.loads(self.store.read_text())
        s["jobs"][0]["status"] = "done"
        s["jobs"][0]["result"] = "runner wrote this"
        self.store.write_text(json.dumps(s))
        rec = joblog.get_record("j1")
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["result"], "runner wrote this")

    def test_recent_is_newest_first(self):
        for i in range(3):
            joblog.record_launch({"id": f"j{i}", "prompt": "p",
                                  "cwd": "/tmp", "mode": "interactive"})
        self.assertEqual([r["id"] for r in joblog.recent()],
                         ["j2", "j1", "j0"])

    def test_finish_caps_result(self):
        joblog.record_launch({"id": "j1", "prompt": "p", "cwd": "/tmp",
                              "mode": "interactive"})
        joblog.record_finish("j1", "done", "x" * 10000)
        self.assertEqual(len(joblog.get_record("j1")["result"]),
                         joblog.RESULT_CAP)

    def test_mark_orphaned_only_touches_running(self):
        joblog.record_launch({"id": "j1", "prompt": "p", "cwd": "/tmp",
                              "mode": "interactive"})
        joblog.record_finish("j1", "done", "ok")
        joblog.mark_orphaned("j1")
        self.assertEqual(joblog.get_record("j1")["status"], "done")


class DiskFallbackTests(unittest.TestCase):
    """GET /api/jobs/{id} for a job the live registry no longer holds."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs_root = Path(self.tmp.name) / "jobs"
        self.store = Path(self.tmp.name) / "jobs-log.json"
        for p in (mock.patch.object(jobfiles, "JOBS_DIR", self.jobs_root),
                  mock.patch.object(joblog, "STORE", self.store)):
            p.start()
            self.addCleanup(p.stop)

    def test_snapshot_from_ledger_and_job_dir(self):
        from server.main import _job_from_disk
        write_job_dir(self.jobs_root, "hist00000001", status="done")
        joblog.record_launch({"id": "hist00000001", "prompt": "p-hist",
                              "cwd": "/tmp", "mode": "autopilot",
                              "permission_mode": "bypassPermissions"})
        joblog.record_finish("hist00000001", "done", "final text")
        snap = _job_from_disk("hist00000001")
        self.assertEqual(snap["status"], "done")
        self.assertEqual(snap["prompt"], "p-hist")
        self.assertIn("working", snap["output"])   # read from output.log
        self.assertFalse(snap["live"])
        self.assertEqual(snap["pending"], [])
        self.assertIsInstance(snap["started"], float)
        json.dumps(snap)

    def test_unknown_job_returns_none(self):
        from server.main import _job_from_disk
        self.assertIsNone(_job_from_disk("nope"))


class PendingRouteTests(unittest.TestCase):
    """GET /api/sessions/pending — the feed behind the app-wide decision
    cards. Every row must name its session the way every other job surface
    does, or the card cannot say who is asking."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs_root = Path(self.tmp.name) / "jobs"
        self.store = Path(self.tmp.name) / "jobs-log.json"
        for p in (mock.patch.object(jobfiles, "JOBS_DIR", self.jobs_root),
                  mock.patch.object(joblog, "STORE", self.store)):
            p.start()
            self.addCleanup(p.stop)

    def _register(self, reg, jid, pending):
        jdir = write_job_dir(self.jobs_root, jid)
        st = json.loads((jdir / "state.json").read_text(encoding="utf-8"))
        st["pending"] = pending
        (jdir / "state.json").write_text(json.dumps(st), encoding="utf-8")
        spec = json.loads((jdir / "job.json").read_text(encoding="utf-8"))
        h = session.DetachedJob(jid, jdir, spec)
        h.last_state = st
        reg.sessions[jid] = h

    def test_rows_carry_the_card_and_the_session_name(self):
        from server import main
        reg = session.Sessions()
        self._register(reg, "pend00000001", [
            {"req_id": "q1", "kind": "ask", "question": "Fold or keep?",
             "options": ["Fold", "Keep"], "created": 1.0}])
        joblog.record_launch({"id": "pend00000001", "prompt": "tidy the queue",
                              "cwd": "/tmp", "mode": "interactive"})
        with mock.patch.object(main, "jobs", reg):
            payload = main.api_sessions_pending()
        (row,) = payload["pending"]
        self.assertEqual(row["job_id"], "pend00000001")
        self.assertEqual(row["card"]["req_id"], "q1")
        self.assertTrue(row["title"])
        self.assertIn("command", row)
        json.dumps(payload)

    def test_no_live_sessions_is_an_empty_list_not_an_error(self):
        from server import main
        with mock.patch.object(main, "jobs", session.Sessions()):
            self.assertEqual(main.api_sessions_pending(), {"pending": []})


if __name__ == "__main__":
    unittest.main()
