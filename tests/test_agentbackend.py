"""The provider-agnostic session backend: routing, sandbox mapping, the
codex argv contract, a full best-effort run against a FAKE codex binary
that speaks the real JSONL event protocol (shapes verified live
2026-07-28), and the ledger round-trip that keeps a FINISHED session
honest about which engine answered it.
"""
import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import agentbackend, jobfiles, joblog, models, session


class RoutingTest(unittest.TestCase):
    def test_provider_of_model(self):
        for m, want in (("gpt-5.1-codex", "openai"), ("o3", "openai"),
                        ("codex-mini", "openai"), ("gemini-2.5-pro", "google"),
                        ("grok-4", "xai"), ("sonnet", "anthropic"),
                        ("claude-fable-5", "anthropic"),
                        ("fable", "anthropic"), ("", ""),
                        ("mystery-9", "")):
            self.assertEqual(agentbackend.provider_of_model(m), want, m)

    def test_session_provider_precedence(self):
        # explicit wins over the model, model over the default
        self.assertEqual(
            agentbackend.session_provider(model="gpt-5.1",
                                          provider="anthropic"), "anthropic")
        self.assertEqual(agentbackend.session_provider(model="gpt-5.1"),
                         "openai")
        self.assertEqual(agentbackend.session_provider(), "anthropic")
        self.assertEqual(agentbackend.session_provider(model="mystery"),
                         "anthropic")

    def test_launch_refuses_a_sessionless_provider(self):
        with self.assertRaises(ValueError) as ctx:
            session.Sessions().launch("p", model="gemini-2.5-pro")
        self.assertIn("cannot host live agent sessions", str(ctx.exception))

    def test_launch_stamps_provider_on_the_spec(self):
        reg = session.Sessions()
        seen = {}

        def fake_spawn(data):
            seen.update(data)
            h = mock.Mock()
            h.kind = "detached"
            h.working.return_value = False
            return h

        with mock.patch.object(session, "SDK_AVAILABLE", True), \
             mock.patch.object(reg, "_spawn_runner", fake_spawn):
            reg.launch("do it", model="gpt-5.1-codex", mode="autopilot")
        self.assertEqual(seen["provider"], "openai")
        self.assertEqual(seen["model"], "gpt-5.1-codex")


class SandboxTest(unittest.TestCase):
    def test_ladder_maps_onto_codex_sandboxes(self):
        self.assertEqual(agentbackend.sandbox_for(
            {"read_only": True, "mode": "interactive"}), "read-only")
        self.assertEqual(agentbackend.sandbox_for(
            {"publish_plan": True, "mode": "autopilot"}), "read-only")
        self.assertEqual(agentbackend.sandbox_for(
            {"mode": "autopilot"}), "danger-full-access")
        self.assertEqual(agentbackend.sandbox_for(
            {"mode": "interactive"}), "workspace-write")
        self.assertEqual(agentbackend.sandbox_for(
            {"mode": "acceptedits"}), "workspace-write")


class ArgvTest(unittest.TestCase):
    def _argv(self, spec, resume=None):
        return agentbackend._codex_argv("/x/codex", spec, resume, "hi")

    def test_first_turn_carries_the_sandbox(self):
        argv = self._argv({"mode": "interactive", "model_resolved": "gpt-5.1"})
        self.assertEqual(argv[:2], ["/x/codex", "exec"])
        self.assertIn("--sandbox", argv)
        self.assertIn("workspace-write", argv)
        self.assertIn("--json", argv)
        self.assertEqual(argv[-1], "hi")

    def test_resume_inherits_the_sandbox(self):
        # `codex exec resume` does not accept --sandbox — the session keeps
        # the one it started with. Passing it would fail the whole turn.
        argv = self._argv({"mode": "interactive"}, resume="tid-1")
        self.assertEqual(argv[1:4], ["exec", "resume", "tid-1"])
        self.assertNotIn("--sandbox", argv)

    def test_autopilot_bypass_rides_every_turn(self):
        for resume in (None, "tid-1"):
            argv = self._argv({"mode": "autopilot"}, resume=resume)
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
            self.assertNotIn("--sandbox", argv)


