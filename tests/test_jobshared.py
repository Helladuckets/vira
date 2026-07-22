"""Job-hunt shared-logic tests: cut parity (the same adjudication input
produces identical decisions through applications._apply_adjudication
and jobboards.evaluate), uid parity (role_uid over a posting URL equals
the fetcher-side board_uid for each ATS, with the frontier specials
pinned), and score-file loading (glob + (uid, _fulluid) double-index).

Run: .venv/bin/python -m unittest tests.test_jobshared
"""
import json
import re
import tempfile
import unittest
from pathlib import Path

from server import applications, jobboards, jobshared

ADJ = {
    "shortlist": {"pick-1": 0},
    "cut_comp": {"ote"},
    "cut_titles": [re.compile(r"account executive|\bsales\b", re.I)],
    "reason_comp": "quota comp — cut",
    "reason_title": "selling role — cut",
}


class CutParityTests(unittest.TestCase):
    """One adjudication, two call sites, identical decisions."""

    def _both(self, title, comp):
        """(applications cut, jobboards cut) for the same role facts."""
        role = {"uid": "x", "title": title, "comp_kind": comp,
                "shortlist": 0, "cut": ""}
        applications._apply_adjudication(role, ADJ)
        rec = {"uid": "x", "title": title, "comp": comp,
               "locations": ["New York City, NY"]}
        jobboards.evaluate(rec, ADJ)
        return role["cut"], rec["cut"]

    def test_ote_cut_matches(self):
        a, b = self._both("Customer Success Manager", "ote")
        self.assertEqual(a, "quota comp — cut")
        self.assertEqual(a, b)

    def test_title_cut_matches(self):
        a, b = self._both("Enterprise Account Executive", "")
        self.assertEqual(a, "selling role — cut")
        self.assertEqual(a, b)

    def test_survivor_matches(self):
        a, b = self._both("Deployment Strategist", "base")
        self.assertEqual(a, "")
        self.assertEqual(a, b)

    def test_comp_cut_outranks_title(self):
        a, b = self._both("Sales Engineer", "ote")
        self.assertEqual(a, "quota comp — cut")   # comp checked first
        self.assertEqual(a, b)

    def test_no_adjudication_never_cuts(self):
        self.assertEqual(jobshared.cut_reason("ote", "Sales", None), "")

    def test_pick_never_cut_applications_side(self):
        role = {"uid": "pick-1", "title": "Sales Account Executive",
                "comp_kind": "ote", "shortlist": 0, "cut": ""}
        applications._apply_adjudication(role, ADJ)
        self.assertEqual(role["shortlist"], 1)
        self.assertEqual(role["cut"], "")


