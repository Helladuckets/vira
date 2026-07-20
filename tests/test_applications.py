"""Applications module: data.js ingest + uid dedupe, owner-state store,
LinkedIn-connections parsing, payload composition, and the apply prompt.

All fixtures are synthetic — no real teardown data, names, or contacts.

Run: .venv/bin/python -m unittest tests.test_applications
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import applications


TEARDOWN = {
    "meta": {"company": "Example Labs", "source": "example board"},
    "jobs": [
        {"title": "Platform Architect", "team": "Platform",
         "family": "Engineering", "locations": ["New York City, NY"],
         "seniority": "IC", "minYears": 8,
         "salaryMin": 300000, "salaryMax": 380000, "interval": "1 YEAR",
         "equity": True, "tags": ["ml/llm", "program-management"],
         "fit": 88, "bucket": "Domain moat", "reason": "platform + domain",
         "url": "https://job-boards.greenhouse.io/examplelabs/jobs/1234567",
         "apply": "https://job-boards.greenhouse.io/examplelabs/jobs/1234567",
         "blurb": "Build the platform."},
    ],
}

FRONTIER = {
    "meta": {"captured": "2026-07-13"},
    "jobs": [
        # duplicate of the teardown role (same board id) — must NOT win
        {"uid": "g-examplelabs-1234567", "company": "Example Labs",
         "title": "Platform Architect", "dept": "Platform",
         "function": "Engineering", "locations": ["New York City, NY"],
         "url": "https://job-boards.greenhouse.io/examplelabs/jobs/1234567",
         "apply": "https://job-boards.greenhouse.io/examplelabs/jobs/1234567"},
        # frontier-only role, unscored
        {"uid": "as-otherco-abcdefab-1111-2222-3333-444444444444",
         "company": "OtherCo", "title": "Ops Lead", "dept": "Ops",
         "function": "Operations", "locations": ["Remote"],
         "remote": "remote",
         "url": ("https://jobs.ashbyhq.com/otherco/"
                 "abcdefab-1111-2222-3333-444444444444"),
         "apply": ("https://jobs.ashbyhq.com/otherco/"
                   "abcdefab-1111-2222-3333-444444444444/application")},
    ],
}

CONNECTIONS_CSV = """Notes:
"Some preamble text from the export."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Avery,Fictional,https://example.com/in/avery,,Example Labs,Engineer,01 Jan 2026
Blake,Invented,https://example.com/in/blake,,Example Labs Ventures,Partner,02 Jan 2026
Casey,Madeup,https://example.com/in/casey,,Unrelated Co,Analyst,03 Jan 2026
"""


class ApplicationsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        td = root / "teardown-data.js"
        td.write_text("window.DATA=" + json.dumps(TEARDOWN))
        fr = root / "frontier-data.js"
        fr.write_text("window.DATA=" + json.dumps(FRONTIER))
        conns = root / "Connections.csv"
        conns.write_text(CONNECTIONS_CSV)
        self.sources = [
            {"slug": "example-teardown", "company": "Example Labs",
             "path": td},
            {"slug": "frontier", "company": None, "path": fr},
        ]
        self.universe = root / "analysis"          # empty by default
        (self.universe / "candidate-universe" / "role").mkdir(parents=True)
        patches = [
            mock.patch.object(applications, "sources",
                              lambda: self.sources),
            mock.patch.object(applications, "connections_csv",
                              lambda: conns),
            mock.patch.object(applications, "universe_dir",
                              lambda: self.universe),
            mock.patch.object(applications, "STORE",
                              root / "applications.json"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for cache, blank in ((applications._cache, {"key": None, "roles": None}),
                             (applications._universe_cache,
                              {"key": None, "roles": None}),
                             (applications._conn_cache,
                              {"mtime": None, "by_company": None})):
            cache.update(blank)
            self.addCleanup(lambda c=cache, b=blank: c.update(dict(b)))
        self.addCleanup(self.tmp.cleanup)

    def seed_universe(self):
        """Two universe roles: the teardown-known architect (scored, tier 1
        via final_tier override) and a universe-only untriaged role."""
        rdir = self.universe / "candidate-universe" / "role"
        (rdir / "g-examplelabs-1234567.json").write_text(json.dumps({
            "uid": "g-examplelabs-1234567", "company": "Example Labs",
            "title": "Platform Architect", "team": "Platform",
            "function": "Engineering", "seniority": "IC",
            "salaryMin": 300000, "salaryMax": 380000, "comp": "base",
            "locations": ["New York City, NY"], "tags": ["ml/llm"],
            "url": "https://job-boards.greenhouse.io/examplelabs/jobs/1234567",
            "blurb": "Build the platform.", "fit_old": 88,
            "served": "Platform Architect"}))
        (rdir / "g-examplelabs-777.json").write_text(json.dumps({
            "uid": "g-examplelabs-777", "company": "Example Labs",
            "title": "Ops Coordinator", "team": "Ops",
            "function": "Operations", "comp": "ote",
            "locations": ["Remote - US"], "fit_old": 41,
            "url": "https://job-boards.greenhouse.io/examplelabs/jobs/777"}))
        (self.universe / "v2-raw-scores.json").write_text(json.dumps([
            {"uid": "g-examplelabs-12345", "_fulluid": "g-examplelabs-1234567",
             "fit": 90, "tier": "2",
             "final_tier": "1", "lane": "A — deployer",
             "why_fit": "matches the build record",
             "lead_with": "the shipped systems",
             "caveat": "no formal SA title", "comp_note": "base band"},
        ]))
        (self.universe / "candidate-universe" / "manifest.json").write_text(
            json.dumps({"total": 2,
                        "all_uids": ["g-examplelabs-1234567",
                                     "g-examplelabs-777"]}))
        applications._universe_cache.update({"key": None, "roles": None})

    def seed_adjudication(self, shortlist=None):
        """Owner ruling fixture: OTE cut by comp, marketing cut by title,
        optional pinned shortlist."""
        rdir = self.universe / "candidate-universe" / "role"
        (rdir / "g-examplelabs-888.json").write_text(json.dumps({
            "uid": "g-examplelabs-888", "company": "Example Labs",
            "title": "Growth Marketing Manager", "team": "Marketing",
            "function": "Marketing & Comms", "comp": "base",
            "locations": ["New York City, NY"], "fit_old": 55,
            "url": "https://job-boards.greenhouse.io/examplelabs/jobs/888"}))
        (self.universe / "owner-adjudication.json").write_text(json.dumps({
            "shortlist": [{"uid": u} for u in (shortlist or [])],
            "cut": {"comp": ["ote"],
                    "title_patterns": ["\\bmarketing\\b"],
                    "reason_comp": "quota/commission — cut on the owner's call",
                    "reason_title": "selling/marketing — cut on the owner's call"},
        }))
        applications._universe_cache.update({"key": None, "roles": None})


class UidTest(unittest.TestCase):
    def test_anthropic_greenhouse(self):
        self.assertEqual(applications.role_uid(
            {"url": "https://job-boards.greenhouse.io/anthropic/jobs/55"}),
            "a-55")

    def test_other_greenhouse_org(self):
        self.assertEqual(applications.role_uid(
            {"url": "https://job-boards.greenhouse.io/examplelabs/jobs/9"}),
            "g-examplelabs-9")

    def test_openai_ashby(self):
        u = ("https://jobs.ashbyhq.com/openai/"
             "abcdefab-1111-2222-3333-444444444444")
        self.assertEqual(applications.role_uid({"url": u}),
                         "o-abcdefab-1111-2222-3333-444444444444")

    def test_frontier_uid_passthrough(self):
        self.assertEqual(applications.role_uid(
            {"uid": "a-5222180008", "url": "https://x"}), "a-5222180008")


class IngestTest(ApplicationsBase):
    def test_dedupe_prefers_teardown_record(self):
        roles, meta = applications.load_roles()
        uids = [r["uid"] for r in roles]
        self.assertEqual(len(uids), len(set(uids)))
        arch = next(r for r in roles if r["uid"] == "g-examplelabs-1234567")
        self.assertEqual(arch["fit"], 88)          # teardown record won
        self.assertEqual(arch["source"], "example-teardown")
        ops = next(r for r in roles if r["company"] == "OtherCo")
        self.assertIsNone(ops["fit"])
        srcs = {s["slug"]: s for s in meta["sources"]}
        self.assertTrue(srcs["example-teardown"]["ok"])
        self.assertEqual(srcs["frontier"]["new"], 1)

    def test_mtime_cache_refreshes(self):
        applications.load_roles()
        td = self.sources[0]["path"]
        data = json.loads(td.read_text().split("=", 1)[1])
        data["jobs"][0]["fit"] = 91
        td.write_text("window.DATA=" + json.dumps(data))
        roles, _ = applications.load_roles()
        arch = next(r for r in roles if r["uid"] == "g-examplelabs-1234567")
        self.assertEqual(arch["fit"], 91)


class StateTest(ApplicationsBase):
    def test_roundtrip(self):
        applications.update_state("g-examplelabs-1234567", starred=True)
        applications.update_state("g-examplelabs-1234567", status="applied",
                                  comment="spoke to the recruiter")
        st = applications.get_state()["g-examplelabs-1234567"]
        self.assertTrue(st["starred"])
        self.assertEqual(st["status"], "applied")
        self.assertEqual(len(st["comments"]), 1)

    def test_bad_status_rejected(self):
        with self.assertRaises(ValueError):
            applications.update_state("x", status="wishful")

    def test_apply_stamps_job(self):
        applications.update_state("x", job_id="job_123")
        st = applications.get_state()["x"]
        self.assertEqual(st["last_job"], "job_123")
        self.assertIn("applied_when", st)


class ConnectionsTest(ApplicationsBase):
    def test_preamble_skipped_and_matching(self):
        hits = applications.connections_for("Example Labs")
        names = sorted(h["name"] for h in hits)
        # loose contains-match picks up the ventures affiliate too
        self.assertEqual(names, ["Avery Fictional", "Blake Invented"])
        self.assertEqual(applications.connections_for("Nowhere"), [])


class ComposeTest(ApplicationsBase):
    def test_all_view_merges_state_and_companies(self):
        applications.update_state("g-examplelabs-1234567", starred=True)
        out = applications.compose(view="all")
        self.assertEqual(out["roles"][0]["uid"], "g-examplelabs-1234567")
        self.assertTrue(out["roles"][0]["starred"])
        self.assertIsNone(out["roles"][-1]["fit"])   # unscored sorts last
        comp = out["companies"]["Example Labs"]
        self.assertEqual(comp["roles"], 1)
        self.assertEqual(comp["connections"], 2)

    def test_company_filter(self):
        out = applications.compose(company="otherco", view="all")
        self.assertEqual(len(out["roles"]), 1)
        self.assertEqual(out["roles"][0]["company"], "OtherCo")

    def test_empty_universe_default_view(self):
        out = applications.compose()
        self.assertEqual(out["roles"], [])
        self.assertEqual(out["meta"]["universe"]["total"], 0)


class UniverseTest(ApplicationsBase):
    def setUp(self):
        super().setUp()
        self.seed_universe()

    def test_overlay_and_order(self):
        uni = applications.load_universe()
        self.assertEqual([r["uid"] for r in uni],
                         ["g-examplelabs-1234567", "g-examplelabs-777"])
        arch, ops = uni
        self.assertEqual(arch["fit"], 90)            # v2 score wins
        self.assertEqual(arch["tier"], "1")          # final_tier overrides
        self.assertEqual(arch["lane"], "A — deployer")
        # apply URL joined from the corpus record
        self.assertEqual(arch["apply_url"],
                         TEARDOWN["jobs"][0]["apply"])
        self.assertIsNone(ops["fit"])                # untriaged
        self.assertEqual(ops["fit_old"], 41)
        self.assertEqual(ops["tier"], "")
        self.assertEqual(ops["comp_kind"], "ote")

    def test_universe_is_default_view(self):
        out = applications.compose()
        self.assertEqual(len(out["roles"]), 2)
        self.assertTrue(all(r["in_universe"] for r in out["roles"]))
        self.assertEqual(out["meta"]["universe"]["scored"], 1)
        self.assertEqual(out["meta"]["universe"]["tier1"], 1)

    def test_all_view_appends_corpus_only(self):
        out = applications.compose(view="all")
        uids = [r["uid"] for r in out["roles"]]
        self.assertEqual(uids[:2],
                         ["g-examplelabs-1234567", "g-examplelabs-777"])
        rest = out["roles"][2:]
        self.assertTrue(rest)
        self.assertTrue(all(not r["in_universe"] for r in rest))

    def test_adjudication_pins_and_cuts(self):
        self.seed_adjudication(shortlist=["g-examplelabs-777"])
        uni = applications.load_universe()
        by = {r["uid"]: r for r in uni}
        # the pinned pick sorts first and can never be cut (it is OTE)
        self.assertEqual(uni[0]["uid"], "g-examplelabs-777")
        self.assertEqual(uni[0]["shortlist"], 1)
        self.assertEqual(uni[0]["cut"], "")
        # scored tier-1 role stays uncut, ranked after the picks
        self.assertEqual(uni[1]["uid"], "g-examplelabs-1234567")
        # marketing title cut by pattern, demoted to the bottom
        self.assertEqual(uni[-1]["uid"], "g-examplelabs-888")
        self.assertIn("marketing", by["g-examplelabs-888"]["cut"])

    def test_adjudication_cut_by_comp_without_shortlist(self):
        self.seed_adjudication()
        by = {r["uid"]: r for r in applications.load_universe()}
        self.assertIn("quota/commission", by["g-examplelabs-777"]["cut"])
        out = applications.compose()
        self.assertEqual(out["meta"]["universe"]["cut"], 2)
        self.assertEqual(out["meta"]["universe"]["shortlist"], 0)

    def test_apply_prompt_warns_on_cut_role(self):
        self.seed_adjudication()
        role = applications.find_role("g-examplelabs-777")
        p = applications.apply_prompt(role)
        self.assertIn("WARNING", p)
        self.assertIn("owner CUT", p)
        self.assertIn("quota/commission", p)

    def test_apply_prompt_carries_dossier(self):
        role = applications.find_role("g-examplelabs-1234567")
        self.assertEqual(role["source"], "universe")
        p = applications.apply_prompt(role)
        self.assertIn("DOSSIER READ", p)
        self.assertIn("the shipped systems", p)
        self.assertIn("no formal SA title", p)


class PromptTest(ApplicationsBase):
    def test_apply_prompt_shape(self):
        roles, _ = applications.load_roles()
        role = roles[0]
        p = applications.apply_prompt(role, note="prioritize the letter")
        self.assertIn("application-package", p)
        self.assertIn(role["uid"], p)
        self.assertIn("never submit", p.lower())
        self.assertIn("prioritize the letter", p)
        body = json.loads(p.split("ROLE:\n", 1)[1].rsplit("\n\nOwner", 1)[0])
        self.assertEqual(body["title"], role["title"])


class ApplyRouteTest(ApplicationsBase):
    """The apply-prompt endpoint and note/model passthrough on apply."""

    def setUp(self):
        super().setUp()
        self.seed_universe()
        from server import main
        self.main = main

    def test_apply_prompt_returns_prompt_and_cwd(self):
        out = self.main.api_applications_apply_prompt(
            "g-examplelabs-1234567",
            self.main.AppPromptReq(note="lead with the platform work"))
        self.assertIn("Platform Architect", out["prompt"])
        self.assertIn("lead with the platform work", out["prompt"])
        self.assertEqual(out["cwd"], str(applications.self_record()))

    def test_apply_prompt_unknown_uid_404(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self.main.api_applications_apply_prompt(
                "g-nosuch-role", self.main.AppPromptReq())
        self.assertEqual(ctx.exception.status_code, 404)

    def test_apply_prompt_writes_no_state(self):
        self.main.api_applications_apply_prompt(
            "g-examplelabs-1234567", self.main.AppPromptReq())
        self.assertEqual(applications.get_state(), {})

    def test_apply_passes_note_and_model_to_launch(self):
        calls = {}

        def fake_launch(prompt, cwd, permission_mode, model,
                        publish_plan, idea_id, mode):
            calls.update(prompt=prompt, cwd=cwd, model=model, mode=mode,
                         permission_mode=permission_mode)
            return "job-123"

        with mock.patch.object(self.main.jobs, "launch", fake_launch):
            out = self.main.api_applications_apply(
                "g-examplelabs-1234567",
                self.main.AppApplyReq(note="emphasize the caveat",
                                      model="opus"))
        self.assertEqual(out["job_id"], "job-123")
        self.assertIn("emphasize the caveat", calls["prompt"])
        self.assertEqual(calls["model"], "opus")
        self.assertEqual(calls["mode"], "interactive")
        self.assertIsNone(calls["permission_mode"])
        self.assertEqual(
            applications.get_state()["g-examplelabs-1234567"]["last_job"],
            "job-123")


if __name__ == "__main__":
    unittest.main()