class RenderTest(unittest.TestCase):
    def test_item_shapes(self):
        r = agentbackend._render_item
        self.assertEqual(r({"type": "agent_message", "text": "hi"}), "hi\n")
        self.assertIsNone(r({"type": "reasoning", "text": "hmm"}))
        self.assertIn("Bash: ls", r({"type": "command_execution",
                                     "command": "ls"}))
        self.assertIn("(exit 2)", r({"type": "command_execution",
                                     "command": "ls", "exit_code": 2}))
        self.assertIn("a.py", r({"type": "file_change",
                                 "changes": [{"path": "a.py"}]}))
        self.assertIn("novel_item", r({"type": "novel_item"}))


class _FakeRunner:
    """The slice of runner.Runner that run_cliexec touches."""

    END = object()

    def __init__(self, spec, replies=None):
        self.spec = spec
        self.state = {"session_id": "", "pending": []}
        self.out = []
        self.inbox = asyncio.Queue()
        self.exec_proc = None
        self.closing = False
        self.interrupted = False
        self.finished_cleanly = False
        self._replies = list(replies or [])

    def append(self, piece):
        self.out.append(piece)

    def flush_state(self):
        pass

    def parks_at_turn_end(self):
        return True

    async def await_reply(self):
        if self._replies:
            return self._replies.pop(0)
        return None


