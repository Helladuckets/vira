"""Evidence Ledger: mining Vira's own build provenance (session retros, git
history, the job ledger) into interview-ready case studies.

Covers the two-rung split (find.py's pattern): mine() is pure and
deterministic; compose_episode() makes exactly one model call per episode
and validates everything it returns against the episode's own material
before saving — an ungrounded citation is dropped, never fabricated
forward. Also covers the store CRUD, plain-text export, the route layer,
and honest dormancy when no retro source is configured.

Run: .venv/bin/python -m unittest discover tests
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import evidence


def write_retro(dir_, name, date="2026-07-20", time_="09:00",
                goal="Ship the thing.",
                shipped="- did the thing\n  - a detail\n- another change"):
    path = Path(dir_) / name
    path.write_text(f"""---
tags: [session, retrospective]
project: vira
date: {date}
time: "{time_}"
---

# vira — {date} {time_}

## Goal

{goal}

## Shipped

{shipped}

## Surprises / blockers

Nothing major.
""", encoding="utf-8")
    return path


class _EngineCase(unittest.TestCase):
    """Isolates the retro sources, the store, and the repo retros dir so
    no test touches anything real on this machine."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.retro_dir = root / "sessions"
        self.retro_dir.mkdir()
        self.store = root / "evidence.json"
        self.repo_retros = root / "no-repo-retros"   # never created -> dormant
        for target, value in (
            ("_retro_dir", lambda: self.retro_dir),
            ("REPO_RETROS_DIR", self.repo_retros),
            ("STORE", self.store),
        ):
            p = mock.patch.object(evidence, target, value)
            p.start()
            self.addCleanup(p.stop)
        gp = mock.patch.object(evidence, "_git_log", return_value=[])
        gp.start()
        self.addCleanup(gp.stop)
        jp = mock.patch("server.joblog.list_records", return_value=[])
        jp.start()
        self.addCleanup(jp.stop)
        bp = mock.patch.object(evidence.settings, "get",
                               side_effect=lambda k: "" if k == "vault_root"
                               else evidence.settings.DEFAULTS.get(k))
        bp.start()
        self.addCleanup(bp.stop)


