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

from server import joblog, viratools
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
