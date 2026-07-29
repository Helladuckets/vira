"""What the owner reads at the end of a turn, and which model actually ran.

Two defects from 2026-07-29, both the same shape as the branch-guard drop:
a field the UI reads that nothing writes, and harness chrome standing in
for content that was never specified.

  - `[vira] turn complete — reply to keep going, or Finish to close the
    session` was appended after the agent's last words, at the exact spot
    the owner reads for the conclusion. It restated the compose bar's own
    placeholder and button labels. Removed; the close-out is now the
    agent's job, specified in the preamble.
  - `model_used` was read in three places in app.js and written nowhere,
    so every surface fell back to the requested alias. On CLI 2.1.207
    `opus` resolves to claude-opus-4-8 while claude-opus-5 answers fine,
    so the banner said "Opus" over a previous-generation session.

Run: .venv/bin/python -m unittest discover tests
"""
import asyncio
import inspect
import json
import tempfile
import time
import unittest
from pathlib import Path

from unittest import mock

from server import joblog, session, viratools
from server import runner as runner_mod


def make_runner(tmp, **over):
    """A real Runner over a hand-built job dir. The spec shape does not
    matter here — this file is about the transcript, not the launch seam
    (tests/test_branch_guard_wiring.py owns that)."""
    jdir = Path(tmp) / "job"
    jdir.mkdir(parents=True, exist_ok=True)
    spec = {"id": "j" * 12, "prompt": "x", "cwd": str(tmp), "model": None,
            "model_resolved": "m", "permission_mode": None,
            "publish_plan": False, "idea_id": None, "mode": "manual",
            "started": time.time(), "auto_allow": [],
            "permission_timeout": 600, "reply_window": 30}
    spec.update(over)
    (jdir / "job.json").write_text(json.dumps(spec), encoding="utf-8")
    (jdir / "control.jsonl").touch()
    return runner_mod.Runner(jdir)


