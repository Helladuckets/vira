"""Session-registry tests: the legacy /api/jobs response-shape contract
(now assembled from a detached job dir), launch-mode derivation, the
SDK-absent fallback path, and the live-session cap. The runner itself
(gate + control protocol) is covered in test_runner.py.

Run: .venv/bin/python -m unittest discover tests
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import jobfiles, session

# The exact keys the pre-session Jobs dict carried; GET /api/jobs/{id}
# consumers (the terminal render, joblog) rely on every one of them.
LEGACY_JOB_KEYS = {"id", "prompt", "cwd", "status", "output", "started",
                   "finished", "permission_mode", "model", "publish_plan",
                   "idea_id", "session_id"}
NEW_JOB_KEYS = {"mode", "awaiting", "live", "pending"}


def make_registry():
    return session.Sessions()


def make_detached(registry, tmp, jid="d" * 12, status="running",
                  pending=(), output="[vira] test-model working…\n"):
    """Register a detached handle over a hand-built job dir — exactly what
    a re-attach or a spawn produces, minus the process."""
    jdir = Path(tmp) / jid
    jdir.mkdir(parents=True, exist_ok=True)
    spec = {"id": jid, "prompt": "do the thing", "cwd": "/tmp",
            "model": None, "model_resolved": "test-model",
            "permission_mode": None, "publish_plan": False,
            "idea_id": None, "mode": "interactive", "started": time.time(),
            "auto_allow": [], "permission_timeout": 600}
    state = {"id": jid, "status": status, "started": spec["started"],
             "finished": None if status == "running" else time.time(),
             "session_id": "", "awaiting": None, "pending": list(pending),
             "result_text": "", "heartbeat": time.time(), "pid": 12345,
             "mode": "interactive", "live": True, "error": ""}
    (jdir / "job.json").write_text(json.dumps(spec))
    (jdir / "state.json").write_text(json.dumps(state))
    (jdir / "output.log").write_text(output)
    h = session.DetachedJob(jid, jdir, spec)
    h.last_state = state
    registry.sessions[jid] = h
    return h


class DetachedSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_snapshot_carries_legacy_and_new_keys(self):
        reg = make_registry()
        make_detached(reg, self.tmp.name)
        snap = reg.get("d" * 12)
        self.assertTrue(LEGACY_JOB_KEYS.issubset(snap.keys()),
                        LEGACY_JOB_KEYS - set(snap.keys()))
        self.assertTrue(NEW_JOB_KEYS.issubset(snap.keys()),
                        NEW_JOB_KEYS - set(snap.keys()))
        self.assertTrue(snap["live"])
        self.assertIn("working", snap["output"])
        json.dumps(snap)                          # JSON-safe end to end

    def test_pending_cards_ride_the_snapshot(self):
        reg = make_registry()
        make_detached(reg, self.tmp.name, pending=[
            {"req_id": "r1", "tool": "Bash", "summary": "Bash: ls",
             "preview": "ls", "created": 2.0},
            {"req_id": "r0", "tool": "Write", "summary": "Write x",
             "preview": "x", "created": 1.0}])
        snap = reg.get("d" * 12)
        self.assertEqual([p["req_id"] for p in snap["pending"]],
                         ["r0", "r1"])            # sorted by created

    def test_recent_shape(self):
        reg = make_registry()
        make_detached(reg, self.tmp.name)
        (row,) = reg.recent()
        for k in ("id", "prompt", "status", "started", "finished",
                  "mode", "awaiting"):
            self.assertIn(k, row)

    def test_controls_append_to_control_file(self):
        reg = make_registry()
        h = make_detached(reg, self.tmp.name,
                          pending=[{"req_id": "r1", "tool": "Bash",
                                    "summary": "s", "preview": "",
                                    "created": 1.0}])
        reg.say(h.id, "steer it")
        reg.permission(h.id, "r1", True, "session")
        reg.interrupt(h.id)
        reg.close(h.id)
        _, cmds = jobfiles.read_control(h.dir, 0)
        self.assertEqual([c["op"] for c in cmds],
                         ["say", "permission", "interrupt", "close"])
        self.assertEqual(cmds[1]["scope"], "session")

    def test_permission_unknown_request_raises(self):
        reg = make_registry()
        h = make_detached(reg, self.tmp.name)
        with self.assertRaises(KeyError):
            reg.permission(h.id, "nope", True)

    def test_controls_rejected_when_not_running(self):
        reg = make_registry()
        h = make_detached(reg, self.tmp.name, status="done")
        with self.assertRaises(ValueError):
            reg.say(h.id, "too late")

    def test_empty_say_rejected(self):
        reg = make_registry()
        h = make_detached(reg, self.tmp.name)
        with self.assertRaises(ValueError):
            reg.say(h.id, "   ")


class LaunchTests(unittest.TestCase):
    def _launch_stubbed(self, reg, **kwargs):
        """Launch through the real code path with the subprocess runner
        stubbed out (no claude CLI, no joblog writes)."""
        def fake_run(_self, s):
            s.data["status"] = "done"
            s.data["finished"] = 1.0

        with mock.patch.object(session.Sessions, "_run_subprocess", fake_run):
            jid = reg.launch("do the thing", cwd="/tmp", **kwargs)
        return jid

    def test_mode_derivation_from_legacy_permission_mode(self):
        reg = make_registry()
        with mock.patch.object(session, "SDK_AVAILABLE", False):
            jid = self._launch_stubbed(
                reg, permission_mode="bypassPermissions")
        self.assertEqual(reg.get(jid)["mode"], "autopilot")
        self.assertEqual(reg.get(jid)["permission_mode"], "bypassPermissions")

    def test_explicit_mode_wins(self):
        reg = make_registry()
        with mock.patch.object(session, "SDK_AVAILABLE", False):
            jid = self._launch_stubbed(reg, mode="interactive")
        self.assertEqual(reg.get(jid)["mode"], "interactive")

    def test_sdk_absent_falls_back_and_says_so(self):
        reg = make_registry()
        ran = []
        with mock.patch.object(session, "SDK_AVAILABLE", False), \
             mock.patch.object(session.Sessions, "_run_subprocess",
                               lambda self, s: ran.append(s.data["id"])):
            jid = reg.launch("hello", mode="interactive")
        snap = reg.get(jid)
        self.assertFalse(snap["live"])
        self.assertIn("interactive session unavailable", snap["output"])
        self.assertIn("claude-agent-sdk not installed", snap["output"])
        for _ in range(200):
            if ran:
                break
            time.sleep(0.01)
        self.assertEqual(ran, [jid])              # the legacy path really ran

    def test_steering_rejected_on_non_live_session(self):
        reg = make_registry()
        with mock.patch.object(session, "SDK_AVAILABLE", False), \
             mock.patch.object(session.Sessions, "_run_subprocess",
                               lambda self, s: None):
            jid = reg.launch("hello")
        with self.assertRaises(ValueError):
            reg.say(jid, "steer this")

    def test_sdk_present_spawns_detached_runner(self):
        reg = make_registry()
        spawned = []

        def fake_spawn(_self, data):
            spawned.append(data["id"])
            h = session.DetachedJob(data["id"], "/nonexistent",
                                    {"prompt": data["prompt"],
                                     "cwd": data["cwd"], "mode": data["mode"],
                                     "started": data["started"]})
            h.last_state = {"status": "running"}
            return h

        with mock.patch.object(session, "SDK_AVAILABLE", True), \
             mock.patch.object(session.Sessions, "_spawn_runner", fake_spawn):
            jid = reg.launch("hello", mode="autopilot")
        self.assertEqual(spawned, [jid])
        self.assertEqual(reg.sessions[jid].kind, "detached")

    def test_live_session_cap(self):
        reg = make_registry()

        def fake_spawn(_self, data):
            h = session.DetachedJob(data["id"], "/nonexistent",
                                    {"prompt": data["prompt"],
                                     "cwd": data["cwd"], "mode": data["mode"],
                                     "started": data["started"]})
            h.last_state = {"status": "running"}
            return h

        with mock.patch.object(session, "SDK_AVAILABLE", True), \
             mock.patch.object(session.Sessions, "_spawn_runner",
                               fake_spawn), \
             mock.patch.object(session, "_scfg",
                               side_effect=lambda k:
                               1 if k == "session_max_live"
                               else session.SESSION_DEFAULTS[k]):
            reg.launch("first")
            with self.assertRaises(ValueError):
                reg.launch("second")


if __name__ == "__main__":
    unittest.main()
