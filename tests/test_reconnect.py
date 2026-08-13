"""Pivot reconnect tests: pivot-picture derivation from the Applications
universe, dormancy filtering, deterministic scoring + reasons, the
LinkedIn-overlay freshness rule, the AI curation pass + fallback, dismiss
+ re-arm, and the radar.compose() passthrough.

All fixture data is synthetic and PII-safe. Run:
  .venv/bin/python -m unittest discover tests
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from server import applications, radar, reconnect

NOW = datetime.now()


def _ago(days):
    return (NOW - timedelta(days=days)).isoformat()


UNIVERSE_ROLES = [
    {"uid": "u1", "company": "Lumon Industries",
     "title": "Forward Deployed Engineer", "tier": "1",
     "lane": "forward deployed", "why_fit": "ships production agents",
     "lead_with": "infrastructure background", "cut": "", "fit": 90},
    {"uid": "u2", "company": "Lumon Industries", "title": "Applied AI Lead",
     "tier": "1", "lane": "applied ai", "why_fit": "owns applied roadmap",
     "lead_with": "", "cut": "", "fit": 80},
    {"uid": "u3", "company": "Initech", "title": "Sales Engineer",
     "tier": "2", "lane": "sales", "why_fit": "sells the platform",
     "lead_with": "", "cut": "no sales — owner's rule", "fit": 95},
]

RECON_CRM = {
    "people": [
        {"id": "p1", "name": "Irving Vale", "profile_tier": "active",
         "handles": {"imessage": []}, "activity": {"imsg_last": _ago(60)}},
        {"id": "p2", "name": "Lumon Direct", "profile_tier": "active",
         "handles": {"imessage": []}, "activity": {"imsg_last": _ago(500)}},
        {"id": "p3", "name": "Bo Reyes", "profile_tier": "active",
         "handles": {"imessage": []}, "activity": {"imsg_last": _ago(90)}},
        {"id": "p4", "name": "Keyword Only", "profile_tier": "active",
         "handles": {"imessage": []}, "activity": {"imsg_last": _ago(90)}},
        {"id": "p5", "name": "Director Only", "profile_tier": "active",
         "handles": {"imessage": []}, "activity": {"imsg_last": _ago(200)}},
        {"id": "p6", "name": "Fresh Fresh", "profile_tier": "active",
         "handles": {"imessage": []}, "activity": {"imsg_last": _ago(10)}},
        {"id": "p7", "name": "(unidentified)", "profile_tier": "active",
         "handles": {"imessage": []}, "activity": {}},
        {"id": "p8", "name": "Bank Alerts", "profile_tier": "active",
         "class_hint": "company", "handles": {"imessage": []},
         "activity": {}},
        {"id": "p9", "name": "No Date Ever", "profile_tier": "active",
         "handles": {"imessage": []}, "activity": {}},
        {"id": "p10", "name": "No Tier", "handles": {"imessage": []},
         "activity": {}},
    ],
    "by_id": {},
    "profiles": {
        "p4": {"relationship_summary": "Leads forward deployed "
                                       "engineering at a startup, ships "
                                       "production agents"},
    },
}
RECON_CRM["by_id"] = {p["id"]: p for p in RECON_CRM["people"]}

MASTER = {
    "p1": {"company": "Old Corp", "title": "Something"},
    "p2": {"company": "Lumon Industries", "title": "Someone at Lumon"},
    "p3": {"company": "", "title": ""},
    "p4": {"company": "Acme Startup", "title": "Engineer"},
    "p5": {"company": "Acme Corp", "title": "Director of Ops"},
    "p9": {"company": "", "title": ""},
}

CONNECTIONS = {
    "lumon industries": [
        {"name": "Irving Vale", "company": "Lumon Industries",
         "position": "VP of Platform"},
        {"name": "B Reyes", "company": "Lumon Industries",
         "position": "Director"},
    ],
}


def _get_person(pid):
    return {"master": MASTER.get(pid, {})}


class PivotPictureTests(unittest.TestCase):
    def test_derives_companies_lanes_keywords_from_uncut_roles(self):
        with mock.patch.object(applications, "load_universe",
                               return_value=UNIVERSE_ROLES):
            picture = reconnect.pivot_picture()
        self.assertIsNotNone(picture)
        self.assertIn("lumon industries", picture["companies"])
        self.assertNotIn("initech", picture["companies"])   # cut, no signal
        info = picture["companies"]["lumon industries"]
        self.assertEqual(info["tier1"], 2)
        self.assertNotIn("shortlist", info)   # picks retired
        # the roles the search is about, tier-1 and fit-ordered
        self.assertEqual(picture["top_titles"],
                         ["Forward Deployed Engineer",
                          "Applied AI Lead"])
        self.assertIn("forward deployed", picture["lanes"])
        self.assertIn("applied ai", picture["lanes"])
        self.assertNotIn("sales", picture["lanes"])          # cut role only
        self.assertIn("forward", picture["keywords"])
        self.assertIn("deployed", picture["keywords"])

    def test_empty_universe_is_none(self):
        with mock.patch.object(applications, "load_universe",
                               return_value=[]):
            self.assertIsNone(reconnect.pivot_picture())

    def test_universe_error_is_none(self):
        with mock.patch.object(applications, "load_universe",
                               side_effect=RuntimeError("boom")):
            self.assertIsNone(reconnect.pivot_picture())

    def test_all_cut_universe_is_none(self):
        cut_only = [dict(r, cut="cut lane") for r in UNIVERSE_ROLES]
        with mock.patch.object(applications, "load_universe",
                               return_value=cut_only):
            self.assertIsNone(reconnect.pivot_picture())


class DormancyTests(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch("server.reconnect.crm._load",
                      return_value=RECON_CRM),
            mock.patch("server.reconnect.brief._live_imsg_last",
                      return_value={}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_dormancy_filter(self):
        pids = {c["pid"] for c in reconnect.dormant_candidates()}
        self.assertIn("p1", pids)     # 60 days
        self.assertIn("p2", pids)     # 500 days
        self.assertIn("p9", pids)     # no date at all — kept with days=None
        self.assertNotIn("p6", pids)  # 10 days — too recent
        self.assertNotIn("p7", pids)  # (unidentified) placeholder
        self.assertNotIn("p8", pids)  # class_hint company
        self.assertNotIn("p10", pids)  # no tier at all
        by_pid = {c["pid"]: c for c in reconnect.dormant_candidates()}
        self.assertIsNone(by_pid["p9"]["days"])

    def test_live_imsg_overlay_error_degrades(self):
        with mock.patch("server.reconnect.brief._live_imsg_last",
                        side_effect=RuntimeError("no chat.db")):
            pids = {c["pid"] for c in reconnect.dormant_candidates()}
        self.assertIn("p1", pids)


class ScoringTests(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch("server.reconnect.crm._load",
                      return_value=RECON_CRM),
            mock.patch("server.reconnect.crm.get_person",
                      side_effect=_get_person),
            mock.patch("server.reconnect.brief._live_imsg_last",
                      return_value={}),
            mock.patch.object(applications, "connections_by_company",
                             return_value=CONNECTIONS),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with mock.patch.object(applications, "load_universe",
                               return_value=UNIVERSE_ROLES):
            self.picture = reconnect.pivot_picture()

    def test_company_match_outranks_keyword_only(self):
        rows = {r["pid"]: r for r in reconnect.score_candidates(self.picture)}
        self.assertIn("p1", rows)
        self.assertIn("p4", rows)
        self.assertGreater(rows["p1"]["score"], rows["p4"]["score"])
        self.assertTrue(any("Lumon Industries" in r
                            for r in rows["p1"]["reasons"]))
        self.assertTrue(any("target company" in r
                            for r in rows["p1"]["reasons"]))
        self.assertTrue(any("pivot ground" in r
                            for r in rows["p4"]["reasons"]))

    def test_linkedin_overlay_wins_over_stale_master(self):
        rows = {r["pid"]: r for r in reconnect.score_candidates(self.picture)}
        self.assertEqual(rows["p1"]["company"], "Lumon Industries")
        self.assertEqual(rows["p1"]["company_source"], "linkedin")

    def test_near_miss_linkedin_name_does_not_match(self):
        rows = {r["pid"]: r for r in reconnect.score_candidates(self.picture)}
        self.assertNotIn("p3", rows)   # "Bo Reyes" != "B Reyes", score 0

    def test_seniority_only_falls_below_floor(self):
        rows = {r["pid"]: r for r in reconnect.score_candidates(self.picture)}
        self.assertNotIn("p5", rows)

    def test_crm_master_company_match_when_no_linkedin_row(self):
        rows = {r["pid"]: r for r in reconnect.score_candidates(self.picture)}
        self.assertEqual(rows["p2"]["company_source"], "crm")
        self.assertTrue(any("dormant" in r for r in rows["p2"]["reasons"]))


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "reconnect.json"
        patches = [
            mock.patch.object(reconnect, "STORE", self.store),
            mock.patch("server.reconnect.crm._load",
                      return_value=RECON_CRM),
            mock.patch("server.reconnect.crm.get_person",
                      side_effect=_get_person),
            mock.patch("server.reconnect.brief._live_imsg_last",
                      return_value={}),
            mock.patch.object(applications, "connections_by_company",
                             return_value=CONNECTIONS),
            mock.patch.object(applications, "load_universe",
                             return_value=UNIVERSE_ROLES),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_refresh_curates_top_targets_and_drops_unknown_pid(self):
        payload = json.dumps({"targets": [
            {"person_id": "p1", "why": "runs platform at Lumon",
             "opener": "hey, saw Lumon is hiring — how's it going?"},
            {"person_id": "p999", "why": "not real", "opener": "x"},
            {"person_id": "p4", "why": "deep in forward deployed work",
             "opener": "been a while — how's the new gig?"},
        ]})
        with mock.patch("server.reconnect.suggest.complete",
                        return_value=payload):
            targets = reconnect.refresh()
        self.assertEqual(len(targets), 2)
        pids = {t["person_id"] for t in targets}
        self.assertEqual(pids, {"p1", "p4"})
        for t in targets:
            self.assertTrue(t["curated"])
            self.assertTrue(t["key"].startswith(f"rec:{t['person_id']}:"))
        stored = json.loads(self.store.read_text(encoding="utf-8"))
        self.assertEqual(len(stored["targets"]), 2)
        self.assertTrue(stored["curated"])
        self.assertIn("Lumon Industries", stored["picture"]["companies"])

    def test_fallback_on_model_failure(self):
        with mock.patch("server.reconnect.suggest.complete",
                        side_effect=RuntimeError("offline")):
            targets = reconnect.refresh()
        self.assertTrue(targets)
        self.assertLessEqual(len(targets), reconnect.TOP_N)
        self.assertTrue(all(not t["curated"] for t in targets))
        self.assertTrue(all(t["why"] for t in targets))  # never empty

    def test_dormant_without_universe(self):
        with mock.patch.object(applications, "load_universe",
                               return_value=[]):
            targets = reconnect.refresh()
        self.assertEqual(targets, [])
        out = reconnect.list_targets()
        self.assertEqual(out["targets"], [])
        self.assertEqual(out["picture"], {})
        self.assertIsNotNone(out["generated"])


class DismissTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "reconnect.json"
        p = mock.patch.object(reconnect, "STORE", self.store)
        p.start()
        self.addCleanup(p.stop)

    def _seed(self, targets):
        reconnect._write({**reconnect._blank(), "generated": "x",
                          "targets": targets})

    def test_dismiss_hides_and_restore_reveals(self):
        self._seed([{"key": "rec:p1:2026-01-01", "person_id": "p1",
                     "name": "Irving Vale", "why": "", "opener": ""}])
        reconnect.dismiss("rec:p1:2026-01-01")
        self.assertEqual(reconnect.list_targets()["targets"], [])
        reconnect.dismiss("rec:p1:2026-01-01", restore=True)
        self.assertEqual(len(reconnect.list_targets()["targets"]), 1)

    def test_rearm_on_fresh_last_contact(self):
        self._seed([{"key": "rec:p1:2026-01-01", "person_id": "p1",
                     "name": "Irving Vale", "why": "", "opener": ""}])
        reconnect.dismiss("rec:p1:2026-01-01")
        self.assertEqual(reconnect.list_targets()["targets"], [])
        # the relationship woke up and went quiet again -> a fresh key
        self._seed([{"key": "rec:p1:2026-03-15", "person_id": "p1",
                     "name": "Irving Vale", "why": "", "opener": ""}])
        keys = [t["key"] for t in reconnect.list_targets()["targets"]]
        self.assertEqual(keys, ["rec:p1:2026-03-15"])


class ComposePassthroughTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patches = [
            mock.patch.object(radar, "STORE",
                             Path(self.tmp.name) / "radar-groupings.json"),
            mock.patch.object(radar, "LEGACY",
                             Path(self.tmp.name) / "radar-intros.json"),
            mock.patch("server.radar.crm._load",
                      return_value={"people": [], "by_id": {},
                                    "profiles": {}}),
            mock.patch("server.radar.brief._unreplied_imessages",
                      return_value=[]),
            mock.patch("server.radar.brief._going_quiet", return_value=[]),
            mock.patch("server.radar.brief._open_loops", return_value=[]),
            mock.patch("server.radar.brief._calendar",
                      side_effect=RuntimeError("no calendar store")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_compose_carries_reconnect(self):
        fake = {"generated": "now", "targets": [{"key": "rec:p1:x"}],
               "picture": {"companies": ["Lumon Industries"]},
               "curated": True}
        with mock.patch.object(reconnect, "list_targets",
                               return_value=fake):
            out = radar.compose()
        self.assertEqual(out["reconnect"], fake)

    def test_compose_survives_reconnect_failure(self):
        with mock.patch.object(reconnect, "list_targets",
                               side_effect=RuntimeError("store corrupt")):
            out = radar.compose()
        self.assertEqual(out["reconnect"],
                         {"generated": None, "targets": [], "picture": {}})


if __name__ == "__main__":
    unittest.main()
