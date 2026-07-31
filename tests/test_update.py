"""Updater dependency sync: the pip step that rides every update.

apply() pulls, then installs requirements.txt into the running venv before
restarting — so a Vira commit that bumps the qocha pin (or adds any
dependency) reaches updating users in the same click as the code that
needs it. Editable dev installs are never overwritten, and a failed
install blocks the restart instead of booting onto broken deps.

Run: .venv/bin/python -m unittest discover tests
"""
import unittest
from pathlib import Path
from unittest import mock

from server import update


class NoSandboxLoop(unittest.TestCase):
    """supervisor() asks the ENVIRONMENT whether a sandbox relaunch loop is
    watching, and that branch outranks the config ones. A suite run from
    inside a sandbox would otherwise answer differently from one run outside
    it — the precondition is pinned rather than inherited from the machine."""

    def setUp(self):
        p = mock.patch.object(update.settings, "sandbox_loop", return_value="")
        p.start()
        self.addCleanup(p.stop)


class ReqNameTests(unittest.TestCase):
    def test_parses_common_shapes(self):
        self.assertEqual(update._req_name("fastapi"), "fastapi")
        self.assertEqual(update._req_name("uvicorn[standard]"), "uvicorn")
        self.assertEqual(update._req_name("claude-agent-sdk==0.2.115"),
                         "claude-agent-sdk")
        self.assertEqual(
            update._req_name(
                "qocha @ git+https://github.com/Helladuckets/qocha@v0.2.0"),
            "qocha")

    def test_blank_and_comment_lines_are_none(self):
        self.assertIsNone(update._req_name(""))
        self.assertIsNone(update._req_name("   "))
        self.assertIsNone(update._req_name("# a comment"))
        self.assertIsNone(update._req_name("-r other.txt"))


