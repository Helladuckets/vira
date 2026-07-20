"""Detached-runner tests: the permission gate's decision logic (now living
in server/runner.py) and the control.jsonl protocol (say / permission /
interrupt / close). No real ClaudeSDKClient is ever connected — the gate
and control handlers are exercised directly against a temp job dir.

Run: .venv/bin/python -m unittest discover tests
"""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import jobfiles, joblog
from server import runner as runner_mod


def make_spec(**over):
    spec = {
        "id": "t" * 12, "prompt": "test", "cwd": "/tmp",
        "model": None, "model_resolved": "test-model",
        "permission_mode": None, "publish_plan": False, "idea_id": None,
        "mode": "interactive", "started": time.time(),
        "auto_allow": ["Read", "Grep", "Glob", "TodoWrite", "Task",
                       "NotebookRead", "WebSearch"],
        "permission_timeout": 600,
    }
    spec.update(over)
    return spec


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make_runner(self, **over):
        spec = make_spec(**over)
        jdir = Path(self.tmp.name) / spec["id"]
        jdir.mkdir(parents=True, exist_ok=True)
        (jdir / "job.json").write_text(json.dumps(spec))
        r = runner_mod.Runner(jdir)
        self.addCleanup(r.out.close)
        return r

    def run_gate(self, r, tool, tool_input, resolver=None):
        """Drive one gate decision on a fresh loop. `resolver(r)` (async)
        runs after the card is up."""
        async def scenario():
            task = asyncio.ensure_future(r.gate(tool, tool_input, None))
            await asyncio.sleep(0.01)
            if resolver:
                await resolver(r)
            return await task
        return asyncio.run(scenario())

    def output(self, r):
        return (r.dir / "output.log").read_text()

    def state(self, r):
        return json.loads((r.dir / "state.json").read_text())


class GateTests(RunnerCase):
    def test_auto_allow_read_only_tool(self):
        r = self.make_runner()
        res = self.run_gate(r, "Read", {"file_path": "/tmp/x"})
        self.assertEqual(res.behavior, "allow")
        self.assertEqual(r.state["pending"], [])   # no card was raised
        self.assertIsNone(r.state["awaiting"])

    def test_native_vira_tools_auto_allow(self):
        r = self.make_runner()
        res = self.run_gate(r, "mcp__vira__calendar", {"days": 3})
        self.assertEqual(res.behavior, "allow")
        self.assertEqual(r.state["pending"], [])

    def test_session_grant_auto_allows(self):
        r = self.make_runner()
        r.session_allow.add("Bash")
        res = self.run_gate(r, "Bash", {"command": "ls"})
        self.assertEqual(res.behavior, "allow")

    def test_approve_once_allows_but_grants_nothing(self):
        r = self.make_runner()

        async def approve(rr):
            (card,) = rr.state["pending"]
            self.assertEqual(rr.state["awaiting"], "permission")
            # the card is mirrored to disk while the gate blocks
            self.assertEqual(self.state(rr)["pending"][0]["req_id"],
                             card["req_id"])
            await rr.handle({"op": "permission", "req_id": card["req_id"],
                             "allow": True, "scope": "once"})

        res = self.run_gate(r, "Bash", {"command": "echo hi"}, approve)
        self.assertEqual(res.behavior, "allow")
        self.assertNotIn("Bash", r.session_allow)
        self.assertEqual(r.state["pending"], [])
        self.assertIsNone(r.state["awaiting"])
        self.assertIn("approved", self.output(r))

    def test_approve_for_session_adds_grant(self):
        r = self.make_runner()

        async def approve(rr):
            (card,) = rr.state["pending"]
            await rr.handle({"op": "permission", "req_id": card["req_id"],
                             "allow": True, "scope": "session"})

        res = self.run_gate(r, "Bash", {"command": "git status"}, approve)
        self.assertEqual(res.behavior, "allow")
        self.assertIn("Bash", r.session_allow)
        res2 = self.run_gate(r, "Bash", {"command": "git diff"})
        self.assertEqual(res2.behavior, "allow")
        self.assertEqual(r.state["pending"], [])

    def test_deny_with_reason_reaches_the_agent(self):
        r = self.make_runner()

        async def deny(rr):
            (card,) = rr.state["pending"]
            await rr.handle({"op": "permission", "req_id": card["req_id"],
                             "allow": False, "scope": "once",
                             "reason": "wrong file, use config instead"})

        res = self.run_gate(r, "Write",
                            {"file_path": "/tmp/x", "content": "y"}, deny)
        self.assertEqual(res.behavior, "deny")
        self.assertIn("wrong file, use config instead", res.message)
        self.assertIn("denied", self.output(r))

    def test_timeout_is_default_deny(self):
        r = self.make_runner(permission_timeout=0.05)
        res = self.run_gate(r, "Bash", {"command": "rm -rf /"})
        self.assertEqual(res.behavior, "deny")
        self.assertEqual(r.state["pending"], [])
        self.assertIsNone(r.state["awaiting"])
        self.assertIn("timed out", self.output(r))

    def test_plan_session_denies_writes_without_a_card(self):
        r = self.make_runner(publish_plan=True)
        res = self.run_gate(r, "Bash", {"command": "touch x"})
        self.assertEqual(res.behavior, "deny")
        self.assertIn("read-only", res.message)
        self.assertEqual(r.state["pending"], [])   # denied outright, no wait

    def test_plan_session_still_auto_allows_read_only(self):
        r = self.make_runner(publish_plan=True)
        res = self.run_gate(r, "Grep", {"pattern": "foo"})
        self.assertEqual(res.behavior, "allow")

    def test_read_only_strips_non_reads_from_auto_allow(self):
        # audit P1-4: the read-only denial outranks auto-allow — Task and
        # WebSearch sit in the default auto-allow set yet are not reads,
        # and update_module_map is the one true write on the native server.
        r = self.make_runner(read_only=True)
        for tool in ("Task", "WebSearch", "mcp__vira__update_module_map"):
            res = self.run_gate(r, tool, {})
            self.assertEqual(res.behavior, "deny", tool)
            self.assertEqual(r.state["pending"], [])  # no card, no wait

    def test_read_only_ignores_session_grants(self):
        # a grant minted before a mode flip (or a poisoned state file) must
        # not open a write path in a read-only session
        r = self.make_runner(read_only=True)
        r.session_allow.add("Bash")
        res = self.run_gate(r, "Bash", {"command": "ls"})
        self.assertEqual(res.behavior, "deny")

    def test_read_only_still_allows_native_reads(self):
        r = self.make_runner(read_only=True)
        res = self.run_gate(r, "mcp__vira__calendar", {"days": 3})
        self.assertEqual(res.behavior, "allow")


