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


class PendingAllTests(unittest.TestCase):
    """The app-wide decision list behind the floating approval/question
    cards: every unanswered card across every live session, oldest first."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_cards_from_every_session_oldest_first(self):
        reg = make_registry()
        make_detached(reg, self.tmp.name, jid="a" * 12, pending=[
            {"req_id": "r2", "tool": "Bash", "summary": "ls", "created": 3.0}])
        make_detached(reg, self.tmp.name, jid="b" * 12, pending=[
            {"req_id": "r1", "kind": "ask", "question": "which?",
             "created": 1.0},
            {"req_id": "r3", "tool": "Write", "summary": "w", "created": 9.0}])
        rows = reg.pending_all()
        self.assertEqual([r["card"]["req_id"] for r in rows],
                         ["r1", "r2", "r3"])
        self.assertEqual([r["job_id"] for r in rows],
                         ["b" * 12, "a" * 12, "b" * 12])
        json.dumps(rows)                          # JSON-safe end to end

    def test_a_finished_session_contributes_nothing(self):
        reg = make_registry()
        make_detached(reg, self.tmp.name, jid="c" * 12, status="done",
                      pending=[{"req_id": "r1", "tool": "Bash",
                                "created": 1.0}])
        self.assertEqual(reg.pending_all(), [])

    def test_malformed_cards_are_skipped_not_rendered(self):
        # a card with no req_id cannot be answered, so it must never reach
        # the owner as something to click
        reg = make_registry()
        make_detached(reg, self.tmp.name, jid="e" * 12,
                      pending=["nonsense", {"tool": "Bash"},
                               {"req_id": "ok", "tool": "Bash",
                                "created": 1.0}])
        self.assertEqual([r["card"]["req_id"] for r in reg.pending_all()],
                         ["ok"])

    def test_state_is_read_fresh_from_disk(self):
        # the supervisor's cache does not run on a passive instance, and a
        # decision list that silently stops updating is the one thing this
        # surface must not do
        reg = make_registry()
        h = make_detached(reg, self.tmp.name, jid="f" * 12, pending=[])
        self.assertEqual(reg.pending_all(), [])
        st = json.loads((h.dir / "state.json").read_text(encoding="utf-8"))
        st["pending"] = [{"req_id": "late", "tool": "Bash", "created": 1.0}]
        (h.dir / "state.json").write_text(json.dumps(st), encoding="utf-8")
        self.assertEqual([r["card"]["req_id"] for r in reg.pending_all()],
                         ["late"])

    def test_the_read_does_not_disturb_the_supervisor_cache(self):
        # _poll_once detects a status transition by comparing its cached
        # status against a fresh read; refreshing that cache from here would
        # swallow the very event it is watching for
        reg = make_registry()
        h = make_detached(reg, self.tmp.name, jid="g" * 12)
        st = json.loads((h.dir / "state.json").read_text(encoding="utf-8"))
        st["status"] = "done"
        (h.dir / "state.json").write_text(json.dumps(st), encoding="utf-8")
        reg.pending_all()
        self.assertEqual(h.last_state["status"], "running")

    def test_legacy_fallback_sessions_are_not_listed(self):
        # a legacy subprocess run has no gate and cannot be answered
        reg = make_registry()
        reg.sessions["leg"] = session.Session({"id": "leg",
                                               "status": "running"})
        self.assertEqual(reg.pending_all(), [])

    def test_shape_carries_what_the_card_needs_to_name_its_session(self):
        reg = make_registry()
        make_detached(reg, self.tmp.name, jid="h" * 12, pending=[
            {"req_id": "r1", "tool": "Bash", "created": 1.0}])
        (row,) = reg.pending_all()
        for k in ("job_id", "mode", "provider", "cwd", "card"):
            self.assertIn(k, row)
        self.assertEqual(row["mode"], "interactive")


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

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

    def test_answer_appends_to_the_control_file(self):
        reg = make_registry()
        h = make_detached(reg, self.tmp.name, pending=[
            {"req_id": "q1", "kind": "ask", "question": "Fold or keep?",
             "options": ["Fold", "Keep"], "summary": "Fold or keep?",
             "created": 1.0}])
        reg.answer(h.id, "q1", "Fold")
        _, cmds = jobfiles.read_control(h.dir, 0)
        self.assertEqual(cmds[-1]["op"], "answer")
        self.assertEqual(cmds[-1]["answer"], "Fold")

    def test_answering_an_unknown_question_raises(self):
        """A double-tap on a phone must not look like it worked."""
        reg = make_registry()
        h = make_detached(reg, self.tmp.name)
        with self.assertRaises(KeyError):
            reg.answer(h.id, "nope", "Fold")

    def test_a_permission_card_is_not_answerable(self):
        """The two card kinds resolve futures of different SHAPES, so
        answering a permission card would hand the gate a string."""
        reg = make_registry()
        h = make_detached(reg, self.tmp.name, pending=[
            {"req_id": "r1", "tool": "Bash", "summary": "s", "preview": "",
             "created": 1.0}])
        with self.assertRaises(KeyError):
            reg.answer(h.id, "r1", "Fold")

    def test_empty_answer_rejected(self):
        reg = make_registry()
        h = make_detached(reg, self.tmp.name, pending=[
            {"req_id": "q1", "kind": "ask", "question": "q", "options": [],
             "summary": "q", "created": 1.0}])
        with self.assertRaises(ValueError):
            reg.answer(h.id, "q1", "   ")

    def test_permission_unknown_request_raises(self):
        reg = make_registry()
        h = make_detached(reg, self.tmp.name)
        with self.assertRaises(KeyError):
            reg.permission(h.id, "nope", True)

    def test_controls_other_than_say_rejected_when_not_running(self):
        # These drive the runner through its control file, so a dead session
        # would take the write and nobody would ever read it — a silent
        # no-op. `say` is deliberately NOT here: it resumes instead (see
        # ResumeTests), which is the one control that has somewhere to go.
        reg = make_registry()
        h = make_detached(reg, self.tmp.name, status="done")
        for call in (lambda: reg.interrupt(h.id),
                     lambda: reg.close(h.id),
                     lambda: reg.permission(h.id, "req1", True),
                     lambda: reg.answer(h.id, "req1", "yes")):
            with self.assertRaises(ValueError):
                call()

    def test_say_on_an_unknown_job_is_still_a_404(self):
        # Resuming looks the job up in the ledger; one that exists nowhere
        # must stay a KeyError (404) rather than becoming a confusing 409.
        reg = make_registry()
        with mock.patch.object(session.joblog, "get_record",
                               lambda jid: None):
            with self.assertRaises(KeyError):
                reg.say("nosuchjob1234", "hello?")

    def test_empty_say_rejected(self):
        reg = make_registry()
        h = make_detached(reg, self.tmp.name)
        with self.assertRaises(ValueError):
            reg.say(h.id, "   ")


class LaunchTests(unittest.TestCase):
    def _launch_stubbed(self, reg, **kwargs):
        """Launch through the real code path with the subprocess runner
        stubbed out (no claude CLI, no joblog writes). These are legacy
        Anthropic fallback tests, so they must not inherit a machine whose
        configured go-to is Codex and take the detached CLI-exec path."""
        def fake_run(_self, s):
            s.data["status"] = "done"
            s.data["finished"] = 1.0

        kwargs.setdefault("provider", "anthropic")
        with mock.patch.object(session.Sessions, "_run_subprocess", fake_run):
            jid = reg.launch("do the thing", cwd="/tmp", **kwargs)
        return jid

    def test_mode_derivation_from_legacy_permission_mode(self):
        reg = make_registry()
        with mock.patch.object(session, "SDK_AVAILABLE", False):
            jid = self._launch_stubbed(
                reg, permission_mode="bypassPermissions")
        self.assertEqual(reg.get(jid)["mode"], "bypassPermissions")
        self.assertEqual(reg.get(jid)["permission_mode"], "bypassPermissions")

    def test_explicit_mode_wins(self):
        reg = make_registry()
        with mock.patch.object(session, "SDK_AVAILABLE", False):
            jid = self._launch_stubbed(reg, mode="interactive")
        self.assertEqual(reg.get(jid)["mode"], "manual")

    def test_sdk_absent_falls_back_and_says_so(self):
        # The provider is PINNED, not inherited from this machine's config:
        # the legacy --print fallback is an ANTHROPIC path, and a missing SDK
        # only makes a session non-live for that provider. A CLI-exec engine
        # (codex) needs no SDK, so on an install whose go-to is Codex this
        # same launch is correctly live — which is what failed here once the
        # default provider started following `ai_provider`.
        reg = make_registry()
        ran = []
        with mock.patch.object(session, "SDK_AVAILABLE", False), \
             mock.patch.object(session.Sessions, "_run_subprocess",
                               lambda self, s: ran.append(s.data["id"])):
            jid = reg.launch("hello", mode="interactive", provider="anthropic")
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
        # provider pinned for the same reason as the fallback test above:
        # only the SDK-less ANTHROPIC path yields a non-live session.
        #
        # The legacy one-shot path has no control file AND records no model
        # session, so there is nothing to steer and nothing to continue. It
        # still refuses — but by NAMING why, rather than writing into a void.
        reg = make_registry()
        with mock.patch.object(session, "SDK_AVAILABLE", False), \
             mock.patch.object(session.Sessions, "_run_subprocess",
                               lambda self, s: None):
            jid = reg.launch("hello", provider="anthropic")
        with mock.patch.object(session.joblog, "get_record",
                               lambda j: {"id": j, "session_id": "",
                                          "cwd": "/tmp"}):
            with self.assertRaises(ValueError) as caught:
                reg.say(jid, "steer this")
        self.assertIn("no conversation to continue", str(caught.exception))

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


class ResumableTests(unittest.TestCase):
    """One answer to "does the compose box still reach the model" — the live
    snapshot and the ledger replay both read it, so they cannot disagree."""

    def test_a_running_session_is_always_reachable(self):
        # steered through its control file; it needs no recorded session id
        self.assertTrue(session.resumable({"status": "running",
                                           "session_id": ""}))

    def test_a_failed_session_with_a_conversation_is_resumable(self):
        # the usage-limit case: it ended, nobody finished it, the model
        # session exists
        self.assertTrue(session.resumable({"status": "error",
                                           "session_id": "sess-1"}))

    def test_finishing_closes_the_door(self):
        self.assertFalse(session.resumable({"status": "done",
                                            "session_id": "sess-1",
                                            "finished_by_owner": True}))

    def test_no_conversation_is_not_resumable(self):
        self.assertFalse(session.resumable({"status": "error",
                                            "session_id": ""}))

    def test_empty_is_safe(self):
        self.assertFalse(session.resumable({}))
        self.assertFalse(session.resumable(None))


class ResumeTests(unittest.TestCase):
    """Typing into an ENDED session continues the same conversation.

    The compose box is the owner's only channel and it used to close with
    the process, so a usage limit — 21 of 450 ledger jobs on 2026-08-13 —
    ended the conversation for good while its transcript sat on disk.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []

    def _reg(self, row):
        """A registry whose launch is captured rather than run, over a ledger
        holding exactly `row`."""
        reg = make_registry()
        def fake_launch(_self, prompt, **kw):
            self.calls.append({"prompt": prompt, **kw})
            return "newjob123456"
        self.launch_patch = mock.patch.object(
            session.Sessions, "launch", fake_launch)
        self.ledger_patch = mock.patch.object(
            session.joblog, "get_record", lambda jid: row)
        return reg

    def _row(self, **over):
        row = {"id": "oldjob123456", "session_id": "sess-abc-123",
               "cwd": self.tmp.name, "model": "claude-opus-5",
               "provider": "anthropic", "mode": "bypassPermissions",
               "permission_mode": None, "read_only": False,
               "publish_plan": False, "idea_id": "idea_x",
               "meta": {"machine": True, "kind": "board-score"}}
        row.update(over)
        return row

    def test_say_on_an_ended_session_resumes_the_conversation(self):
        reg = self._reg(self._row())
        h = make_detached(reg, self.tmp.name, status="error")
        with self.launch_patch, self.ledger_patch:
            res = reg.say(h.id, "usage is back — carry on")
        self.assertTrue(res["resumed"])
        self.assertEqual(res["job"], "newjob123456")
        call = self.calls[0]
        # The recorded conversation is continued, not a fresh one started.
        self.assertEqual(call["resume_session"], "sess-abc-123")
        self.assertEqual(call["resumed_from"], h.id)
        # The owner's message IS the prompt.
        self.assertEqual(call["prompt"], "usage is back — carry on")
        # cwd carries the placement, so launch re-arms the branch guard from
        # the same worktree instead of minting a second one.
        self.assertEqual(call["cwd"], self.tmp.name)
        self.assertEqual(call["model"], "claude-opus-5")
        self.assertEqual(call["idea_id"], "idea_x")

    def test_a_resumed_machine_run_becomes_an_owner_session(self):
        # meta.machine keeps a run from parking at its turn end — right for a
        # dispatch nobody watches, wrong the moment the owner types into it.
        reg = self._reg(self._row())
        h = make_detached(reg, self.tmp.name, status="error")
        with self.launch_patch, self.ledger_patch:
            reg.say(h.id, "keep going")
        meta = self.calls[0]["meta"]
        self.assertNotIn("machine", meta)
        self.assertEqual(meta["resumed_from"], h.id)

    def test_a_live_session_is_steered_not_resumed(self):
        reg = self._reg(self._row())
        h = make_detached(reg, self.tmp.name, status="running")
        with self.launch_patch, self.ledger_patch:
            res = reg.say(h.id, "while you work")
        self.assertFalse(res["resumed"])
        self.assertEqual(res["job"], h.id)
        self.assertEqual(self.calls, [])          # nothing was launched
        ctl = (h.dir / "control.jsonl").read_text(encoding="utf-8")
        self.assertIn("while you work", ctl)

    def test_no_recorded_conversation_refuses_by_name(self):
        reg = self._reg(self._row(session_id=""))
        h = make_detached(reg, self.tmp.name, status="error")
        with self.launch_patch, self.ledger_patch:
            with self.assertRaises(ValueError) as caught:
                reg.say(h.id, "hello?")
        self.assertIn("no conversation to continue", str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_a_vanished_working_directory_refuses_by_name(self):
        # Its worktree was tidied away. Resuming elsewhere would drop the
        # session into an unrelated tree with the guard armed from THAT tree.
        gone = str(Path(self.tmp.name) / "no-such-worktree")
        reg = self._reg(self._row(cwd=gone))
        h = make_detached(reg, self.tmp.name, status="error")
        with self.launch_patch, self.ledger_patch:
            with self.assertRaises(ValueError) as caught:
                reg.say(h.id, "hello?")
        self.assertIn("gone", str(caught.exception))
        self.assertIn(gone, str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_passive_instances_refuse_to_resume(self):
        reg = self._reg(self._row())
        h = make_detached(reg, self.tmp.name, status="error")
        with self.launch_patch, self.ledger_patch, \
             mock.patch.dict(session.os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(ValueError) as caught:
                reg.say(h.id, "hello?")
        self.assertIn("passive", str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_resume_session_reaches_the_launch_data(self):
        """The JOIN, not the halves. The branch guard shipped dead for four
        days because launch() accepted a field the spawn path never carried;
        the structural test in test_branch_guard_wiring then proves anything
        in the launch data reaches job.json."""
        reg = make_registry()
        seen = {}

        def fake_spawn(_self, data):
            seen.update(data)
            return make_detached(reg, self.tmp.name, jid=data["id"])

        with mock.patch.object(session, "SDK_AVAILABLE", True), \
             mock.patch.object(session.Sessions, "_spawn_runner", fake_spawn), \
             mock.patch.object(session.joblog, "record_launch",
                               lambda job: None):
            reg.launch("continue please", cwd=self.tmp.name,
                       provider="anthropic",
                       resume_session="sess-abc-123",
                       resumed_from="oldjob123456")
        self.assertEqual(seen["resume_session"], "sess-abc-123")
        self.assertEqual(seen["resumed_from"], "oldjob123456")

    def test_an_ordinary_launch_carries_no_resume(self):
        reg = make_registry()
        seen = {}

        def fake_spawn(_self, data):
            seen.update(data)
            return make_detached(reg, self.tmp.name, jid=data["id"])

        with mock.patch.object(session, "SDK_AVAILABLE", True), \
             mock.patch.object(session.Sessions, "_spawn_runner", fake_spawn), \
             mock.patch.object(session.joblog, "record_launch",
                               lambda job: None):
            reg.launch("fresh work", cwd=self.tmp.name, provider="anthropic")
        # Empty, never absent: the runner reads `spec.get("resume_session")`
        # and an empty string is what makes it start a NEW conversation.
        self.assertEqual(seen["resume_session"], "")


if __name__ == "__main__":
    unittest.main()
