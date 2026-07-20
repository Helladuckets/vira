"""Job-boards layer: fetch normalization, the NYC-or-remote location rule,
adjudication cuts, poll diff/state (new / closed / re-listed), notify
batching + per-uid dedupe, registry add, and the score prompt.

All fixtures are synthetic — no real roles, companies beyond the public
board names, or personal data.

Run: .venv/bin/python -m unittest tests.test_jobboards
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import jobboards


GH_BOARD = {"company": "Example Labs", "ats": "greenhouse", "slug": "exlabs"}

GH_PAYLOAD = {
    "jobs": [
        {"id": 111, "title": "Deployment Strategist",
         "absolute_url": "https://job-boards.greenhouse.io/exlabs/jobs/111",
         "location": {"name": "New York City, NY"},
         "departments": [{"name": "Deployment"}],
         "content": "Own customer outcomes end to end.",
         "updated_at": "2026-07-01T00:00:00Z"},
        {"id": 222, "title": "Enterprise Account Executive",
         "absolute_url": "https://job-boards.greenhouse.io/exlabs/jobs/222",
         "location": {"name": "New York City, NY"},
         "departments": [{"name": "Sales"}],
         "content": "Quota carrying. On Target Earnings apply.",
         "updated_at": "2026-07-01T00:00:00Z"},
    ],
}

ASHBY_BOARD = {"company": "OtherCo", "ats": "ashby", "slug": "otherco"}

ASHBY_PAYLOAD = {
    "jobs": [
        {"id": "abcdefab-1111-2222-3333-444444444444",
         "title": "Field Engineer", "department": "Field",
         "location": "Seoul", "secondaryLocations": [],
         "isRemote": True, "isListed": True,
         "descriptionHtml": "<p>Work with APAC customers.</p>",
         "jobUrl": "https://jobs.ashbyhq.com/otherco/abcdefab",
         "publishedAt": "2026-07-10T00:00:00Z"},
    ],
}

ADJ = {
    "shortlist": {},
    "cut_comp": {"ote"},
    "cut_titles": [__import__("re").compile(
        r"account executive|\bsales\b", __import__("re").I)],
    "reason_comp": "quota comp — cut",
    "reason_title": "selling role — cut",
}


class NormAndEligibility(unittest.TestCase):

    def test_greenhouse_parse_and_comp(self):
        with mock.patch.object(jobboards, "_get", return_value=GH_PAYLOAD):
            out = jobboards.fetch_greenhouse(GH_BOARD)
        self.assertEqual(len(out), 2)
        strat, ae = out
        self.assertEqual(strat["uid"], "g-exlabs-111")
        self.assertEqual(strat["company"], "Example Labs")
        self.assertIn("New York City, NY", strat["locations"])
        self.assertEqual(ae["comp"], "ote")     # OTE marker in the JD
        self.assertEqual(strat["comp"], "")     # no salary, no marker

    def test_ashby_remote_tag_appended(self):
        with mock.patch.object(jobboards, "_get", return_value=ASHBY_PAYLOAD):
            out = jobboards.fetch_ashby(ASHBY_BOARD)
        self.assertEqual(out[0]["uid"],
                         "as-otherco-abcdefab-1111-2222-3333-444444444444")
        self.assertIn("Remote", out[0]["locations"])

    def test_location_rule(self):
        ok = {"locations": ["New York City, NY"]}
        bare_remote = {"locations": ["Remote"]}
        us_remote = {"locations": ["San Francisco", "Remote"]}
        foreign_remote = {"locations": ["Seoul", "Remote"]}
        foreign_only = {"locations": ["London"]}
        eu_remote = {"locations": ["Remote - Europe"]}
        self.assertTrue(jobboards.eligible_location(ok))
        self.assertTrue(jobboards.eligible_location(bare_remote))
        self.assertTrue(jobboards.eligible_location(us_remote))
        self.assertFalse(jobboards.eligible_location(foreign_remote))
        self.assertFalse(jobboards.eligible_location(foreign_only))
        self.assertFalse(jobboards.eligible_location(eu_remote))

    def test_adjudication_cut_by_title_and_comp_never_function(self):
        strat = {"uid": "x1", "title": "Deployment Strategist",
                 "function": "Sales & Go-To-Market", "comp": "",
                 "locations": ["New York City, NY"]}
        ae = {"uid": "x2", "title": "Enterprise Account Executive",
              "comp": "", "locations": ["New York City, NY"]}
        ote = {"uid": "x3", "title": "Customer Success Manager",
               "comp": "ote", "locations": ["New York City, NY"]}
        jobboards.evaluate(strat, ADJ)
        jobboards.evaluate(ae, ADJ)
        jobboards.evaluate(ote, ADJ)
        self.assertEqual(strat["cut"], "")   # GTM function label never cuts
        self.assertTrue(strat["eligible"])
        self.assertEqual(ae["cut"], "selling role — cut")
        self.assertEqual(ote["cut"], "quota comp — cut")


class PollDiffAndNotify(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "boards.json").write_text(json.dumps(
            {"boards": [dict(GH_BOARD)]}))
        patches = [
            mock.patch.object(jobboards, "boards_dir",
                              return_value=self.dir),
            mock.patch.object(jobboards, "_adjudication",
                              return_value=ADJ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.tmp2 = self.tmp   # keep alive
        self.addCleanup(self.tmp.cleanup)

    def _poll(self, payload, notify_ret=True):
        sent = []

        def fake_ping(text, key=None):
            sent.append((text, key))
            return notify_ret
        with mock.patch.object(jobboards, "_get", return_value=payload), \
                mock.patch("server.notify.agent_ping", fake_ping):
            r = jobboards.poll_once()
        return r, sent

    def test_new_then_stable_then_closed(self):
        r1, sent1 = self._poll(GH_PAYLOAD)
        self.assertEqual(r1["new"], 2)
        self.assertEqual(r1["eligible_new"], 1)   # the AE is cut
        self.assertEqual(len(sent1), 1)
        self.assertIn("Deployment Strategist", sent1[0][0])
        self.assertIn("(NYC)", sent1[0][0])

        # second poll: nothing new, nothing re-notified
        r2, sent2 = self._poll(GH_PAYLOAD)
        self.assertEqual(r2["new"], 0)
        self.assertEqual(sent2, [])

        # the strategist vanishes from the board -> closed, kept in snapshot
        one = {"jobs": [GH_PAYLOAD["jobs"][1]]}
        r3, _ = self._poll(one)
        self.assertEqual(r3["closed"], 1)
        snap = json.loads((self.dir / "snapshot.json").read_text())
        self.assertTrue(snap["roles"]["g-exlabs-111"].get("closed"))

        # it comes back -> reopened, but NOT re-notified (state remembers)
        r4, sent4 = self._poll(GH_PAYLOAD)
        self.assertEqual(r4["new"], 0)
        self.assertEqual(sent4, [])
        snap = json.loads((self.dir / "snapshot.json").read_text())
        self.assertFalse(snap["roles"]["g-exlabs-111"].get("closed"))

    def test_failed_ping_leaves_undedupe(self):
        _, sent1 = self._poll(GH_PAYLOAD, notify_ret=False)
        self.assertEqual(len(sent1), 1)
        # ping failed -> not marked notified -> next poll retries
        _, sent2 = self._poll(GH_PAYLOAD, notify_ret=True)
        self.assertEqual(len(sent2), 1)

    def test_board_error_never_closes_roles(self):
        self._poll(GH_PAYLOAD)

        def boom(url, **kw):
            raise RuntimeError("board down")
        with mock.patch.object(jobboards, "_get", side_effect=boom), \
                mock.patch("server.notify.agent_ping", lambda *a, **k: True):
            r = jobboards.poll_once()
        self.assertEqual(r["closed"], 0)
        snap = json.loads((self.dir / "snapshot.json").read_text())
        self.assertFalse(snap["roles"]["g-exlabs-111"].get("closed"))
        bid = jobboards._board_id(GH_BOARD)
        self.assertFalse(snap["boards"][bid]["ok"])

    def test_registry_add_and_validation(self):
        reg = jobboards.add_board("NewCo", "ashby", slug="newco")
        self.assertEqual(len(reg["boards"]), 2)
        with self.assertRaises(ValueError):
            jobboards.add_board("NewCo", "ashby", slug="newco")  # dupe
        with self.assertRaises(ValueError):
            jobboards.add_board("X", "nonsense", slug="x")
        with self.assertRaises(ValueError):
            jobboards.add_board("X", "greenhouse")               # no slug
        with self.assertRaises(ValueError):
            jobboards.add_board("X", "google")                   # no query

    def test_score_prompt_lists_unscored_eligible(self):
        self._poll(GH_PAYLOAD)
        with mock.patch.object(jobboards, "_scored_uids",
                               return_value=set()):
            prompt, n = jobboards.score_prompt()
        self.assertEqual(n, 1)                    # AE is cut, strategist in
        self.assertIn("g-exlabs-111", prompt)
        self.assertIn("TWO-SCORE", prompt)
        with mock.patch.object(jobboards, "_scored_uids",
                               return_value={"g-exlabs-111"}):
            _, n2 = jobboards.score_prompt()
        self.assertEqual(n2, 0)

    def test_status_counts(self):
        self._poll(GH_PAYLOAD)
        with mock.patch.object(jobboards, "_scored_uids",
                               return_value=set()):
            s = jobboards.status()
        self.assertEqual(s["registered"], 1)
        self.assertEqual(s["roles_open"], 2)
        self.assertEqual(s["eligible"], 1)
        self.assertEqual(s["fresh"], 2)
        self.assertEqual(s["unscored_eligible"], 1)


if __name__ == "__main__":
    unittest.main()