class InstallDepsTests(unittest.TestCase):
    def test_editable_packages_are_skipped(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen["req"] = Path(cmd[-1]).read_text()
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(update, "_editable",
                               side_effect=lambda n: n == "qocha"), \
             mock.patch.object(update.subprocess, "run", side_effect=fake_run):
            note = update._install_deps()
        names = {update._req_name(l) for l in seen["req"].splitlines()}
        self.assertNotIn("qocha", names)
        self.assertIn("fastapi", names)
        self.assertIn("editable, untouched: qocha", note)

    def test_nothing_editable_installs_everything(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen["req"] = Path(cmd[-1]).read_text()
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(update, "_editable", return_value=False), \
             mock.patch.object(update.subprocess, "run", side_effect=fake_run):
            note = update._install_deps()
        names = {update._req_name(l) for l in seen["req"].splitlines()}
        self.assertIn("qocha", names)
        self.assertEqual(note, "dependencies synced")

    def test_pip_failure_raises(self):
        with mock.patch.object(update, "_editable", return_value=False), \
             mock.patch.object(update.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="",
                                                      stderr="boom")):
            with self.assertRaises(RuntimeError):
                update._install_deps()


def _fake_git(*args, **kw):
    """Clean tree, successful pull, stable sha — the happy-path git."""
    if args[0] == "status":
        return mock.Mock(returncode=0, stdout="", stderr="")
    return mock.Mock(returncode=0, stdout="abc1234\n", stderr="")


class ApplyDepsGateTests(NoSandboxLoop):
    # Platform pinned: these run on the Windows CI runner too, where the
    # supervisor seam would otherwise demand windows_task_name instead.
    def test_deps_failure_blocks_restart(self):
        with mock.patch.object(update.settings, "IS_WIN", False), \
             mock.patch.object(update.settings, "raw",
                               return_value={"launchd_label": "test.label"}), \
             mock.patch.object(update, "status",
                               return_value={"git": True, "remote": True,
                                             "behind": 1}), \
             mock.patch.object(update, "_git", side_effect=_fake_git), \
             mock.patch.object(update, "_install_deps",
                               side_effect=RuntimeError("offline")), \
             mock.patch.object(update.threading, "Timer") as timer:
            with self.assertRaises(ValueError) as ctx:
                update.apply()
            timer.assert_not_called()
        self.assertIn("not restarting", str(ctx.exception))

    def test_success_installs_then_restarts(self):
        with mock.patch.object(update.settings, "IS_WIN", False), \
             mock.patch.object(update.settings, "raw",
                               return_value={"launchd_label": "test.label"}), \
             mock.patch.object(update, "status",
                               return_value={"git": True, "remote": True,
                                             "behind": 1}), \
             mock.patch.object(update, "_git", side_effect=_fake_git), \
             mock.patch.object(update, "_install_deps",
                               return_value="dependencies synced") as deps, \
             mock.patch.object(update.threading, "Timer") as timer:
            out = update.apply()
        deps.assert_called_once()
        timer.assert_called_once()
        self.assertTrue(out["updated"])
        self.assertEqual(out["deps"], "dependencies synced")


class SupervisorSeamTests(NoSandboxLoop):
    """One restart contract, two supervisors: launchd on a Mac,
    a Task Scheduler relaunch loop on Windows. The refuse-without-a-
    supervisor rule (bucket-A #6) must hold identically on both."""

    def test_mac_reads_launchd_label(self):
        with mock.patch.object(update.settings, "IS_WIN", False), \
             mock.patch.object(update.settings, "raw",
                               return_value={"launchd_label": "com.x.vira",
                                             "windows_task_name": "Vira"}):
            self.assertEqual(update.supervisor(), ("launchd", "com.x.vira"))

    def test_windows_reads_task_name(self):
        with mock.patch.object(update.settings, "IS_WIN", True), \
             mock.patch.object(update.settings, "raw",
                               return_value={"launchd_label": "com.x.vira",
                                             "windows_task_name": "Vira"}):
            self.assertEqual(update.supervisor(), ("task", "Vira"))

    def test_windows_apply_refuses_without_task_name(self):
        # A launchd label is not a supervisor on Windows.
        with mock.patch.object(update.settings, "IS_WIN", True), \
             mock.patch.object(update.settings, "raw",
                               return_value={"launchd_label": "com.x.vira"}):
            with self.assertRaises(ValueError) as ctx:
                update.apply()
        self.assertIn("windows_task_name", str(ctx.exception))
        self.assertIn("run.ps1", str(ctx.exception))

    def test_windows_apply_proceeds_with_task_name(self):
        with mock.patch.object(update.settings, "IS_WIN", True), \
             mock.patch.object(update.settings, "raw",
                               return_value={"windows_task_name": "Vira"}), \
             mock.patch.object(update, "status",
                               return_value={"git": True, "remote": True,
                                             "behind": 1}), \
             mock.patch.object(update, "_git", side_effect=_fake_git), \
             mock.patch.object(update, "_install_deps",
                               return_value="dependencies synced"), \
             mock.patch.object(update.threading, "Timer") as timer:
            out = update.apply()
        timer.assert_called_once()
        self.assertTrue(out["updated"])


class WindowsRestartTests(NoSandboxLoop):
    """_restart on Windows: exit into the scheduled task's relaunch loop
    (run.ps1 -Serve), plus a detached best-effort `schtasks /Run` so a
    task whose action runs the server directly also comes back. Never
    `schtasks /End` from inside — ending the task terminates this
    process's whole job, helper included, before /Run could ever fire."""

    def test_restart_spawns_run_helper_then_exits(self):
        with mock.patch.object(update.settings, "IS_WIN", True), \
             mock.patch.object(update.settings, "raw",
                               return_value={"windows_task_name": "Vira"}), \
             mock.patch.object(update.subprocess, "Popen") as popen, \
             mock.patch.object(update.os, "_exit") as ex:
            update._restart()
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[:2], ["cmd", "/c"])
        self.assertIn('schtasks /Run /TN "Vira"', cmd[2])
        self.assertNotIn("/End", cmd[2])
        ex.assert_called_once_with(0)

    def test_restart_without_name_just_exits(self):
        with mock.patch.object(update.settings, "IS_WIN", True), \
             mock.patch.object(update.settings, "raw", return_value={}), \
             mock.patch.object(update.subprocess, "Popen") as popen, \
             mock.patch.object(update.os, "_exit") as ex:
            update._restart()
        popen.assert_not_called()
        ex.assert_called_once_with(0)

    def test_helper_failure_still_exits(self):
        with mock.patch.object(update.settings, "IS_WIN", True), \
             mock.patch.object(update.settings, "raw",
                               return_value={"windows_task_name": "Vira"}), \
             mock.patch.object(update.subprocess, "Popen",
                               side_effect=OSError("no cmd")), \
             mock.patch.object(update.os, "_exit") as ex:
            update._restart()
        ex.assert_called_once_with(0)


