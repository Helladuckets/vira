"""The profile-refresh button's plumbing: the one-pass current mode (model
mocked) writes the description back with provenance and a one-deep prev,
the explore mode dispatches a session whose prompt names the real tools,
and the agent-side write tool resolves people and refuses junk.

Run: .venv/bin/python -m unittest tests.test_profilerefresh
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import data as crm
from server import profilerefresh, viratools


def _seed_crm(root):
    root = Path(root)
    (root / "profiles").mkdir(parents=True)
    people = {"people": [
        {"id": "p_test00000001", "name": "Casey Example",
         "handles": {"imessage": ["casey@example.test"], "emails": [],
                     "phones10": []}},
        {"id": "p_test00000002", "name": "Drew Sample",
         "handles": {"imessage": [], "emails": [], "phones10": []}},
    ]}
    (root / "people.json").write_text(json.dumps(people), encoding="utf-8")
    (root / "master.json").write_text("[]", encoding="utf-8")
    prof = {"name": "Casey Example",
            "relationship_class": "friend",
            "relationship_summary": "Casey is an old friend [2024-01-01].",
            "refresh_count": 3,
            "stats": {"imsg_first": "2015-11-02"}}
    (root / "profiles" / "p_test00000001.json").write_text(
        json.dumps(prof), encoding="utf-8")
    return root


NEW_SUMMARY = ("Casey is a close friend whose thread went quiet in spring "
               "[2026-03-01]; the early email shows they met through the "
               "climbing gym [2013-06-15].")


class RefreshBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _seed_crm(self.tmp.name)
        patches = [
            mock.patch("server.data.settings.crm_root",
                       return_value=self.root),
            mock.patch("server.profilerefresh.settings.fixture_mode",
                       return_value=False),
            mock.patch.object(profilerefresh.imessage, "thread_for_person",
                              return_value=[
                                  {"when": "2026-03-01T10:00:00",
                                   "from_me": False,
                                   "text": "long time! we should climb"}]),
            mock.patch.object(profilerefresh.textindex, "search",
                              return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        crm.invalidate()
        self.addCleanup(crm.invalidate)
        self.addCleanup(self.tmp.cleanup)

    def _profile(self):
        return json.loads(
            (self.root / "profiles"
             / "p_test00000001.json").read_text(encoding="utf-8"))


class TestCurrentMode(RefreshBase):
    def _model(self, payload):
        return mock.patch.object(profilerefresh.suggest, "complete",
                                 return_value=json.dumps(payload))

    def test_refresh_writes_summary_prev_and_provenance(self):
        with self._model({"relationship_summary": NEW_SUMMARY,
                          "how_we_met": "Through the climbing gym."}):
            out = profilerefresh.refresh_current("p_test00000001")
        self.assertEqual(out["status"], "ok")
        prof = self._profile()
        self.assertEqual(prof["relationship_summary"], NEW_SUMMARY)
        self.assertEqual(prof["prev_relationship_summary"],
                         "Casey is an old friend [2024-01-01].")
        self.assertEqual(prof["how_we_met"], "Through the climbing gym.")
        self.assertEqual(prof["refresh_count"], 4)
        self.assertEqual(prof["last_refresh_reason"], "vira-refresh-current")
        self.assertIn("relationship_summary_updated_by_vira", prof)

    def test_a_trivial_summary_is_refused_and_nothing_is_written(self):
        with self._model({"relationship_summary": "ok"}):
            with self.assertRaises(ValueError):
                profilerefresh.refresh_current("p_test00000001")
        self.assertEqual(self._profile()["relationship_summary"],
                         "Casey is an old friend [2024-01-01].")

    def test_prompt_carries_thread_and_mail_sections(self):
        with mock.patch.object(
                profilerefresh.textindex, "search",
                return_value=[{"when": "2013-06-15T09:00:00", "from_me": True,
                               "subject": "gym", "text": "see you there"}]):
            prompt = profilerefresh._prompt(
                profilerefresh._context("p_test00000001"))
        self.assertIn("RECENT DIRECT THREAD", prompt)
        self.assertIn("OLDEST EMAIL ON RECORD", prompt)
        self.assertIn("we should climb", prompt)
        self.assertIn("STRICT JSON", prompt)

    def test_unknown_person_raises_keyerror(self):
        with self.assertRaises(KeyError):
            profilerefresh.refresh_current("p_nobody")

    def test_unchanged_how_we_met_is_not_restamped(self):
        prof = self._profile()
        prof["how_we_met"] = "Through the climbing gym."
        (self.root / "profiles" / "p_test00000001.json").write_text(
            json.dumps(prof), encoding="utf-8")
        crm.invalidate()
        with self._model({"relationship_summary": NEW_SUMMARY,
                          "how_we_met": "Through the climbing gym."}):
            profilerefresh.refresh_current("p_test00000001")
        # value identical -> _clean drops it; the field survives untouched
        self.assertEqual(self._profile()["how_we_met"],
                         "Through the climbing gym.")


class TestExploreMode(RefreshBase):
    def test_explore_launches_a_session_with_the_real_tools(self):
        launched = {}

        class FakeSessions:
            def launch(self, prompt, **kw):
                launched["prompt"] = prompt
                launched["meta"] = kw.get("meta")
                return "job_123"
        with mock.patch("server.session.sessions", FakeSessions()):
            out = profilerefresh.explore("p_test00000001")
        self.assertEqual(out, {"status": "ok", "job_id": "job_123"})
        self.assertEqual(launched["meta"]["person_id"], "p_test00000001")
        for tool in ("mcp__vira__find", "mcp__vira__mail_search",
                     "mcp__vira__update_person_profile"):
            self.assertIn(tool, launched["prompt"])
        self.assertIn("Casey Example", launched["prompt"])
        self.assertIn("2015-11-02", launched["prompt"])

    def test_the_write_tool_is_registered_and_marked_mutating(self):
        names = [n for n, *_ in viratools.TOOL_SPECS]
        self.assertIn("update_person_profile", names)
        self.assertIn("mcp__vira__update_person_profile",
                      viratools.WRITE_TOOLS)


class TestWriteTool(RefreshBase):
    def test_writes_by_person_id(self):
        out = viratools._update_person_profile_text(
            "p_test00000001", NEW_SUMMARY, "")
        self.assertIn("Profile refreshed", out)
        prof = self._profile()
        self.assertEqual(prof["relationship_summary"], NEW_SUMMARY)
        self.assertEqual(prof["last_refresh_reason"], "vira-refresh-explore")

    def test_resolves_an_unambiguous_name(self):
        out = viratools._update_person_profile_text(
            "Casey", NEW_SUMMARY, "Through the climbing gym.")
        self.assertIn("Profile refreshed", out)
        self.assertEqual(self._profile()["how_we_met"],
                         "Through the climbing gym.")

    def test_an_ambiguous_or_unknown_name_is_refused(self):
        out = viratools._update_person_profile_text(
            "Zebulon", NEW_SUMMARY, "")
        self.assertIn("No unique person", out)

    def test_an_empty_summary_is_refused(self):
        out = viratools._update_person_profile_text(
            "p_test00000001", "   ", "")
        self.assertIn("Refused", out)
        self.assertEqual(self._profile()["relationship_summary"],
                         "Casey is an old friend [2024-01-01].")


if __name__ == "__main__":
    unittest.main()
