"""Applications module: data.js ingest + uid dedupe, owner-state store,
LinkedIn-connections parsing, payload composition, and the apply prompt.

All fixtures are synthetic — no real teardown data, names, or contacts.

Run: .venv/bin/python -m unittest tests.test_applications
"""
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from server import applicationmap, applications, joblog, jobboards


def fresh_seen():
    """A sighting inside jobboards.VERIFY_DAYS on any run date. A literal
    date here is a time bomb: availability degrades a stale last_seen to
    `unverified`, so hardcoded sightings rotted 48 hours after they were
    written."""
    return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


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
         "blurb": "Build the platform.",
         "jd": ("About the role. Advise customers and build the platform. "
                "Responsibilities. Design scalable architectures, deploy "
                "customer systems, and develop evaluation frameworks for "
                "safe and reliable production use.")},
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

# The catalog computes location eligibility through
# jobboards.location_rule(), which reads the RUNNING machine's config —
# pinned here so eligibility assertions test the code, not this Mac.
NYC_CFG = {"applications_locations": ["New York", "NYC"],
           "applications_remote_ok": True}

# bound BEFORE the base class patches the name — _nyc_rule replaces
# jobboards.location_rule, so calling it through the module would recurse
_real_location_rule = jobboards.location_rule


def _nyc_rule():
    with mock.patch.object(jobboards.settings, "raw",
                           return_value=NYC_CFG):
        return _real_location_rule()


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
        self.self_record = root / "self"
        (self.self_record / "canon").mkdir(parents=True)
        (self.self_record / "canon" / "MASTER_HISTORY.md").write_text(
            "## Career chronology\n\n"
            "- Led cross-functional platform programs through ambiguity.\n\n"
            "# Endnotes\n\n"
            "[^1]: Built safe production systems and reusable platforms.\n",
            encoding="utf-8")
        (self.self_record / "canon" / "VOICE.md").write_text(
            "# VOICE\n\n- Plain, concrete, verb-led.\n", encoding="utf-8")
        (self.self_record / "canon" / "self.json").write_text("{}", encoding="utf-8")
        self.packages = root / "packages"
        self.packages.mkdir()
        patches = [
            mock.patch.object(applications, "sources",
                              lambda: self.sources),
            mock.patch.object(applications, "connections_csv",
                              lambda: conns),
            mock.patch.object(applications, "universe_dir",
                              lambda: self.universe),
            mock.patch.object(applications, "STORE",
                              root / "applications.json"),
            mock.patch.object(applications, "self_record",
                              lambda: self.self_record),
            mock.patch.object(applicationmap, "packages_root",
                              lambda: self.packages),
            # Availability is read from the boards state, and boards_dir()
            # checks CONFIG before it falls back to the universe — so
            # without this the suite reads the running instance's own
            # boards, and a machine with a config override makes these
            # tests pass or fail on facts about that machine. Patching
            # what the code writes never isolates what it reads.
            mock.patch.object(jobboards, "boards_dir",
                              lambda: self.universe / "boards"),
            mock.patch.object(jobboards, "location_rule", _nyc_rule),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for cache, blank in ((applications._cache, {"key": None, "roles": None}),
                             (applications._universe_cache,
                              {"key": None, "roles": None}),
                             (applicationmap._PKG_CACHE,
                              {"key": None, "rows": []}),
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
            "jd": ("About the role. Advise customers and build the platform. "
                   "Responsibilities. Design scalable architectures, ship "
                   "reusable prototypes, and support safe production use."),
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
        """Owner ruling fixture: OTE cut by comp, marketing cut by title.

        `shortlist` still writes the pinned-picks key so a test can prove
        the loader IGNORES it — the file on the owner's disk still carries
        one, and reading it back is exactly the regression to catch.
        """
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
        self.assertIn("Advise customers", arch["jd"])
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
        self.assertTrue(out["roles"][0]["has_description"])
        self.assertNotIn("jd", out["roles"][0])   # full text is compare-only
        self.assertIsNone(out["roles"][-1]["fit"])   # unscored sorts last
        self.assertFalse(out["roles"][-1]["has_description"])
        comp = out["companies"]["Example Labs"]
        self.assertEqual(comp["roles"], 1)
        self.assertEqual(comp["connections"], 2)

    def test_company_filter(self):
        out = applications.compose(company="otherco", view="all")
        self.assertEqual(len(out["roles"]), 1)
        self.assertEqual(out["roles"][0]["company"], "OtherCo")

    def test_empty_universe_still_serves_the_corpus(self):
        """One list: with nothing analyzed yet the raw postings ARE the
        catalog. The old default served [] here, which is what a fresh
        install saw — an empty module beside a full boards snapshot."""
        out = applications.compose()
        self.assertEqual(len(out["roles"]), 2)
        self.assertEqual(out["meta"]["universe"]["total"], 0)
        self.assertEqual(out["meta"]["universe"]["unanalyzed"], 2)
        self.assertTrue(all(not r["in_universe"] for r in out["roles"]))

    def write_package(self, name, uid, company, title, url):
        version = self.packages / name / "V1"
        version.mkdir(parents=True)
        (version / "posting.md").write_text(
            f"# {title}\n\nCompany: {company}\nPosting URL: {url}\n"
            f"Role record uid: {uid}\n", encoding="utf-8")

    def test_a_role_with_a_package_on_disk_reads_as_written(self):
        self.write_package(
            "example-labs-architect", "g-examplelabs-1234567", "Example Labs",
            "Platform Architect",
            "https://job-boards.greenhouse.io/examplelabs/jobs/1234567")
        out = applications.compose(view="all")
        rows = {r["uid"]: r for r in out["roles"]}
        self.assertTrue(rows["g-examplelabs-1234567"]["written"])
        self.assertEqual(rows["g-examplelabs-1234567"]["package"],
                         "example-labs-architect")
        others = [r for uid, r in rows.items() if uid != "g-examplelabs-1234567"]
        self.assertTrue(others and all(not r["written"] for r in others))
        self.assertTrue(all(r["package"] is None for r in others))

    def test_written_is_read_off_disk_not_off_the_dispatch_stamp(self):
        # `last_job` records that a session was DISPATCHED — it survives one
        # that failed, and it misses a package written from a copy-out
        # session Vira never launched. The catalog reports what exists.
        applications.update_state("g-examplelabs-1234567",
                                  job_id="job-that-produced-nothing")
        rows = {r["uid"]: r for r in applications.compose(view="all")["roles"]}
        self.assertEqual(rows["g-examplelabs-1234567"]["last_job"],
                         "job-that-produced-nothing")
        self.assertFalse(rows["g-examplelabs-1234567"]["written"])

    def test_company_research_graph_is_joined_without_becoming_role_data(self):
        graph = {"id": "example-labs", "name": "Example research",
                 "company": "Example Labs", "status": "ready",
                 "claim_count": 12}
        with mock.patch("server.research.catalog", return_value=[graph]):
            out = applications.compose()
        role = next(row for row in out["roles"]
                    if row["company"] == "Example Labs")
        self.assertEqual("example-labs", role["research"]["slug"])
        self.assertEqual(12, role["research"]["claim_count"])
        self.assertEqual("example-labs",
                         out["companies"]["Example Labs"]["research"]["slug"])


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

    def test_one_list_leads_with_the_analyzed_roles(self):
        """The Universe / All-boards toggle is gone. Everything is in one
        list, analyzed roles first, and the axis that toggle was actually
        good for survives as the in_universe flag the tier filter reads."""
        out = applications.compose()
        uids = [r["uid"] for r in out["roles"]]
        self.assertEqual(uids[:2],
                         ["g-examplelabs-1234567", "g-examplelabs-777"])
        rest = out["roles"][2:]
        self.assertTrue(rest)
        self.assertTrue(all(not r["in_universe"] for r in rest))
        self.assertEqual(out["meta"]["universe"]["scored"], 1)
        self.assertEqual(out["meta"]["universe"]["tier1"], 1)
        self.assertEqual(out["meta"]["universe"]["unanalyzed"], len(rest))

    def test_view_param_is_accepted_and_ignored(self):
        """An old bookmark or a stale client must not 404 or get a
        different list than the one the module now has."""
        self.assertEqual(applications.compose(view="all")["roles"],
                         applications.compose()["roles"])
        self.assertEqual(applications.compose(view="universe")["roles"],
                         applications.compose()["roles"])

    def _seed_boards_state(self, roles):
        d = self.universe / "boards"
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps({"roles": roles}),
                                      encoding="utf-8")
        applications._cache.update({"key": None, "roles": None})
        applications._universe_cache.update({"key": None, "roles": None})

    def test_availability_marks_a_dead_posting_without_deleting_it(self):
        """The whole point: the analysis stacked on a role is expensive, so
        a posting that comes down is marked, not dropped."""
        self._seed_boards_state({
            "g-examplelabs-1234567": {"closed": "2026-07-22T10:00:00+00:00"},
            "g-examplelabs-777": {"last_seen": fresh_seen()},
        })
        by = {r["uid"]: r for r in applications.load_universe()}
        gone = by["g-examplelabs-1234567"]
        self.assertEqual(gone["availability"], "gone")
        self.assertTrue(gone["availability_when"].startswith("2026-07-22"))
        self.assertEqual(gone["fit"], 90)          # the analysis survives
        self.assertEqual(gone["tier"], "1")
        self.assertEqual(by["g-examplelabs-777"]["availability"], "open")

    def test_a_role_no_board_covers_reads_unverified_never_open(self):
        """A frozen corpus is not evidence a posting is live. Absence of a
        board is stated, not silently rendered as still-available."""
        self._seed_boards_state({})
        by = {r["uid"]: r for r in applications.load_universe()}
        self.assertEqual(by["g-examplelabs-1234567"]["availability"],
                         "unverified")
        out = applications.compose()
        self.assertEqual(out["meta"]["availability"]["gone"], 0)
        self.assertTrue(out["meta"]["availability"]["unverified"])

    def test_a_closed_role_invalidates_the_cache(self):
        """Availability lives in the boards state, so a sweep that closes a
        role has to invalidate the catalog cache — or the module keeps
        serving it as live until some unrelated file happens to change."""
        self._seed_boards_state(
            {"g-examplelabs-1234567": {"last_seen": fresh_seen()}})
        first = {r["uid"]: r for r in applications.load_universe()}
        self.assertEqual(first["g-examplelabs-1234567"]["availability"],
                         "open")
        d = self.universe / "boards"
        (d / "state.json").write_text(json.dumps(
            {"roles": {"g-examplelabs-1234567":
                       {"closed": "2026-07-29T12:00:00+00:00"}}}),
            encoding="utf-8")
        # no cache reset here on purpose — the key must notice by itself
        again = {r["uid"]: r for r in applications.load_universe()}
        self.assertEqual(again["g-examplelabs-1234567"]["availability"],
                         "gone")

    def test_a_pinned_pick_in_the_file_is_not_read_back(self):
        # Picks are retired (2026-08-13). The owner's adjudication file still
        # lists them, so the loader has to ignore the key rather than depend
        # on the data being gone — and a listed role is cut like any other
        # when the cut rules reach it, since nothing outranks them now.
        self.seed_adjudication(shortlist=["g-examplelabs-777"])
        uni = applications.load_universe()
        by = {r["uid"]: r for r in uni}
        self.assertNotIn("shortlist", by["g-examplelabs-777"])
        self.assertIn("quota/commission", by["g-examplelabs-777"]["cut"])
        self.assertNotIn("shortlist", applications.compose()["meta"]["universe"])

    def test_adjudication_cuts_and_demotes(self):
        self.seed_adjudication()
        uni = applications.load_universe()
        by = {r["uid"]: r for r in uni}
        self.assertIn("quota/commission", by["g-examplelabs-777"]["cut"])
        self.assertIn("marketing", by["g-examplelabs-888"]["cut"])
        # the scored tier-1 role leads; both cut roles sink below it
        self.assertEqual(uni[0]["uid"], "g-examplelabs-1234567")
        cut_at = [i for i, r in enumerate(uni) if r["cut"]]
        self.assertEqual(cut_at, sorted(cut_at))
        self.assertEqual(min(cut_at), 1)
        out = applications.compose()
        self.assertEqual(out["meta"]["universe"]["cut"], 2)

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


class SessionSlugTest(ApplicationsBase):
    """The label leading every Apply prompt, and the naming it drives."""

    def test_the_slug_is_company_then_title_then_date(self):
        role = {"company": "Scale AI", "title": "Frontier Agents "
                "Engineer, Applied AI"}
        self.assertEqual(
            applications.session_slug(role, when=datetime(2026, 8, 12)),
            "scale-ai-frontier-agents-engineer-applied-ai-2026-08-12")

    def test_the_slug_leads_the_prompt(self):
        roles, _ = applications.load_roles()
        first = applications.apply_prompt(roles[0]).splitlines()[0]
        # Computed here rather than compared against a second session_slug
        # call: both would read the clock, and a run crossing midnight
        # between them would fail on a date that was never wrong.
        self.assertRegex(first, r"^[a-z0-9-]+-\d{4}-\d{2}-\d{2}$")
        self.assertTrue(first.startswith("example-labs-platform-architect-"),
                        first)

    def test_the_slug_is_what_names_the_session(self):
        """The join. The slug exists to name the run, and the naming lives
        in joblog — so assert against the REAL composed prompt through the
        REAL naming, not against the string this module just built."""
        roles, _ = applications.load_roles()
        record = {"prompt": applications.apply_prompt(roles[0])}
        slug = applications.session_slug(roles[0])
        self.assertEqual(joblog.command(record), slug)
        self.assertEqual(joblog.default_title(record), slug)

    def test_a_long_title_is_trimmed_so_company_and_date_survive(self):
        # 826 of 3,197 live roles exceed TITLE_CAP; the cut must come out of
        # the title, never the date, or every long-titled role's sessions
        # become indistinguishable again.
        role = {"company": "Example Deepmind",
                "title": "Senior Strategic Portfolio Manager, Frontier "
                         "Safety Alignment and Collaboration"}
        slug = applications.session_slug(role, when=datetime(2026, 8, 12))
        self.assertLessEqual(len(slug), joblog.TITLE_CAP)
        self.assertTrue(slug.startswith("example-deepmind-"), slug)
        self.assertTrue(slug.endswith("-2026-08-12"), slug)
        # and the trim is on a hyphen boundary, not mid-word
        self.assertNotIn("--", slug)
        # the name survives whole — no ellipsis, which is the whole point
        self.assertEqual(joblog.default_title({"prompt": slug}), slug)

    def test_an_absurd_company_still_leaves_a_usable_label(self):
        role = {"company": "A" * 200, "title": "Engineer"}
        slug = applications.session_slug(role, when=datetime(2026, 8, 12))
        self.assertLessEqual(len(slug), joblog.TITLE_CAP)
        self.assertTrue(slug.endswith("-2026-08-12"), slug)

    def test_a_missing_company_never_yields_a_leading_hyphen(self):
        # joblog.slugify-class rule: a label starting with punctuation reads
        # as a broken name, and branch slugs derived from one are illegal.
        slug = applications.session_slug({"title": "Engineer"},
                                         when=datetime(2026, 8, 12))
        self.assertEqual(slug, "role-engineer-2026-08-12")


class PromptTest(ApplicationsBase):
    def test_apply_prompt_embeds_the_claim_gate_and_walls(self):
        # These rules must ride the PROMPT TEXT, not only the self-record's
        # CLAUDE.md: a non-Claude backend (codex) auto-loads no CLAUDE.md,
        # so a run there receives exactly what this string carries. Before
        # 2026-07-31 that was the draft-only rule and nothing else — no
        # claim gate, no confidentiality walls.
        roles, _ = applications.load_roles()
        p = applications.apply_prompt(roles[0])
        self.assertIn("GROUND RULES", p)
        self.assertIn("MASTER_HISTORY.md", p)
        self.assertIn("check_freshness.py", p)
        self.assertIn("INVENTORY.md", p)
        self.assertIn("VOICE.md", p)
        # The gate moved into Master History's endnotes on 2026-08-11 when
        # FACTS.md was folded in and deleted. The prompt must still carry a
        # claim gate, and must not resurrect the deleted filename.
        self.assertIn("ENDNOTES ARE", p)
        self.assertIn("claim gate", p.lower())
        self.assertNotIn("FACTS.md", p)
        # Built from Path like the prompt is, not as a literal with forward
        # slashes: on Windows these render with backslashes and the literal
        # form fails there while passing on macOS. CI runs both.
        self.assertIn(str(applications.self_record() / "evidence" / "sentinel"), p)
        self.assertIn(str(applications.self_record() / "evidence" / "personal"), p)
        self.assertIn("admissible PRIVATE evidence", p)
        self.assertIn("active adjudication", p)
        self.assertIn("evidence map", p)
        self.assertIn("VP Operations", p)
        self.assertIn("pre-Sentinel foundations", p)
        self.assertIn("Draft only", p)
        self.assertIn("never a source", p)
        self.assertNotIn("is the ONLY claim source", p)
        self.assertNotIn("Nothing from " + str(applications.self_record() / "evidence" / "sentinel"), p)

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

    def test_apply_prompt_names_research_authority_boundary(self):
        roles, _ = applications.load_roles()
        context = {
            "research": {"graph": {"database": "/tmp/public.sqlite"}},
            "personal_bridge": {"path": "/tmp/bridge.json", "taxonomy": {
                "items": [{"rank": 1, "id": "craft", "label": "Craft",
                           "application_use": "Lead with artifacts.",
                           "matt_overlap": {
                               "facts_anchors": ["MASTER_HISTORY.md#^18"],
                               "permitted_language": ["builder"],
                               "boundaries": ["Do not overclaim."]}}]}},
        }
        with mock.patch("server.research.application_context",
                        return_value=context):
            p = applications.apply_prompt(roles[0])
        self.assertIn("EMPLOYER RESEARCH CONTEXT", p)
        self.assertIn("describes Example Labs, not the owner", p)
        self.assertIn("MASTER_HISTORY.md#^18", p)
        self.assertIn("Do not overclaim", p)

    def test_apply_prompt_embeds_the_shared_requirement_plan(self):
        roles, _ = applications.load_roles()
        p = applications.apply_prompt(roles[0])
        self.assertIn("APPLICATION EVIDENCE PLAN", p)
        self.assertIn("same framework as Vira's interactive Map", p)
        self.assertIn("TRANSFERABLE ANALOGUE", p)
        self.assertIn("NEEDS ADJUDICATION", p)
        self.assertIn("or GAP", p)
        self.assertIn("Design scalable architectures", p)
        self.assertIn("selected resume treatment", p)
        self.assertIn("cover-letter angle", p)
        self.assertIn("interview narrative/talking point", p)


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

    def test_compare_route_resolves_full_descriptions(self):
        out = self.main.api_applications_compare(self.main.AppCompareReq(
            uids=["g-examplelabs-1234567", "g-examplelabs-777"]))
        self.assertEqual(len(out["roles"]), 2)
        self.assertEqual(len(out["pairs"]), 1)
        self.assertIn("shared_pct", out["overall"])

    def test_compare_route_rejects_unknown_role(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self.main.api_applications_compare(self.main.AppCompareReq(
                uids=["g-examplelabs-1234567", "missing"]))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_apply_passes_note_and_model_to_launch(self):
        calls = {}

        def fake_launch(prompt, cwd, permission_mode=None, model=None,
                        publish_plan=False, idea_id=None, mode=None):
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
        # No hardcoded mode: the dispatch derives from session_default_mode
        # (owner's call, 2026-08-12 — Apply used to pin "manual").
        self.assertIsNone(calls["mode"])
        self.assertIsNone(calls["permission_mode"])
        self.assertEqual(
            applications.get_state()["g-examplelabs-1234567"]["last_job"],
            "job-123")


class PlacesAndLocationTest(ApplicationsBase):
    """The location layer (2026-08-05): canonical place facets, an
    eligibility verdict on EVERY role (stamped by a sweep or computed
    against the owner's rule), and the compose metadata the location
    dropdown is built from."""

    def test_places_normalize_split_aliases_and_remote(self):
        self.assertEqual(
            applications.places_for(
                ["San Francisco, CA | New York City, NY",
                 "NYC", "Remote - US", "Remote-Friendly, United States",
                 "London, UK", "london"]),
            ["San Francisco", "New York", "Remote", "London"])

    def test_norm_respects_a_stamp_and_computes_otherwise(self):
        rule = jobboards.location_rule()
        src = {"slug": "x", "company": "X"}
        stamped = applications._norm(
            {"title": "T", "eligible": False,
             "locations": ["New York, NY"],
             "url": "https://job-boards.greenhouse.io/x/jobs/1"},
            src, {}, rule)
        self.assertIs(stamped["eligible"], False)   # the sweep's verdict wins
        computed = applications._norm(
            {"title": "T", "locations": ["London, UK"],
             "url": "https://job-boards.greenhouse.io/x/jobs/2"},
            src, {}, rule)
        self.assertIs(computed["eligible"], False)
        nyc = applications._norm(
            {"title": "T", "locations": ["New York, NY"],
             "url": "https://job-boards.greenhouse.io/x/jobs/3"},
            src, {}, rule)
        self.assertIs(nyc["eligible"], True)

    def test_first_seen_and_baseline_ride_through(self):
        r = applications._norm(
            {"title": "T", "locations": ["Remote"],
             "first_seen": "2026-08-01T00:00:00+00:00", "baseline": True,
             "url": "https://jobs.ashbyhq.com/x/aaaa-bbbb"},
            {"slug": "boards", "company": None}, {},
            jobboards.location_rule())
        self.assertEqual(r["first_seen"], "2026-08-01T00:00:00+00:00")
        self.assertTrue(r["baseline"])

    def test_catalog_roles_all_carry_a_verdict(self):
        roles, _ = applications.load_roles()
        for r in roles:
            self.assertIn(r["eligible"], (True, False))
        by = {r["uid"]: r for r in roles}
        self.assertTrue(by["g-examplelabs-1234567"]["eligible"])   # NYC
        self.assertEqual(by["g-examplelabs-1234567"]["places"],
                         ["New York"])

    def test_compose_meta_carries_facets_counts_and_the_rule(self):
        with mock.patch.object(applications.settings, "raw",
                               return_value=NYC_CFG):
            data = applications.compose()
        names = {l["name"]: l["count"] for l in data["meta"]["locations"]}
        self.assertIn("New York", names)
        self.assertIn("Remote", names)
        el = data["meta"]["eligibility"]
        self.assertEqual(el["eligible"] + el["outside"],
                         len(data["roles"]))
        lr = data["meta"]["location_rule"]
        self.assertTrue(lr["configured"])
        self.assertEqual(lr["places"], ["New York", "NYC"])
        self.assertTrue(lr["remote_ok"])


class StatusFilterContract(unittest.TestCase):
    """The status filter's options and its matcher must cover each other.

    Every option is either one of the server's stored statuses or a DERIVED
    one the client resolves itself.  An option with neither backing is the
    silent failure this pins: it renders in the picker, ticks, and filters
    the list down to nothing with no error anywhere.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = (Path(__file__).resolve().parents[1] / "static" / "app.js"
                   ).read_text(encoding="utf-8")

    def options(self):
        block = re.search(r"const APP_STATUS_OPTIONS = \[(.*?)\];",
                          self.app, re.S)
        self.assertIsNotNone(block, "APP_STATUS_OPTIONS not found in app.js")
        return re.findall(r'\["([a-z-]+)",', block.group(1))

    def derived(self):
        body = re.search(r"function appStatusMatches\(r, value\) \{(.*?)\n\}",
                         self.app, re.S)
        self.assertIsNotNone(body, "appStatusMatches not found in app.js")
        return set(re.findall(r'value === "([a-z-]+)"', body.group(1)))

    def test_every_option_is_a_stored_status_or_a_handled_derived_one(self):
        options = self.options()
        self.assertTrue(options, "the status picker offers nothing")
        unbacked = [v for v in options
                    if v not in applications.STATUSES and v not in self.derived()]
        self.assertEqual(unbacked, [], f"status options nothing answers: {unbacked}")

    def test_the_working_set_option_is_offered(self):
        # The owner's ask: one filter for what is drafted and still his.
        self.assertIn("written", self.options())
        self.assertIn("written", self.derived())


class SortContract(unittest.TestCase):
    """Each sort option names ONE axis, and something answers for it.

    `appSortKey` falls through to the combined score, so an option the
    comparator does not name does not error — it silently sorts by
    something else while its label claims otherwise. That is the failure
    this pins. (The suite cannot execute the comparator; the ordering
    itself is verified by driving the real list.)
    """

    @classmethod
    def setUpClass(cls):
        cls.app = (Path(__file__).resolve().parents[1] / "static" / "app.js"
                   ).read_text(encoding="utf-8")

    def options(self):
        block = re.search(r"const APP_SORTS = \[(.*?)\];", self.app, re.S)
        self.assertIsNotNone(block, "APP_SORTS not found in app.js")
        return re.findall(r'\["([a-z]+)",', block.group(1))

    def test_every_option_is_named_by_the_comparator(self):
        body = re.search(r"function appSortKey\(r, mode\) \{(.*?)\n\}",
                         self.app, re.S)
        self.assertIsNotNone(body, "appSortKey not found in app.js")
        named = set(re.findall(r'mode === "([a-z]+)"', body.group(1)))
        options = self.options()
        self.assertTrue(options, "the sort dropdown offers nothing")
        # "best" is the documented fall-through, so it needs no branch
        unnamed = [v for v in options if v not in named and v != "best"]
        self.assertEqual(unnamed, [], f"sort options nothing reads: {unnamed}")

    def test_the_validation_list_derives_from_the_table(self):
        # A second hand-written list is a second thing to forget: a saved
        # sort the table no longer offers must fall back, not stick.
        self.assertIn("const APPS_SORTS = APP_SORTS.map(([v]) => v);",
                      self.app)

    def test_the_cut_lane_is_demoted_before_any_score(self):
        # Cut roles score as high as 90 on the real universe, so a bare
        # score sort puts a role the owner refuses at the top of the list.
        body = re.search(r"function appsSort\(rows, mode\) \{(.*?)\n\}",
                         self.app, re.S)
        self.assertIsNotNone(body, "appsSort not found in app.js")
        text = body.group(1)
        self.assertLess(text.index("a.cut"), text.index("appSortKey"),
                        "the cut test must run before the score comparison")


if __name__ == "__main__":
    unittest.main()