class NoHarnessChromeAtTheTurnBoundary(unittest.TestCase):
    """Asserted against what actually reaches output.log — the earlier
    version of this grepped the source and matched the comment explaining
    the removal, which is the tidiest possible way to test nothing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_parking_appends_nothing(self):
        r = make_runner(self.tmp.name)
        self.addCleanup(r.out.close)
        r.inbox.put_nowait("carry on")
        before = (Path(r.dir) / "output.log").read_text(encoding="utf-8")
        got = asyncio.run(r.await_reply())
        after = (Path(r.dir) / "output.log").read_text(encoding="utf-8")
        self.assertEqual(got, "carry on")
        self.assertEqual(after, before,
                         "the agent's last words must be the last words")

    def test_parking_still_flips_the_bar_live(self):
        """Removing the line must not remove the state that keeps the
        compose bar open — that state is the whole feature."""
        r = make_runner(self.tmp.name)
        self.addCleanup(r.out.close)
        seen = []
        real = r.flush_state
        r.flush_state = lambda: (seen.append(r.state.get("awaiting")), real())
        r.inbox.put_nowait("hi")
        asyncio.run(r.await_reply())
        self.assertIn("reply", seen)

    def test_the_timeout_line_survives(self):
        """Closing after the window IS new information — the session is
        gone — so that one line stays."""
        r = make_runner(self.tmp.name, reply_window=0.01)
        self.addCleanup(r.out.close)
        self.assertIsNone(asyncio.run(r.await_reply()))
        out = (Path(r.dir) / "output.log").read_text(encoding="utf-8")
        self.assertIn("closing the session", out.replace("\n", " "))


class PreambleSpecifiesTheCloseOut(unittest.TestCase):
    def test_it_says_how_to_end_a_turn(self):
        p = viratools.preamble()
        self.assertIn("HOW TO END A TURN", p)

    def test_it_names_the_three_shapes(self):
        p = viratools.preamble().lower()
        for cue in ("accomplished", "one question", "work queue"):
            self.assertIn(cue, p)

    def test_it_forbids_restating_the_interface(self):
        p = viratools.preamble().lower()
        self.assertIn("let me know if you need anything else", p)

    def test_the_fallback_preamble_carries_it_too(self):
        """native=False is the legacy --print path; the owner reads the
        same terminal either way."""
        self.assertIn("HOW TO END A TURN", viratools.preamble(native=False))


class ModelUsedIsRecorded(unittest.TestCase):
    def test_joblog_has_the_setter_the_runner_calls(self):
        self.assertTrue(callable(joblog.record_model_used))

    def test_the_runner_records_the_resolved_model(self):
        src = inspect.getsource(runner_mod.Runner.render_message)
        self.assertIn("record_model_used", src)
        self.assertIn("model_used", src)

    def test_the_setter_writes_only_when_it_changes(self):
        rows = {"jobs": [{"id": "j1", "model_used": "claude-opus-5"}]}
        captured = []

        def fake_mutate(fn):
            captured.append(fn(rows))

        real = joblog._mutate
        joblog._mutate = fake_mutate
        try:
            joblog.record_model_used("j1", "claude-opus-5")
            self.assertFalse(captured[-1], "no write when unchanged")
            joblog.record_model_used("j1", "claude-opus-4-8")
            self.assertTrue(captured[-1], "writes when it changes")
            self.assertEqual(rows["jobs"][0]["model_used"], "claude-opus-4-8")
            joblog.record_model_used("j1", "")
            self.assertFalse(captured[-1], "blank is not a resolution")
            joblog.record_model_used("nope", "x")
            self.assertFalse(captured[-1], "unknown job is a no-op")
        finally:
            joblog._mutate = real


if __name__ == "__main__":
    unittest.main()


class AliasOverrides(unittest.TestCase):
    """An alias is supposed to name a TIER and track the newest generation
    of it. On Claude Code 2.1.207 `opus` resolves to claude-opus-4-8 while
    claude-opus-5 answers, so every launch ran a generation behind whatever
    the picker displayed. The override is owner DATA, not a shipped table —
    session.MODEL_ALIASES must stay empty."""

    def _with(self, mapping):
        return mock.patch.object(
            session, "config", lambda: {session.ALIAS_OVERRIDE_KEY: mapping})

    def test_shipped_table_is_still_empty(self):
        self.assertEqual(session.MODEL_ALIASES, {})

    def test_override_wins_over_the_bare_alias(self):
        with self._with({"opus": "claude-opus-5"}):
            self.assertEqual(session.resolve_model("opus"), "claude-opus-5")

    def test_override_is_case_insensitive_on_the_key(self):
        with self._with({"opus": "claude-opus-5"}):
            self.assertEqual(session.resolve_model("  Opus "), "claude-opus-5")

    def test_unlisted_aliases_pass_through_untouched(self):
        with self._with({"opus": "claude-opus-5"}):
            self.assertEqual(session.resolve_model("sonnet"), "sonnet")
            self.assertEqual(session.resolve_model("claude-opus-5"),
                             "claude-opus-5")

    def test_no_overrides_is_the_old_behaviour(self):
        for bad in ({}, None, "nonsense", []):
            with mock.patch.object(
                    session, "config",
                    lambda b=bad: {session.ALIAS_OVERRIDE_KEY: b}):
                self.assertEqual(session.resolve_model("opus"), "opus")
                self.assertIsNone(session.resolve_model(""))

    def test_a_blank_override_does_not_erase_the_model(self):
        with self._with({"opus": "   "}):
            self.assertEqual(session.resolve_model("opus"), "opus")


class SteerMessageMatchesTheState(unittest.TestCase):
    """"queued — delivers at the next turn boundary" immediately followed by
    "reply delivered" told the owner two contradictory things in consecutive
    lines (2026-07-29). A parked session is sitting on the inbox; the wait
    only exists mid-turn."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _say(self, parked):
        r = make_runner(self.tmp.name)
        self.addCleanup(r.out.close)
        r.awaiting_reply = parked
        asyncio.run(r.handle({"op": "say", "text": "discard"}))
        return (Path(r.dir) / "output.log").read_text(encoding="utf-8")

    def test_parked_does_not_claim_a_wait(self):
        out = self._say(True)
        self.assertIn("[you] discard", out)
        self.assertNotIn("queued", out)

    def test_mid_turn_still_says_queued(self):
        out = self._say(False)
        self.assertIn("[you] discard", out)
        self.assertIn("queued", out)