class SandboxLoopSupervisorTests(unittest.TestCase):
    """A sandbox is supervised by scripts/sandbox.sh's relaunch loop, which
    announces itself in the ENVIRONMENT rather than in config — a sandbox has
    no config.json until someone sets one up, and booting without one is the
    whole point of it."""

    def test_loop_outranks_config(self):
        with mock.patch.object(update.settings, "sandbox_loop",
                               return_value="/tmp/x/.maintenance"), \
             mock.patch.object(update.settings, "IS_WIN", False), \
             mock.patch.object(update.settings, "raw",
                               return_value={"launchd_label": "com.x.vira"}):
            kind, name = update.supervisor()
        self.assertEqual(kind, "loop")
        self.assertTrue(name)          # non-empty == supervised == apply() ok

    def test_restart_under_a_loop_just_exits(self):
        # No kickstart, no schtasks: exiting IS the restart, because the loop
        # is already waiting on this process.
        with mock.patch.object(update.settings, "sandbox_loop",
                               return_value="/tmp/x/.maintenance"), \
             mock.patch.object(update.subprocess, "run") as run, \
             mock.patch.object(update.subprocess, "Popen") as popen, \
             mock.patch.object(update.os, "_exit") as ex:
            update._restart()
        run.assert_not_called()
        popen.assert_not_called()
        ex.assert_called_once_with(0)


class PullApplySplitTests(NoSandboxLoop):
    """pull() does the code work and stops; apply() is pull() plus the
    restart. The split exists so a caller that must NOT restart yet (the
    sandbox reset, which has a wipe to queue first) can reuse the refusals
    rather than growing a second copy of them."""

    def test_pull_does_not_restart(self):
        with mock.patch.object(update, "status",
                               return_value={"git": True, "remote": True,
                                             "behind": 1}), \
             mock.patch.object(update, "_git", side_effect=_fake_git), \
             mock.patch.object(update, "_install_deps",
                               return_value="dependencies synced"), \
             mock.patch.object(update.threading, "Timer") as timer:
            out = update.pull()
        timer.assert_not_called()
        self.assertTrue(out["updated"])
        self.assertNotIn("restarting", out)

    def test_pull_needs_no_supervisor(self):
        # The refuse-without-a-supervisor rule guards the RESTART. Updating
        # the checkout harms nothing on its own, so pull() must not inherit it.
        with mock.patch.object(update.settings, "IS_WIN", False), \
             mock.patch.object(update.settings, "raw", return_value={}), \
             mock.patch.object(update, "status",
                               return_value={"git": True, "remote": True,
                                             "behind": 0, "sha": "abc1234"}):
            out = update.pull()
        self.assertFalse(out["updated"])

    def test_deps_failure_is_its_own_error_class(self):
        # A caller that shrugs off an ordinary refusal must still stop here:
        # the code moved and its dependencies did not.
        with mock.patch.object(update, "status",
                               return_value={"git": True, "remote": True,
                                             "behind": 1}), \
             mock.patch.object(update, "_git", side_effect=_fake_git), \
             mock.patch.object(update, "_install_deps",
                               side_effect=RuntimeError("offline")):
            with self.assertRaises(update.DepsError):
                update.pull()

    def test_a_refusal_is_not_a_deps_error(self):
        with mock.patch.object(update, "status",
                               return_value={"git": False}):
            with self.assertRaises(ValueError) as ctx:
                update.pull()
        self.assertNotIsInstance(ctx.exception, update.DepsError)


if __name__ == "__main__":
    unittest.main()