class UidParityTests(unittest.TestCase):
    """role_uid(url) == the fetcher's board_uid for each ATS example."""

    def test_greenhouse_parity(self):
        url = "https://boards.greenhouse.io/scale/jobs/4471234"
        self.assertEqual(applications.role_uid({"url": url}),
                         jobshared.board_uid("greenhouse", "4471234",
                                             "scale"))
        self.assertEqual(applications.role_uid({"url": url}),
                         "g-scale-4471234")

    def test_ashby_parity(self):
        uuid = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
        url = f"https://jobs.ashbyhq.com/cursor/{uuid}"
        self.assertEqual(applications.role_uid({"url": url}),
                         jobshared.board_uid("ashby", uuid, "cursor"))
        self.assertEqual(applications.role_uid({"url": url}),
                         f"as-cursor-{uuid}")

    def test_frontier_specials_pinned(self):
        """The teardown corpora's a-/o- spellings hold on the URL side;
        the fetcher side stays generic (state-key stability) — the
        asymmetry is deliberate and pinned here."""
        gh = "https://boards.greenhouse.io/anthropic/jobs/999001"
        self.assertEqual(applications.role_uid({"url": gh}), "a-999001")
        self.assertEqual(
            jobshared.board_uid("greenhouse", "999001", "anthropic"),
            "g-anthropic-999001")
        uuid = "11111111-2222-3333-4444-555555555555"
        oa = f"https://jobs.ashbyhq.com/openai/{uuid}"
        self.assertEqual(applications.role_uid({"url": oa}), f"o-{uuid}")
        self.assertEqual(jobshared.board_uid("ashby", uuid, "openai"),
                         f"as-openai-{uuid}")

    def test_query_boards_carry_no_org(self):
        self.assertEqual(jobshared.board_uid("microsoft", "18773301"),
                         "ms-18773301")
        self.assertEqual(jobshared.board_uid("google", "74022"), "gg-74022")

    def test_lever_fetcher_shape(self):
        # role_uid has no lever URL branch (falls to u-); the fetcher
        # shape is pinned so a future lever URL branch matches it
        self.assertEqual(
            jobshared.board_uid("lever", "abc-123", "paradigm"),
            "lv-paradigm-abc-123")

    def test_uid_passthrough_wins(self):
        self.assertEqual(applications.role_uid(
            {"uid": "a-1", "url": "https://boards.greenhouse.io/x/jobs/2"}),
            "a-1")

    def test_unknown_url_falls_back(self):
        self.assertIsNone(jobshared.url_uid("https://example.com/job/1"))
        self.assertTrue(applications.role_uid(
            {"url": "https://example.com/job/1"}).startswith("u-"))
        self.assertTrue(applications.role_uid(
            {"title": "Some Role"}).startswith("t-"))

    def test_ats_kinds_derive_from_the_table(self):
        self.assertEqual(jobboards.ATS_KINDS,
                         ("greenhouse", "ashby", "lever", "microsoft",
                          "google", "manual"))


class LoadScoresTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.udir = Path(self.tmp.name)

    def _write(self, name, entries):
        (self.udir / name).write_text(json.dumps(entries))

    def test_double_index_and_multi_file(self):
        self._write("v2-raw-scores.json",
                    [{"uid": "a-1", "fit": 88},
                     {"uid": "as-org-1234", "_fulluid":
                      "as-org-12345678-1234-1234-1234-123456789012",
                      "fit": 71}])
        self._write("d6-raw-scores.json", [{"uid": "g-x-9", "fit": 60}])
        scores = jobshared.load_scores(self.udir)
        self.assertEqual(scores["a-1"]["fit"], 88)
        self.assertEqual(scores["g-x-9"]["fit"], 60)
        # truncated uid AND full board uuid both resolve the same entry
        self.assertIs(scores["as-org-1234"],
                      scores["as-org-12345678-1234-1234-1234-123456789012"])

    def test_later_file_wins_collisions(self):
        # sorted glob order: d6 < v2, so v2 (the adjudicated repass)
        # overwrites a d6 row for the same uid
        self._write("d6-raw-scores.json", [{"uid": "a-1", "fit": 10}])
        self._write("v2-raw-scores.json", [{"uid": "a-1", "fit": 90}])
        self.assertEqual(jobshared.load_scores(self.udir)["a-1"]["fit"], 90)

    def test_corrupt_file_skipped(self):
        (self.udir / "bad-raw-scores.json").write_text("{nope")
        self._write("v2-raw-scores.json", [{"uid": "a-1", "fit": 88}])
        self.assertEqual(set(jobshared.load_scores(self.udir)), {"a-1"})

    def test_empty_dir(self):
        self.assertEqual(jobshared.load_scores(self.udir), {})

    def test_scored_uids_reads_through_shared_loader(self):
        self._write("v2-raw-scores.json",
                    [{"uid": "a-1", "_fulluid": "a-1-full"}])
        from unittest import mock
        with mock.patch.object(applications, "universe_dir",
                               return_value=self.udir):
            self.assertEqual(jobboards._scored_uids(), {"a-1", "a-1-full"})


class NowIsoTests(unittest.TestCase):
    def test_shape_and_both_call_sites(self):
        pat = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$"
        self.assertRegex(jobshared.now_iso(), pat)
        self.assertRegex(applications._now(), pat)
        self.assertRegex(jobboards._now(), pat)


if __name__ == "__main__":
    unittest.main()