class MidTurnSteering(unittest.TestCase):
    """Steering used to reach the model only at the turn boundary, and a
    turn is however many tool calls the agent chooses to make. The owner
    typed "Okay, wrap it up" and watched twenty more calls go by
    (2026-07-29) — indistinguishable from steering being broken. The
    PostToolUse hook's additionalContext is the SDK's channel for handing
    the model text alongside the tool result it is already reading."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.r = make_runner(self.tmp.name)
        self.addCleanup(self.r.out.close)

    def _fire(self):
        return asyncio.run(self.r.steer_hook({}, "tu_1", None))

    def test_nothing_queued_injects_nothing(self):
        self.assertEqual(self._fire(), {})

    def test_a_queued_message_is_handed_over_as_context(self):
        self.r.inbox.put_nowait("Okay, wrap it up.")
        out = self._fire()["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUse")
        self.assertIn("Okay, wrap it up.", out["additionalContext"])
        self.assertIn("mid-turn", out["additionalContext"])

    def test_it_drains_everything_queued(self):
        self.r.inbox.put_nowait("one")
        self.r.inbox.put_nowait("two")
        ctx = self._fire()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("one", ctx)
        self.assertIn("two", ctx)
        self.assertEqual(self._fire(), {}, "second fire has nothing left")

    def test_it_shows_in_the_transcript(self):
        self.r.inbox.put_nowait("stop after this file")
        self._fire()
        out = (Path(self.r.dir) / "output.log").read_text(encoding="utf-8")
        self.assertIn("steering delivered — stop after this file", out)

    def test_finish_is_never_consumed_by_the_hook(self):
        """_END means the owner pressed Finish. Only await_reply may act on
        it; swallowing it here would silently ignore a close."""
        self.r.inbox.put_nowait(runner_mod._END)
        self.assertEqual(self._fire(), {})
        self.assertIs(self.r.inbox.get_nowait(), runner_mod._END)

    def test_finish_queued_behind_a_message_survives(self):
        self.r.inbox.put_nowait("keep going")
        self.r.inbox.put_nowait(runner_mod._END)
        ctx = self._fire()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("keep going", ctx)
        self.assertIs(self.r.inbox.get_nowait(), runner_mod._END)

    def test_a_broken_hook_can_never_kill_the_session(self):
        self.r.inbox = None              # any failure at all
        self.assertEqual(self._fire(), {})


class StopIsAPauseNotAnEnding(unittest.TestCase):
    """The owner pressed Stop; the agent received the queued steer, acted on
    it, wrapped up cleanly — and the session closed anyway, because
    `interrupted` short-circuited await_reply before it could park. Stopping
    something mid-work is exactly when you have something to say to it, and
    it was the one moment Vira took the input box away (2026-07-29)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _runner(self, **kw):
        r = make_runner(self.tmp.name, **kw)
        self.addCleanup(r.out.close)
        return r

    def test_stop_parks_instead_of_ending(self):
        r = self._runner()
        r.interrupted = True
        r.inbox.put_nowait("do it this way instead")
        self.assertEqual(asyncio.run(r.await_reply()), "do it this way instead")

    def test_finish_still_ends_it(self):
        r = self._runner()
        r.closing = True
        self.assertIsNone(asyncio.run(r.await_reply()))

    def test_a_cut_short_turn_is_not_finished_cleanly(self):
        """finished_cleanly gates the epilogue — publishing a plan, closing
        out the idea. Parking after a Stop must not smuggle an interrupted
        run into that path."""
        r = self._runner()
        r.interrupted = True
        r.inbox.put_nowait("carry on")
        asyncio.run(r.await_reply())
        self.assertFalse(r.finished_cleanly)

    def test_an_uninterrupted_park_is_finished_cleanly(self):
        r = self._runner()
        r.inbox.put_nowait("carry on")
        asyncio.run(r.await_reply())
        self.assertTrue(r.finished_cleanly)

    def test_the_state_separates_paused_from_complete(self):
        for interrupted, want in ((True, "paused"), (False, "reply")):
            r = self._runner()
            r.interrupted = interrupted
            seen = []
            real = r.flush_state
            r.flush_state = lambda rr=r: (seen.append(rr.state.get("awaiting")),
                                          real())
            r.inbox.put_nowait("x")
            asyncio.run(r.await_reply())
            self.assertIn(want, seen)

    def test_a_paused_session_does_not_block_the_live_cap(self):
        """DetachedJob.working() excludes a parked session so a handful of
        them cannot wedge the cockpit shut — paused must count the same."""
        h = session.DetachedJob("j" * 12, "/nonexistent", {"mode": "manual"})
        for aw, want in (("reply", False), ("paused", False),
                         (None, True), ("permission", True)):
            h.last_state = {"status": "running", "awaiting": aw}
            self.assertEqual(h.working(), want, f"awaiting={aw}")
