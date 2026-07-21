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


class ApplyDepsGateTests(unittest.TestCase):
    def test_deps_failure_blocks_restart(self):
        with mock.patch.object(update.settings, "raw",
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
        with mock.patch.object(update.settings, "raw",
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


if __name__ == "__main__":
    unittest.main()