class ParseRetroTests(unittest.TestCase):
    def test_frontmatter_and_sections(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = write_retro(tmp.name, "2026-07-20 0900 vira.md")
        info = evidence._parse_retro_full(p)
        self.assertEqual(info["date"], "2026-07-20")
        self.assertEqual(info["time"], "09:00")
        self.assertEqual(info["goal"], "Ship the thing.")
        self.assertIn("Shipped", info["sections"])
        self.assertIn("did the thing", info["sections"]["Shipped"])
        self.assertIn("Surprises / blockers", info["sections"])

    def test_multiline_bullets_collapse_into_one_body(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = write_retro(tmp.name, "x.md",
                        shipped="- did the thing\n  - a nested detail\n"
                                "- second bullet spans\n  two lines")
        info = evidence._parse_retro_full(p)
        body = info["sections"]["Shipped"]
        self.assertIn("nested detail", body)
        self.assertIn("second bullet spans", body)
        self.assertIn("two lines", body)

    def test_empty_file_yields_no_sections(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "empty.md"
        p.write_text("", encoding="utf-8")
        info = evidence._parse_retro_full(p)
        self.assertEqual(info["sections"], {})
        self.assertEqual(info["date"], "")
        self.assertEqual(info["goal"], "")

    def test_h3_headings_do_not_split_a_section(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "h3.md"
        p.write_text("date: 2026-07-01\n\n## Shipped\n\nintro\n\n"
                     "### a subheading\n\nmore detail\n", encoding="utf-8")
        info = evidence._parse_retro_full(p)
        self.assertIn("Shipped", info["sections"])
        self.assertIn("subheading", info["sections"]["Shipped"])
        self.assertIn("more detail", info["sections"]["Shipped"])


class MineTests(_EngineCase):
    def test_join_attaches_commits_and_jobs_by_date(self):
        write_retro(self.retro_dir, "2026-07-20 0900 vira.md",
                    date="2026-07-20")
        write_retro(self.retro_dir, "2026-07-19 1000 vira.md",
                    date="2026-07-19")
        with mock.patch.object(evidence, "_git_log", return_value=[
                {"sha": "a" * 40, "date": "2026-07-20", "subject": "did X"},
                {"sha": "b" * 40, "date": "2026-07-19", "subject": "did Y"},
                {"sha": "c" * 40, "date": "2026-06-01", "subject": "unrelated"},
             ]), mock.patch("server.joblog.list_records", return_value=[
                {"id": "job_1", "cwd": str(evidence.ROOT), "status": "done",
                 "prompt": "do the thing", "started": "2026-07-20T09:05:00",
                 "title": ""},
                {"id": "job_2", "cwd": "/somewhere/else", "status": "done",
                 "prompt": "unrelated repo", "started": "2026-07-20T09:05:00",
                 "title": ""},
             ]):
            episodes = evidence.mine()
        self.assertEqual(len(episodes), 2)
        # newest first
        self.assertEqual(episodes[0]["date"], "2026-07-20")
        self.assertEqual([c["sha"] for c in episodes[0]["commits"]], ["a" * 40])
        self.assertEqual([j["id"] for j in episodes[0]["jobs"]], ["job_1"])
        self.assertEqual(episodes[1]["date"], "2026-07-19")
        self.assertEqual([c["sha"] for c in episodes[1]["commits"]], ["b" * 40])
        self.assertEqual(episodes[1]["jobs"], [])

    def test_composed_flips_once_a_case_exists(self):
        write_retro(self.retro_dir, "2026-07-20 0900 vira.md")
        key = "2026-07-20 0900 vira"
        self.assertFalse(evidence.mine()[0]["composed"])
        evidence._add_case({
            "id": "ev_x", "episode_key": key, "title": "t", "problem": "p",
            "direction": "d", "outcome": "o", "skills": [], "citations": [],
            "status": "draft", "created": "now", "updated": "now",
            "edited": False,
        })
        self.assertTrue(evidence.mine()[0]["composed"])

    def test_archived_case_frees_the_episode(self):
        write_retro(self.retro_dir, "2026-07-20 0900 vira.md")
        key = "2026-07-20 0900 vira"
        evidence._add_case({
            "id": "ev_x", "episode_key": key, "title": "t", "problem": "p",
            "direction": "d", "outcome": "o", "skills": [], "citations": [],
            "status": "archived", "created": "now", "updated": "now",
            "edited": False,
        })
        self.assertFalse(evidence.mine()[0]["composed"])

    def test_empty_retro_is_skipped(self):
        (self.retro_dir / "nothing vira.md").write_text("", encoding="utf-8")
        self.assertEqual(evidence.mine(), [])


class StoreTests(_EngineCase):
    def _seed(self, **overrides):
        case = {
            "id": "ev_seed001", "episode_key": "k1", "title": "Title",
            "problem": "Problem.", "direction": "Direction.",
            "outcome": "Outcome.", "skills": ["scoping"], "citations": [],
            "status": "draft", "created": "2026-07-01T00:00:00",
            "updated": "2026-07-01T00:00:00", "edited": False,
        }
        case.update(overrides)
        evidence._add_case(case)
        return case

    def test_list_cases_roundtrip(self):
        c = self._seed()
        self.assertEqual(evidence.list_cases()[0]["id"], c["id"])

    def test_update_whitelists_fields_and_stamps(self):
        c = self._seed()
        updated = evidence.update_case(c["id"], {
            "title": "New title", "status": "approved",
            "skills": ["a", "b"], "unknown_field": "ignored me",
        })
        self.assertEqual(updated["title"], "New title")
        self.assertEqual(updated["status"], "approved")
        self.assertEqual(updated["skills"], ["a", "b"])
        self.assertTrue(updated["edited"])
        self.assertNotEqual(updated["updated"], c["updated"])

    def test_update_rejects_bad_status(self):
        c = self._seed()
        updated = evidence.update_case(c["id"], {"status": "bogus"})
        self.assertEqual(updated["status"], "draft")   # unchanged

    def test_update_unknown_id_raises(self):
        with self.assertRaises(KeyError):
            evidence.update_case("ev_nope", {"title": "x"})

    def test_delete_removes_and_raises_on_repeat(self):
        c = self._seed()
        evidence.delete_case(c["id"])
        self.assertEqual(evidence.list_cases(), [])
        with self.assertRaises(KeyError):
            evidence.delete_case(c["id"])

    def test_caps_enforced(self):
        c = self._seed()
        long_title = "x" * 500
        updated = evidence.update_case(c["id"], {"title": long_title})
        self.assertEqual(len(updated["title"]), evidence.MAX_TITLE)


class ComposeTests(_EngineCase):
    def setUp(self):
        super().setUp()
        write_retro(self.retro_dir, "2026-07-20 0900 vira.md")
        self.key = "2026-07-20 0900 vira"
        self.retro_file = "2026-07-20 0900 vira.md"
        gp = mock.patch.object(evidence, "_git_log", return_value=[
            {"sha": "abc1234abc1234abc1234abc1234abc1234abcd",
             "date": "2026-07-20", "subject": "did the thing"},
        ])
        gp.start()
        self.addCleanup(gp.stop)
        jp = mock.patch("server.joblog.list_records", return_value=[
            {"id": "job_9", "cwd": str(evidence.ROOT), "status": "done",
             "prompt": "build the thing", "started": "2026-07-20T09:05:00",
             "title": ""},
        ])
        jp.start()
        self.addCleanup(jp.stop)

    def _complete(self, payload):
        return mock.patch("server.suggest.complete",
                          return_value=json.dumps(payload))

    def test_valid_payload_saved_as_draft(self):
        payload = {
            "title": "Shipped the thing", "problem": "Nothing existed.",
            "direction": "I told the agent to build X and fix Y.",
            "outcome": "X shipped, tests green.",
            "skills": ["scoping an ambiguous ask", "verification"],
            "citations": [self.retro_file, "abc1234", "job_9", "made-up-sha"],
        }
        with self._complete(payload):
            case = evidence.compose_episode(self.key)
        self.assertEqual(case["status"], "draft")
        self.assertEqual(case["episode_key"], self.key)
        self.assertIn(self.retro_file, case["citations"])
        self.assertIn("job_9", case["citations"])
        self.assertNotIn("made-up-sha", case["citations"])
        # a full sha starting with the 7-char prefix is grounded
        self.assertTrue(any(c.startswith("abc1234") for c in case["citations"]))

    def test_all_citations_ungrounded_still_saves_flagged_empty(self):
        payload = {
            "title": "T", "problem": "P", "direction": "D", "outcome": "O",
            "skills": [], "citations": ["totally-invented"],
        }
        with self._complete(payload):
            case = evidence.compose_episode(self.key)
        self.assertEqual(case["citations"], [])

    def test_missing_required_field_raises(self):
        payload = {"title": "T", "problem": "P", "direction": "D"}
        with self._complete(payload):
            with self.assertRaises(ValueError):
                evidence.compose_episode(self.key)
        self.assertEqual(evidence.list_cases(), [])

    def test_already_composed_without_force_raises(self):
        payload = {"title": "T", "problem": "P", "direction": "D",
                  "outcome": "O", "skills": [], "citations": []}
        with self._complete(payload):
            evidence.compose_episode(self.key)
            with self.assertRaises(ValueError):
                evidence.compose_episode(self.key)

    def test_force_recomposes(self):
        payload = {"title": "T", "problem": "P", "direction": "D",
                  "outcome": "O", "skills": [], "citations": []}
        with self._complete(payload):
            evidence.compose_episode(self.key)
            evidence.compose_episode(self.key, force=True)
        self.assertEqual(len(evidence.list_cases()), 2)

    def test_unknown_episode_raises_keyerror(self):
        with self.assertRaises(KeyError):
            evidence.compose_episode("no-such-episode")

    def test_fenced_json_is_tolerated(self):
        payload = {"title": "T", "problem": "P", "direction": "D",
                  "outcome": "O", "skills": [], "citations": []}
        fenced = "```json\n" + json.dumps(payload) + "\n```"
        with mock.patch("server.suggest.complete", return_value=fenced):
            case = evidence.compose_episode(self.key)
        self.assertEqual(case["title"], "T")

    def test_compose_new_sweeps_uncomposed_episodes(self):
        write_retro(self.retro_dir, "2026-07-19 0900 vira.md",
                   date="2026-07-19")
        payload = {"title": "T", "problem": "P", "direction": "D",
                  "outcome": "O", "skills": [], "citations": []}
        with self._complete(payload):
            results = evidence.compose_new(limit=5)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(len(evidence.list_cases()), 2)

    def test_compose_new_one_bad_episode_does_not_stop_the_sweep(self):
        write_retro(self.retro_dir, "2026-07-19 0900 vira.md",
                   date="2026-07-19")
        bad = {"title": "T"}   # missing required fields
        good = {"title": "T", "problem": "P", "direction": "D",
                "outcome": "O", "skills": [], "citations": []}
        calls = {"n": 0}

        def side_effect(_prompt):
            calls["n"] += 1
            return json.dumps(bad if calls["n"] == 1 else good)

        with mock.patch("server.suggest.complete", side_effect=side_effect):
            results = evidence.compose_new(limit=5)
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(1 for r in results if r["ok"]), 1)
        self.assertEqual(sum(1 for r in results if not r["ok"]), 1)


class ExportTests(_EngineCase):
    def test_export_case_shape_and_ascii(self):
        evidence._add_case({
            "id": "ev_1", "episode_key": "k1", "title": "A Case — title",
            "problem": "The ‘problem’.", "direction": "Steered it.",
            "outcome": "It shipped.", "skills": ["a"], "citations": ["x.md"],
            "status": "approved", "created": "now", "updated": "2026-07-20",
            "edited": False,
        })
        text = evidence.export_case("ev_1")
        self.assertIn("CASE:", text)
        self.assertIn("PROBLEM:", text)
        self.assertIn("HOW I DIRECTED THE AGENT:", text)
        self.assertIn("WHAT SHIPPED:", text)
        self.assertIn("EVIDENCE: x.md", text)
        self.assertTrue(all(ord(ch) < 128 for ch in text),
                        "export must be ASCII-only")

    def test_export_unknown_case_raises(self):
        with self.assertRaises(KeyError):
            evidence.export_case("ev_nope")

    def test_export_approved_is_approved_only_newest_first(self):
        evidence._add_case({
            "id": "ev_draft", "episode_key": "k1", "title": "Draft one",
            "problem": "p", "direction": "d", "outcome": "o", "skills": [],
            "citations": [], "status": "draft", "created": "now",
            "updated": "2026-07-01", "edited": False,
        })
        evidence._add_case({
            "id": "ev_old", "episode_key": "k2", "title": "Old approved",
            "problem": "p", "direction": "d", "outcome": "o", "skills": [],
            "citations": [], "status": "approved", "created": "now",
            "updated": "2026-07-01", "edited": False,
        })
        evidence._add_case({
            "id": "ev_new", "episode_key": "k3", "title": "New approved",
            "problem": "p", "direction": "d", "outcome": "o", "skills": [],
            "citations": [], "status": "approved", "created": "now",
            "updated": "2026-07-20", "edited": False,
        })
        text = evidence.export_approved()
        self.assertNotIn("Draft one", text)
        self.assertLess(text.index("New approved"), text.index("Old approved"))


class DormancyTests(_EngineCase):
    def test_missing_retro_dir_is_dormant_not_an_exception(self):
        missing = Path(self.tmp.name) / "does-not-exist"
        with mock.patch.object(evidence, "_retro_dir", lambda: missing):
            self.assertEqual(evidence.mine(), [])
            st = evidence.status()
        self.assertFalse(st["retro_dir_exists"])
        self.assertEqual(st["retro_count"], 0)
        self.assertEqual(st["episodes"], 0)

    def test_status_shape_with_data(self):
        write_retro(self.retro_dir, "2026-07-20 0900 vira.md")
        st = evidence.status()
        self.assertEqual(st["retro_count"], 1)
        self.assertTrue(st["retro_dir_exists"])
        self.assertIn("draft", st["cases"])
        self.assertIn("approved", st["cases"])
        self.assertIn("archived", st["cases"])


class RouteTests(_EngineCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from server import main
        cls.client = TestClient(main.app)

    def test_get_evidence_shape(self):
        write_retro(self.retro_dir, "2026-07-20 0900 vira.md")
        r = self.client.get("/api/evidence")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("cases", body)
        self.assertIn("episodes", body)
        self.assertIn("status", body)
        self.assertEqual(len(body["episodes"]), 1)

    def test_compose_one_episode_route(self):
        write_retro(self.retro_dir, "2026-07-20 0900 vira.md")
        payload = {"title": "T", "problem": "P", "direction": "D",
                  "outcome": "O", "skills": [], "citations": []}
        with mock.patch("server.suggest.complete",
                        return_value=json.dumps(payload)):
            r = self.client.post("/api/evidence/compose",
                                 json={"episode": "2026-07-20 0900 vira"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["title"], "T")

    def test_compose_unknown_episode_404(self):
        r = self.client.post("/api/evidence/compose",
                             json={"episode": "nope"})
        self.assertEqual(r.status_code, 404)

    def test_compose_already_composed_409(self):
        write_retro(self.retro_dir, "2026-07-20 0900 vira.md")
        key = "2026-07-20 0900 vira"
        evidence._add_case({
            "id": "ev_x", "episode_key": key, "title": "t", "problem": "p",
            "direction": "d", "outcome": "o", "skills": [], "citations": [],
            "status": "draft", "created": "now", "updated": "now",
            "edited": False,
        })
        r = self.client.post("/api/evidence/compose", json={"episode": key})
        self.assertEqual(r.status_code, 409)

    def test_compose_sweep_starts_in_background(self):
        with mock.patch.object(evidence, "compose_new", return_value=[]):
            r = self.client.post("/api/evidence/compose", json={})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["started"])

    def test_put_update_route(self):
        evidence._add_case({
            "id": "ev_1", "episode_key": "k", "title": "old", "problem": "p",
            "direction": "d", "outcome": "o", "skills": [], "citations": [],
            "status": "draft", "created": "now", "updated": "now",
            "edited": False,
        })
        r = self.client.put("/api/evidence/ev_1", json={"title": "new"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["title"], "new")

    def test_put_unknown_404(self):
        r = self.client.put("/api/evidence/ev_nope", json={"title": "x"})
        self.assertEqual(r.status_code, 404)

    def test_delete_route(self):
        evidence._add_case({
            "id": "ev_1", "episode_key": "k", "title": "t", "problem": "p",
            "direction": "d", "outcome": "o", "skills": [], "citations": [],
            "status": "draft", "created": "now", "updated": "now",
            "edited": False,
        })
        r = self.client.delete("/api/evidence/ev_1")
        self.assertEqual(r.status_code, 200)
        r = self.client.delete("/api/evidence/ev_1")
        self.assertEqual(r.status_code, 404)

    def test_export_routes(self):
        evidence._add_case({
            "id": "ev_1", "episode_key": "k", "title": "t", "problem": "p",
            "direction": "d", "outcome": "o", "skills": [], "citations": [],
            "status": "approved", "created": "now", "updated": "now",
            "edited": False,
        })
        r = self.client.get("/api/evidence/ev_1/export")
        self.assertEqual(r.status_code, 200)
        self.assertIn("CASE:", r.json()["text"])
        r = self.client.get("/api/evidence/export")
        self.assertEqual(r.status_code, 200)
        self.assertIn("CASE:", r.json()["text"])

    def test_export_unknown_case_404(self):
        r = self.client.get("/api/evidence/ev_nope/export")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