class ControlTests(RunnerCase):
    def drive(self, r, *cmds):
        async def scenario():
            for c in cmds:
                await r.handle(c)
        asyncio.run(scenario())

    def test_say_queues_and_echoes(self):
        r = self.make_runner()
        self.drive(r, {"op": "say", "text": "focus on the tests"})
        self.assertEqual(r.inbox.qsize(), 1)
        out = self.output(r)
        self.assertIn("[you] focus on the tests", out)
        self.assertIn("queued", out)

    def test_interrupt_sets_flag_and_denies_pending(self):
        r = self.make_runner()

        async def scenario():
            gate_task = asyncio.ensure_future(
                r.gate("Bash", {"command": "sleep 99"}, None))
            await asyncio.sleep(0.01)
            await r.handle({"op": "interrupt"})
            return await gate_task

        res = asyncio.run(scenario())
        self.assertTrue(r.interrupted)
        self.assertEqual(res.behavior, "deny")
        self.assertIn("interrupted by the owner", self.output(r))

    def test_close_drains_inbox(self):
        r = self.make_runner()
        self.drive(r,
                   {"op": "say", "text": "one"},
                   {"op": "say", "text": "two"},
                   {"op": "close"})
        self.assertTrue(r.closing)
        self.assertTrue(r.interrupted)
        self.assertEqual(r.inbox.qsize(), 0)
        self.assertIn("session closed by the owner", self.output(r))

    def test_control_file_round_trip(self):
        r = self.make_runner()
        jobfiles.append_control(r.dir, {"op": "say", "text": "hello"})
        jobfiles.append_control(r.dir, {"op": "interrupt"})
        consumed, cmds = jobfiles.read_control(r.dir, 0)
        self.assertEqual(consumed, 2)
        self.assertEqual([c["op"] for c in cmds], ["say", "interrupt"])
        # nothing new -> nothing re-consumed
        consumed2, cmds2 = jobfiles.read_control(r.dir, consumed)
        self.assertEqual((consumed2, cmds2), (2, []))

    def test_partial_trailing_line_is_left_for_next_poll(self):
        r = self.make_runner()
        jobfiles.append_control(r.dir, {"op": "say", "text": "whole"})
        with open(r.dir / "control.jsonl", "a") as fh:
            fh.write('{"op": "say", "te')      # mid-append torn line
        consumed, cmds = jobfiles.read_control(r.dir, 0)
        self.assertEqual(consumed, 1)
        self.assertEqual(len(cmds), 1)

    def test_session_id_recorded_on_init(self):
        store = Path(self.tmp.name) / "jobs-log.json"
        with mock.patch.object(joblog, "STORE", store):
            r = self.make_runner()
            joblog.record_launch({"id": r.spec["id"], "prompt": "test",
                                  "cwd": "/tmp", "mode": "interactive"})

            class FakeInit:
                subtype = "init"
                data = {"session_id": "sess-abc-123", "model": "test-model"}

            with mock.patch.object(runner_mod, "SystemMessage", FakeInit), \
                 mock.patch.object(runner_mod, "AssistantMessage", ()), \
                 mock.patch.object(runner_mod, "ResultMessage", ()):
                r.render_message(FakeInit())
        self.assertEqual(r.state["session_id"], "sess-abc-123")
        self.assertEqual(self.state(r)["session_id"], "sess-abc-123")
        rec = json.loads(store.read_text())["jobs"][0]
        self.assertEqual(rec["session_id"], "sess-abc-123")
        self.assertIn("sess-abc-123.jsonl", rec["transcript"])


if __name__ == "__main__":
    unittest.main()