def _fake_codex(tmp, lines_per_call):
    """A stub binary that prints one canned JSONL set per invocation (a
    counter file picks the set), echoing nothing else.

    The payload is a plain .py file and the "binary" is a one-line shim that
    hands it to this interpreter, because a shebang is a POSIX mechanism:
    Windows CreateProcess reads the extension, not the first line, so an
    executable-bit text file raises WinError 193. That is a fact about the
    test harness, not about run_cliexec — the codex path is shipped code a
    Windows install can exercise, so it stays under test on both platforms
    rather than being skipped off-Mac."""
    marker = Path(tmp) / "calls"
    marker.write_text("0", encoding="utf-8")
    impl = Path(tmp) / "codex_impl.py"
    sets = json.dumps(lines_per_call)
    impl.write_text(encoding="utf-8", data=
        "import json, sys\n"
        f"marker = {str(marker)!r}\n"
        f"sets = json.loads({sets!r})\n"
        "n = int(open(marker).read())\n"
        "open(marker, 'w').write(str(n + 1))\n"
        "open(marker + '.argv%d' % n, 'w').write(json.dumps(sys.argv[1:]))\n"
        "for ev in sets[min(n, len(sets) - 1)]:\n"
        "    print(json.dumps(ev))\n")
    if os.name == "nt":
        # cmd forwards the tail of its own command line verbatim through %*,
        # so a quoted prompt arrives as one argv rather than being resplit.
        script = Path(tmp) / "codex.cmd"
        script.write_text(encoding="utf-8", data=
            "@echo off\r\n"
            f'"{sys.executable}" "{impl}" %*\r\n')
    else:
        script = Path(tmp) / "codex"
        script.write_text(encoding="utf-8", data=
            "#!/bin/sh\n"
            f'exec "{sys.executable}" "{impl}" "$@"\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


class CliExecRunTest(unittest.TestCase):
    def _run(self, runner, binary):
        with mock.patch.object(models, "find_binary",
                               return_value=str(binary)):
            return asyncio.run(agentbackend.run_cliexec(runner))

    def test_full_turn_captures_thread_and_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = _fake_codex(tmp, [[
                {"type": "thread.started", "thread_id": "tid-abc"},
                {"type": "turn.started"},
                {"type": "item.completed",
                 "item": {"type": "agent_message", "text": "all done"}},
                {"type": "turn.completed", "usage": {}},
            ]])
            spec = {"id": "j1", "provider": "openai", "cwd": tmp,
                    "mode": "autopilot", "prompt": "do the thing",
                    "model_resolved": "gpt-5.1-codex"}
            runner = _FakeRunner(spec)
            with mock.patch("server.joblog.record_session") as rec:
                text, ok = self._run(runner, binary)
        self.assertTrue(ok)
        self.assertEqual(text, "all done")
        self.assertEqual(runner.state["session_id"], "tid-abc")
        rec.assert_called_once_with("j1", "tid-abc")
        joined = "".join(runner.out)
        self.assertIn("best-effort", joined)
        self.assertIn("all done", joined)

    def test_reply_resumes_the_same_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = _fake_codex(tmp, [
                [{"type": "thread.started", "thread_id": "tid-1"},
                 {"type": "item.completed",
                  "item": {"type": "agent_message", "text": "first"}},
                 {"type": "turn.completed"}],
                [{"type": "item.completed",
                  "item": {"type": "agent_message", "text": "second"}},
                 {"type": "turn.completed"}],
            ])
            spec = {"id": "j2", "provider": "openai", "cwd": tmp,
                    "mode": "interactive", "prompt": "start"}
            runner = _FakeRunner(spec, replies=["keep going"])
            with mock.patch("server.joblog.record_session"):
                text, ok = self._run(runner, binary)
            argv2 = json.loads(Path(tmp, "calls.argv1").read_text(encoding="utf-8"))
        self.assertTrue(ok)
        self.assertEqual(text, "second")
        self.assertEqual(argv2[:2], ["exec", "resume"])
        self.assertEqual(argv2[2], "tid-1")
        self.assertNotIn("--sandbox", argv2)
        # A resumed thread already holds its context — the preamble rides
        # only the first turn, never a resume.
        self.assertNotIn("running inside Vira", argv2[-1])

    def test_lost_thread_reply_recarries_the_preamble(self):
        # No thread.started ever arrives, so the reply cannot resume — it
        # starts a FRESH conversation. Without the preamble re-prepended
        # that turn runs with no Vira context at all, only whatever the
        # provider CLI auto-loaded from cwd.
        with tempfile.TemporaryDirectory() as tmp:
            binary = _fake_codex(tmp, [
                [{"type": "item.completed",
                  "item": {"type": "agent_message", "text": "first"}},
                 {"type": "turn.completed"}],
                [{"type": "item.completed",
                  "item": {"type": "agent_message", "text": "second"}},
                 {"type": "turn.completed"}],
            ])
            spec = {"id": "j5", "provider": "openai", "cwd": tmp,
                    "mode": "interactive", "prompt": "start"}
            runner = _FakeRunner(spec, replies=["more"])
            with mock.patch("server.joblog.record_session"):
                text, ok = self._run(runner, binary)
            argv1 = json.loads(
                Path(tmp, "calls.argv0").read_text(encoding="utf-8"))
            argv2 = json.loads(
                Path(tmp, "calls.argv1").read_text(encoding="utf-8"))
        self.assertTrue(ok)
        self.assertEqual(text, "second")
        self.assertNotEqual(argv2[:2], ["exec", "resume"])
        self.assertIn("running inside Vira", argv1[-1])
        self.assertIn("running inside Vira", argv2[-1])
        self.assertIn("more", argv2[-1])

    def test_failed_turn_reports_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = _fake_codex(tmp, [[
                {"type": "thread.started", "thread_id": "t"},
                {"type": "turn.failed",
                 "error": {"message": "usage limit reached"}},
            ]])
            spec = {"id": "j3", "provider": "openai", "cwd": tmp,
                    "mode": "interactive", "prompt": "x"}
            runner = _FakeRunner(spec)
            with mock.patch("server.joblog.record_session"):
                text, ok = self._run(runner, binary)
        self.assertFalse(ok)
        self.assertIn("usage limit reached", "".join(runner.out))

    def test_missing_binary_fails_honestly(self):
        spec = {"id": "j4", "provider": "openai", "cwd": "/tmp",
                "mode": "interactive", "prompt": "x"}
        runner = _FakeRunner(spec)
        with mock.patch.object(models, "find_binary", return_value=""):
            text, ok = asyncio.run(agentbackend.run_cliexec(runner))
        self.assertFalse(ok)
        self.assertIn("not found", "".join(runner.out))


class LedgerReplayTest(unittest.TestCase):
    """Which engine answered must survive the trip out of the live registry.

    A running job carries `provider` on its live snapshot; a finished one is
    replayed from the ledger. On 2026-07-28 the ledger row did not persist
    the field, so a done OpenAI session that had reported "best-effort —
    sandboxed, no cards" while live re-read as "interactive (gated)" about
    forty minutes later, with no code change in between — the terminal
    banner claiming the containment was the opposite of what actually ran.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "jobs-log.json"
        for p in (mock.patch.object(jobfiles, "JOBS_DIR",
                                    Path(self.tmp.name) / "jobs"),
                  mock.patch.object(joblog, "STORE", self.store)):
            p.start()
            self.addCleanup(p.stop)

    def _replay(self, jid):
        """The snapshot /api/jobs/{id} serves once a job is off the registry."""
        from server.main import _job_from_disk
        return _job_from_disk(jid)

    def test_finished_openai_job_replays_as_best_effort(self):
        joblog.record_launch({"id": "oai000000001", "cwd": "/tmp",
                              "prompt": "Reply naming your model.",
                              "mode": "interactive", "model": "gpt-5.6-sol",
                              "provider": "openai"})
        joblog.record_finish("oai000000001", "done", "I'm Codex.")
        # persisted on the row itself, not just the live spec
        self.assertEqual(joblog.get_record("oai000000001")["provider"],
                         "openai")
        snap = self._replay("oai000000001")
        self.assertFalse(snap["live"])            # off the live registry
        self.assertEqual(snap["provider"], "openai")
        self.assertEqual(agentbackend.sessions_quality(snap["provider"]),
                         "best_effort")

    def test_legacy_row_without_provider_falls_back_to_the_model(self):
        # Rows written BEFORE the ledger persisted the field — exactly the
        # shape job 91d5895ed914 had on disk. The model id still names the
        # engine, so the replay must not regrade the session as gated.
        joblog.record_launch({"id": "old000000001", "cwd": "/tmp",
                              "prompt": "p", "mode": "interactive",
                              "model": "gpt-5.6-sol", "provider": "openai"})
        raw = json.loads(self.store.read_text(encoding="utf-8"))
        raw["jobs"][0].pop("provider", None)   # isolate the replay fallback
        self.store.write_text(json.dumps(raw), encoding="utf-8")
        snap = self._replay("old000000001")
        self.assertEqual(snap["provider"], "openai")
        self.assertEqual(agentbackend.sessions_quality(snap["provider"]),
                         "best_effort")

    def test_anthropic_job_stays_gated(self):
        # The other half of honesty: the fallback must not flip the gated
        # default to best-effort for a session that really was gated.
        joblog.record_launch({"id": "ant000000001", "cwd": "/tmp",
                              "prompt": "p", "mode": "interactive",
                              "model": "claude-opus-5",
                              "provider": "anthropic"})
        snap = self._replay("ant000000001")
        self.assertEqual(snap["provider"], "anthropic")
        self.assertEqual(agentbackend.sessions_quality(snap["provider"]),
                         "gated")

    def test_unnamed_model_replays_as_the_gated_default(self):
        # A launch that named no model at all still has to answer the
        # question — "" would fall through the banner's ladder the same way
        # a missing key did.
        joblog.record_launch({"id": "bare00000001", "cwd": "/tmp",
                              "prompt": "p", "mode": "interactive"})
        raw = json.loads(self.store.read_text(encoding="utf-8"))
        raw["jobs"][0].pop("provider", None)   # isolate the replay fallback
        self.store.write_text(json.dumps(raw), encoding="utf-8")
        snap = self._replay("bare00000001")
        self.assertEqual(snap["provider"], "anthropic")


if __name__ == "__main__":
    unittest.main()
