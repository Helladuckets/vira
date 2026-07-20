"""Change-log project scoping: the Vira changelog folds in ONLY
Vira-project ideas and jobs that ran in the Vira checkout (or were
dispatched from a Vira-project idea). Entries belonging to other
projects never leak in.

Run: .venv/bin/python -m unittest tests.test_changelog
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import changelog


RETRO = """---
date: 2026-07-11
time: "21:00"
---

## Goal

Ship the widget.

## Shipped

- The widget shipped.
"""


def _idea(iid, text, project, status="done", updated="2026-07-11T20:00:00"):
    return {"id": iid, "text": text, "project": project,
            "status": status, "updated": updated}


def _job(jid, cwd, idea_id=None, started="2026-07-11T20:30:00"):
    return {"id": jid, "prompt": "do " + jid, "cwd": cwd,
            "idea_id": idea_id, "status": "done", "started": started,
            "model": None, "session_id": None}


class ChangelogScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        sessions = Path(self.tmp.name)
        (sessions / "2026-07-11 vira.md").write_text(RETRO)
        self.sessions_patch = mock.patch.object(
            changelog, "SESSIONS", sessions)
        self.sessions_patch.start()
        self.addCleanup(self.sessions_patch.stop)

    def _groups(self, ideas, jobs):
        with mock.patch.object(changelog.ideasstore, "list_items",
                               return_value=ideas), \
             mock.patch.object(changelog.joblog, "list_records",
                               return_value=jobs):
            return changelog.groups()

    def _texts(self, groups):
        return [e["text"] for g in groups for e in g["entries"]]

    def test_foreign_project_ideas_stay_out(self):
        groups = self._groups(
            [_idea("i1", "vira thing", "Vira"),
             _idea("i2", "site thing", "other-project"),
             _idea("i3", "legacy thing", None)],   # project-less = Vira
            [])
        texts = self._texts(groups)
        self.assertIn("vira thing", texts)
        self.assertIn("legacy thing", texts)
        self.assertNotIn("site thing", texts)

    def test_foreign_cwd_jobs_stay_out(self):
        vira_cwd = str(changelog.REPO)
        groups = self._groups([], [
            _job("j1", vira_cwd),
            _job("j2", str(Path.home())),
            _job("j3", str(Path.home() / "TC-IL")),
        ])
        texts = " ".join(self._texts(groups))
        self.assertIn("do j1", texts)
        self.assertNotIn("do j2", texts)
        self.assertNotIn("do j3", texts)

    def test_vira_idea_job_counts_even_from_foreign_cwd(self):
        groups = self._groups(
            [_idea("i1", "vira idea", "Vira", status="open")],
            [_job("j1", str(Path.home()), idea_id="i1"),
             _job("j2", str(Path.home()), idea_id="missing")])
        texts = " ".join(self._texts(groups))
        # idea-linked jobs are named for the idea, not the prompt head
        self.assertIn("Implement — vira idea", texts)
        self.assertNotIn("do j2", texts)

    def test_job_labels_prefer_meaning_over_prompt_head(self):
        routine = _job("j1", str(changelog.REPO))
        routine["meta"] = {"routine_id": "system-map", "kind": "digest"}
        ask = _job("j2", str(changelog.REPO))
        ask["prompt"] = ('You are Vira, spawned from a right-click.\n"""\n'
                         "What is the demo contact waiting on?\n" + '"""\n')
        groups = self._groups([], [routine, ask])
        texts = " ".join(self._texts(groups))
        self.assertIn("System map — refresh the registry from the change log",
                      texts)
        self.assertIn("Ask Vira — What is the demo contact waiting on?", texts)

    def test_edited_title_wins_in_the_log(self):
        job = _job("j1", str(changelog.REPO))
        job["title"] = "Morning subs pipeline"      # an owner rename
        groups = self._groups([], [job])
        self.assertIn("Morning subs pipeline — done",
                      " ".join(self._texts(groups)))

    def test_retro_ships_survive_scoping(self):
        groups = self._groups([], [])
        self.assertEqual(groups[0]["date"], "2026-07-11")
        self.assertIn("The widget shipped.", self._texts(groups))
