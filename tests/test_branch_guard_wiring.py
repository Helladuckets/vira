"""The join between placement and enforcement — the test that was missing.

WHY THIS FILE EXISTS. On 2026-07-25 the branch-first harness shipped four
coordinated pieces: placement in Sessions.launch, the backstop in
runner.gate, the branch paragraphs in viratools.preamble, and a test suite.
One of the four worked. `_spawn_runner` rebuilt the launch dict as a
hand-typed literal that never named `worktree`, `branch`, `live_root` or
`branch_note`, so those fields never reached job.json — and the runner reads
nothing but job.json. The guard could not fire, the model was never told it
was in a worktree, and no transcript ever recorded which tree it edited.
Diagnosed 2026-07-29, four days and ~30 dispatches later.

The suite was green throughout, for two reasons this file exists to remove:

  - tests/test_session.py is the only place that calls the real launch(),
    and it patches out `_spawn_runner` — the one function with the defect.
  - tests/test_runner.py does test the backstop, but hand-builds a spec
    containing the very keys production omits, so it exercises a shape
    production has never produced.

So every assertion here reads the FILE, with the real `_spawn_runner`
running. Only subprocess.Popen is stubbed, because starting an actual
detached claude process is the one thing a unit test must not do.

Run: .venv/bin/python -m unittest discover tests
"""
import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import joblog, session, viratools, worktree
from server import runner as runner_mod


def runner_only_popen():
    """A Popen stand-in that refuses ONLY the detached runner spawn and lets
    every other subprocess through. Patching Popen wholesale also breaks the
    git calls placement depends on — and those must run for real, since the
    production path is the thing under test."""
    real = subprocess.Popen

    def popen(args, *a, **kw):
        if (isinstance(args, (list, tuple)) and len(args) > 2
                and list(args[1:3]) == ["-m", "server.runner"]):
            return mock.Mock(pid=424242)
        return real(args, *a, **kw)
    return popen


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True)


