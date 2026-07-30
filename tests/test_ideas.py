"""ideas.py project folders, and the folder picker's refusals.

Run: .venv/bin/python -m unittest tests.test_ideas
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import ideas, pickfolder


class ProjectPaths(unittest.TestCase):
    """"Connect a project" means point it at a folder.

    A project was a NAME only, while every dispatch asked the owner to
    re-type a target repo into the run sheet — the same fact, asked twice.
    Stored as a separate map because every reader of `projects` expects
    strings; changing that shape would be a migration for no gain.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._store = ideas.STORE
        ideas.STORE = Path(self.tmp.name) / "ideas.json"

    def tearDown(self):
        ideas.STORE = self._store
        self.tmp.cleanup()

    def test_connecting_a_folder_registers_the_project(self):
        r = ideas.set_project_path("Qocha", self.tmp.name)
        self.assertIn("Qocha", r["projects"])
        self.assertEqual(ideas.project_path("Qocha"),
                         str(Path(self.tmp.name).resolve()))

    def test_a_non_folder_is_refused_not_stored(self):
        f = Path(self.tmp.name) / "a-file.txt"
        f.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            ideas.set_project_path("Bad", str(f))
        with self.assertRaises(ValueError):
            ideas.set_project_path("Bad", str(Path(self.tmp.name) / "nope"))
        self.assertEqual(ideas.project_path("Bad"), "")

    def test_empty_path_clears_the_connection(self):
        ideas.set_project_path("Qocha", self.tmp.name)
        ideas.set_project_path("Qocha", "")
        self.assertEqual(ideas.project_path("Qocha"), "")
        # Clearing the FOLDER must not delete the PROJECT.
        self.assertIn("Qocha", ideas.list_projects())

    def test_projects_list_still_holds_plain_strings(self):
        ideas.set_project_path("Qocha", self.tmp.name)
        self.assertTrue(all(isinstance(p, str) for p in ideas.list_projects()))

    def test_unconnected_project_reads_empty(self):
        ideas.add_project("Nameonly")
        self.assertEqual(ideas.project_path("Nameonly"), "")


class PickFolderGuards(unittest.TestCase):
    """The picker opens a window on the owner's REAL desktop, so every
    context where nobody is looking at that desktop refuses rather than
    half-serves. Demo mode is the sharpest: a sandbox walkthrough popping a
    real folder panel is the exact escape --demo exists to prevent."""

    def test_remote_browser_refused(self):
        ok, reason = pickfolder.available(local=False)
        self.assertFalse(ok)
        self.assertIn("machine running Vira", reason)

    def test_demo_mode_refused(self):
        with mock.patch.object(pickfolder.settings, "demo", return_value=True):
            ok, reason = pickfolder.available(local=True)
        self.assertFalse(ok)
        self.assertIn("demo", reason)

    def test_passive_refused(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            ok, _ = pickfolder.available(local=True)
        self.assertFalse(ok)

    def test_pick_returns_unavailable_rather_than_raising(self):
        # A picker that 500s is worse than one that says it cannot run —
        # the text field it falls back to is right there either way.
        r = pickfolder.pick("x", local=False)
        self.assertTrue(r["unavailable"])
        self.assertNotIn("path", r)

    def test_mac_prompt_is_a_quoted_literal(self):
        # The prompt reaches osascript as part of a SCRIPT BODY, so a stray
        # quote would end the string and change what runs.
        self.assertEqual(pickfolder._mac_literal('a "b" \\c'),
                         '"a \\"b\\" \\\\c"')


if __name__ == "__main__":
    unittest.main()