def make_branch_first_repo(root):
    """A real git repo that declares the workflow the way worktree.py reads
    it — by shipping scripts/branch.sh. The script is the genuine article
    from this checkout, so `ensure()` provisions exactly as it does live."""
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "scripts").mkdir()
    real = Path(__file__).resolve().parent.parent / "scripts" / "branch.sh"
    (root / "scripts" / "branch.sh").write_text(
        real.read_text(encoding="utf-8"), encoding="utf-8")
    (root / "scripts" / "branch.sh").chmod(0o755)
    (root / "server").mkdir()
    (root / "server" / "main.py").write_text("# live\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root


@unittest.skipUnless(os.name == "posix",
                     "branch.sh is POSIX-only dev tooling")
class SpecReachesDisk(unittest.TestCase):
    """Call the real launch() against a real branch-first repo, let the real
    _spawn_runner write the job dir, then read job.json back off disk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = make_branch_first_repo(Path(self.tmp.name) / "repo")
        self.jobs = Path(self.tmp.name) / "jobs"
        self.jobs.mkdir()
        # Job dirs land in our tmp, and the ONE process we refuse to start is
        # the detached runner. Everything else — every git call placement
        # makes, including branch.sh itself — runs for real, because the
        # whole point is to exercise the production path.
        mock.patch.object(session.subprocess, "Popen",
                          runner_only_popen()).start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(session.jobfiles, "job_dir",
                          lambda jid: self.jobs / jid).start()
        mock.patch.object(session.joblog, "record_launch",
                          lambda job: self.rows.append(job)).start()
        mock.patch.object(session, "SDK_AVAILABLE", True).start()
        self.rows = []

    def launch(self, **kw):
        reg = session.Sessions()
        jid = reg.launch("Add a thing to the widget", cwd=str(self.root), **kw)
        return jid, json.loads(
            (self.jobs / jid / "job.json").read_text(encoding="utf-8"))

    def test_placement_fields_reach_job_json_present_and_non_empty(self):
        """THE regression test. A key that is present but empty disarms the
        gate exactly as a missing one does (`if wt and live_root`), so
        truthiness is asserted, not just presence."""
        _, spec = self.launch()
        for key in ("worktree", "branch", "live_root", "branch_note"):
            self.assertIn(key, spec, f"{key} never reached job.json")
            self.assertTrue(spec[key], f"{key} reached job.json empty")

    def test_cwd_is_the_worktree_and_live_root_is_the_repo(self):
        _, spec = self.launch()
        self.assertEqual(Path(spec["cwd"]).resolve(),
                         Path(spec["worktree"]).resolve())
        self.assertEqual(Path(spec["live_root"]).resolve(),
                         self.root.resolve())
        self.assertTrue(worktree.is_worktree(spec["cwd"]))
        self.assertTrue(spec["branch"].startswith("claude/"))

    def test_the_gate_is_actually_armed_by_what_landed(self):
        """Feed the real job.json to a real Runner and drive one gate call.
        This closes the loop test_runner.py short-circuits by hand."""
        jid, spec = self.launch(mode="bypassPermissions")
        jdir = self.jobs / jid
        (jdir / "control.jsonl").touch()
        r = runner_mod.Runner(jdir)
        self.addCleanup(r.out.close)
        self.assertFalse(r.disarmed, r.disarmed)

        deny = asyncio.run(r.gate("Write", {
            "file_path": str(self.root / "server" / "main.py")}, None))
        self.assertEqual(type(deny).__name__, "PermissionResultDeny")

        allow = asyncio.run(r.gate("Write", {
            "file_path": str(Path(spec["worktree"]) / "server" / "main.py")},
            None))
        self.assertEqual(type(allow).__name__, "PermissionResultAllow")

    def test_bypass_still_denies_the_live_tree(self):
        """The rung the owner runs by default is the rung that used to have
        no gate at all (can_use_tool=None)."""
        jid, _ = self.launch(mode="bypassPermissions")
        (self.jobs / jid / "control.jsonl").touch()
        r = runner_mod.Runner(self.jobs / jid)
        self.addCleanup(r.out.close)
        out = asyncio.run(r.gate("Bash", {
            "command": f"echo x > {self.root}/server/main.py"}, None))
        self.assertEqual(type(out).__name__, "PermissionResultDeny")

    def test_bypass_allows_ordinary_work(self):
        jid, spec = self.launch(mode="bypassPermissions")
        (self.jobs / jid / "control.jsonl").touch()
        r = runner_mod.Runner(self.jobs / jid)
        self.addCleanup(r.out.close)
        for tool, inp in (
                ("Bash", {"command": "pytest -q"}),
                # reading the live tree is how a session learns what to change
                ("Bash", {"command": f"grep -rn foo {self.root}/server"}),
                ("Write", {"file_path": str(Path(spec["worktree"]) / "x.py")})):
            out = asyncio.run(r.gate(tool, inp, None))
            self.assertEqual(type(out).__name__, "PermissionResultAllow",
                             f"{tool} {inp} should be allowed")

    def test_the_model_is_told(self):
        """The silent casualty: preamble gates its branch paragraph on
        worktree_path, so an empty field meant the session was never told it
        was placed, nor given the finish-what-you-start paragraph."""
        _, spec = self.launch()
        text = viratools.preamble(worktree_path=spec["worktree"],
                                  branch=spec["branch"],
                                  live_root=spec["live_root"])
        self.assertIn(spec["worktree"], text)
        self.assertIn(spec["live_root"], text)

    def test_the_audit_line_prints(self):
        _, spec = self.launch()
        self.assertIn("branch-first", spec["branch_note"])
        self.assertIn(spec["worktree"], spec["branch_note"])

    def test_the_ledger_records_where_it_could_write(self):
        """Job dirs prune at 400 and the registry forgets on restart, so the
        ledger row is the only durable answer to 'was that session guarded?'"""
        self.launch()
        row = joblog.record_launch.__wrapped__ if False else self.rows[-1]
        for key in ("worktree", "branch", "live_root"):
            self.assertTrue(row.get(key), f"{key} missing from the ledger row")

    def test_read_only_sessions_are_not_placed(self):
        """Placement is skipped for sessions that cannot damage anything —
        and the runner must not then read that as a disarmed guard."""
        jid, spec = self.launch(read_only=True)
        self.assertEqual(spec["worktree"], "")
        self.assertEqual(Path(spec["cwd"]).resolve(), self.root.resolve())
        (self.jobs / jid / "control.jsonl").touch()
        r = runner_mod.Runner(self.jobs / jid)
        self.addCleanup(r.out.close)
        self.assertFalse(r.disarmed)


class SpecDerivationContract(unittest.TestCase):
    """The structural guard. The literal that dropped the guard had already
    dropped read_only/meta, then reply_window, then provider — four times,
    each found by hand. This states the contract as one set equation so the
    fifth is caught by the suite instead."""

    def test_spec_is_launch_inputs_minus_runner_owned(self):
        data_keys = None
        spec_keys = {}

        def capture(path, obj):
            spec_keys.update(obj)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        jobs = Path(tmp.name)
        with mock.patch.object(session.jobfiles, "job_dir",
                               lambda jid: jobs / jid), \
             mock.patch.object(session.jobfiles, "write_json_atomic",
                               capture), \
             mock.patch.object(session.subprocess, "Popen",
                               runner_only_popen()), \
             mock.patch.object(session.joblog, "record_launch",
                               lambda job: None), \
             mock.patch.object(session, "SDK_AVAILABLE", True):
            reg = session.Sessions()
            real_spawn = session.Sessions._spawn_runner

            def spy(self_, data):
                nonlocal data_keys
                data_keys = set(data)
                return real_spawn(self_, data)

            with mock.patch.object(session.Sessions, "_spawn_runner", spy):
                reg.launch("hello", cwd=str(Path(tmp.name)))

        expected = (data_keys - session.RUNNER_OWNED) | session.SPAWN_COMPUTED
        self.assertEqual(set(spec_keys), expected)
        # and the exclusions really are only live state the runner republishes
        self.assertTrue(session.RUNNER_OWNED <= data_keys)


class ModeNaming(unittest.TestCase):
    """The rungs are named for Claude Code's --permission-mode values, and
    stored modes predate the rename — job.json files, ledger rows, circuit
    stage defs and routines.json all hold the retired spellings."""

    def test_retired_spellings_resolve(self):
        self.assertEqual(session.norm_mode("interactive"), "manual")
        self.assertEqual(session.norm_mode("acceptedits"), "acceptEdits")
        self.assertEqual(session.norm_mode("autopilot"), "bypassPermissions")

    def test_current_names_pass_through_and_junk_does_not(self):
        for m in session.MODES:
            self.assertEqual(session.norm_mode(m), m)
        self.assertIsNone(session.norm_mode("nonsense"))
        self.assertIsNone(session.norm_mode(""))
        self.assertEqual(session.norm_mode(None, "manual"), "manual")

    def test_every_rung_matches_a_claude_code_mode(self):
        """The whole point of the rename: a rung means the same thing in
        both tools. These are Claude Code's --permission-mode values."""
        claude_code = {"acceptEdits", "auto", "bypassPermissions", "manual",
                       "dontAsk", "plan"}
        self.assertTrue(set(session.MODES) <= claude_code,
                        f"{set(session.MODES) - claude_code} is not a Claude "
                        f"Code permission mode")

    def test_default_is_bypass(self):
        self.assertEqual(session.SESSION_DEFAULTS["session_default_mode"],
                         "bypassPermissions")


if __name__ == "__main__":
    unittest.main()
