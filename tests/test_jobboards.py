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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from server import applications, jobboards


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
    "cut_comp": {"ote"},
    "cut_titles": [__import__("re").compile(
        r"account executive|\bsales\b", __import__("re").I)],
    "reason_comp": "quota comp — cut",
    "reason_title": "selling role — cut",
}


# The owner-config this suite pins itself to. jobboards.location_rule()
# reads data/config.json, which is the RUNNING INSTANCE's file — without
# this every eligibility assertion below would depend on how the machine
# running the tests happens to be set up.
NYC_CONFIG = {"applications_locations": ["New York", "NYC"],
              "applications_remote_ok": True}


def NYC_RULE():
    with mock.patch.object(jobboards.settings, "raw",
                           return_value=NYC_CONFIG):
        return jobboards.location_rule()


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

    def test_conditional_sales_ote_disclaimer_is_not_role_comp(self):
        disclaimer = (
            "The annual compensation range for this role is listed below. "
            "For sales roles, the range provided is the role’s On Target "
            "Earnings (\"OTE\") range, meaning that the range includes both "
            "the sales commissions/sales bonuses target and annual base "
            "salary for the role. Annual Salary: $240,000 - $300,000 USD")
        self.assertEqual(jobboards._comp_kind(disclaimer, 240000), "base")

    def test_role_specific_ote_survives_conditional_disclaimer(self):
        jd = (
            "This is a quota-carrying position with uncapped commission. "
            "For sales roles, the range provided is the role's On-Target "
            "Earnings (OTE) range, meaning that the range includes both the "
            "sales commission/sales bonus target and annual base salary for "
            "the role.")
        self.assertEqual(jobboards._comp_kind(jd, 180000), "ote")

    def test_sales_title_still_cuts_when_disclaimer_is_generic(self):
        disclaimer = (
            "For sales roles, the range provided is the role's On Target "
            "Earnings (OTE) range, meaning that the range includes both the "
            "sales commissions/sales bonuses target and annual base salary "
            "for the role.")
        rec = {"uid": "x4", "title": "Enterprise Account Executive",
               "comp": jobboards._comp_kind(disclaimer, 200000),
               "locations": ["New York City, NY"]}
        jobboards.evaluate(rec, ADJ)
        self.assertEqual(rec["comp"], "base")
        self.assertEqual(rec["cut"], "selling role — cut")

    def test_ashby_remote_tag_appended(self):
        with mock.patch.object(jobboards, "_get", return_value=ASHBY_PAYLOAD):
            out = jobboards.fetch_ashby(ASHBY_BOARD)
        self.assertEqual(out[0]["uid"],
                         "as-otherco-abcdefab-1111-2222-3333-444444444444")
        self.assertIn("Remote", out[0]["locations"])

    def test_location_rule(self):
        # The rule is CONFIGURED, so the test configures it. Reading the
        # developer's own data/config.json here would make the suite pass
        # or fail depending on whose machine it runs on — and would hide
        # the unconfigured-means-unfiltered default from the assertions
        # entirely (that property is covered in test_frontdoor).
        rule = NYC_RULE()
        ok = {"locations": ["New York City, NY"]}
        bare_remote = {"locations": ["Remote"]}
        us_remote = {"locations": ["San Francisco", "Remote"]}
        foreign_remote = {"locations": ["Seoul", "Remote"]}
        foreign_only = {"locations": ["London"]}
        eu_remote = {"locations": ["Remote - Europe"]}
        self.assertTrue(jobboards.eligible_location(ok, rule))
        self.assertTrue(jobboards.eligible_location(bare_remote, rule))
        self.assertTrue(jobboards.eligible_location(us_remote, rule))
        self.assertFalse(jobboards.eligible_location(foreign_remote, rule))
        self.assertFalse(jobboards.eligible_location(foreign_only, rule))
        self.assertFalse(jobboards.eligible_location(eu_remote, rule))

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
            # poll_once and the ping label both read the owner's location
            # rule from config; pin it so the suite tests the code rather
            # than the machine it runs on.
            mock.patch.object(jobboards.settings, "raw",
                              return_value=NYC_CONFIG),
            # The universe, pinned inside the fixture. maybe_auto_score's
            # second phase (the stale-score drain) reaches the candidate
            # universe and the owner's canon, and patching boards_dir does
            # nothing about what a function READS — without these two the
            # empty-backlog case built the real 962-role rescue queue off
            # the owner's own self-record from inside a unit test.
            # (patched on the applications module itself: jobboards imports
            # it inside each function, so `jobboards.applications` is not an
            # attribute to patch — and create=True there would invent a name
            # nothing reads.)
            mock.patch.object(applications, "universe_dir",
                              return_value=self.dir),
            mock.patch.object(applications, "load_universe",
                              return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.tmp2 = self.tmp   # keep alive
        self.addCleanup(self.tmp.cleanup)
        # Every test below starts from a board Vira has already swept once.
        # A board's FIRST sweep is a baseline — it discovers the whole board
        # at once, which is not news — and that has its own test.
        self._poll({"jobs": []})

    def _poll(self, payload, notify_ret=True):
        sent = []

        def fake_ping(text, key=None):
            sent.append((text, key))
            return notify_ret
        with mock.patch.object(jobboards, "_get", return_value=payload), \
                mock.patch("server.notify.agent_ping", fake_ping):
            r = jobboards.poll_once()
        return r, sent

    def test_a_catalog_tombstone_that_reappears_does_not_kill_the_sweep(self):
        """The 2026-08-02 wedge: a catalog-closing sweep mints state
        entries carrying ONLY {closed: ts} (a corpus role no board had
        served). When one then shows up in a fetch, poll_once read its
        missing first_seen and crashed — every sweep died mid-loop for
        three days and the snapshot was never written again."""
        state_path = jobboards._state_path()
        state = jobboards._read_json(state_path, {})
        state.setdefault("roles", {})["g-exlabs-111"] = {
            "closed": "2026-08-01T00:00:00+00:00"}
        jobboards._write_json(state_path, state)
        r, _ = self._poll(GH_PAYLOAD)          # must not raise
        self.assertTrue(r["ok"])
        snap = jobboards._read_json(jobboards._snapshot_path(), {})
        rec = snap["roles"]["g-exlabs-111"]
        self.assertTrue(rec.get("first_seen"))  # stamped now, not missing
        self.assertNotIn("closed", rec)         # it is listed again

    def test_new_then_stable_then_closed(self):
        r1, sent1 = self._poll(GH_PAYLOAD)
        self.assertEqual(r1["new"], 2)
        self.assertEqual(r1["eligible_new"], 1)   # the AE is cut
        self.assertEqual(len(sent1), 1)
        self.assertIn("Deployment Strategist", sent1[0][0])
        # The ping names the place the role actually matched, echoed from
        # the owner's own configured list, rather than a label hardcoded
        # to one city.
        self.assertIn("(New York)", sent1[0][0])

        # second poll: nothing new, nothing re-notified
        r2, sent2 = self._poll(GH_PAYLOAD)
        self.assertEqual(r2["new"], 0)
        self.assertEqual(sent2, [])

        # the strategist vanishes from the board -> closed, kept in snapshot
        one = {"jobs": [GH_PAYLOAD["jobs"][1]]}
        r3, _ = self._poll(one)
        self.assertEqual(r3["closed"], 1)
        snap = json.loads((self.dir / "snapshot.json").read_text(encoding="utf-8"))
        self.assertTrue(snap["roles"]["g-exlabs-111"].get("closed"))

        # it comes back -> reopened, but NOT re-notified (state remembers)
        r4, sent4 = self._poll(GH_PAYLOAD)
        self.assertEqual(r4["new"], 0)
        self.assertEqual(sent4, [])
        snap = json.loads((self.dir / "snapshot.json").read_text(encoding="utf-8"))
        self.assertFalse(snap["roles"]["g-exlabs-111"].get("closed"))

    def test_first_sweep_of_a_new_board_is_a_baseline(self):
        """Registering a company means discovering its whole board at once
        — announcing that as new jobs would bury the ping that matters."""
        self.dir.joinpath("boards.json").write_text(json.dumps(
            {"boards": [dict(GH_BOARD),
                        {"company": "Late Co", "ats": "greenhouse",
                         "slug": "lateco"}]}), encoding="utf-8")
        r, sent = self._poll(GH_PAYLOAD)      # lateco's first sweep
        self.assertEqual(r["baselined"], ["greenhouse-lateco"])
        # the already-swept board's new roles are real news...
        self.assertEqual(r["new"], 2)          # both exlabs roles
        self.assertEqual(r["eligible_new"], 1)  # the AE is cut
        self.assertEqual(len(sent), 1)
        self.assertIn("Example Labs", sent[0][0])
        # ...while the whole of the new board is baselined, not announced
        self.assertNotIn("Late Co", sent[0][0])
        state = json.loads((self.dir / "state.json").read_text(
            encoding="utf-8"))
        self.assertEqual(state["roles"]["g-lateco-111"]["notified"],
                         "baseline")
        # ...and a role that shows up AFTER the baseline is real news
        later = {"jobs": GH_PAYLOAD["jobs"] + [
            {"id": 333, "title": "Deployment Engineer",
             "absolute_url": "https://job-boards.greenhouse.io/exlabs/jobs/333",
             "location": {"name": "New York City, NY"},
             "departments": [{"name": "Deployment"}],
             "content": "Base salary role.", "updated_at": "2026-07-02"}]}
        r2, sent2 = self._poll(later)
        self.assertEqual(r2["new"], 2)        # one per registered board
        self.assertEqual(len(sent2), 1)
        self.assertIn("Deployment Engineer", sent2[0][0])

    def test_upgrading_does_not_baseline_an_already_swept_registry(self):
        """An install that predates this rule has no board state, but its
        snapshot records every board it has ever swept. Baselining those
        would silently swallow one cycle's real new roles."""
        self._poll(GH_PAYLOAD)
        state = json.loads((self.dir / "state.json").read_text(
            encoding="utf-8"))
        state.pop("boards")                     # the pre-upgrade shape
        (self.dir / "state.json").write_text(json.dumps(state),
                                             encoding="utf-8")
        later = {"jobs": GH_PAYLOAD["jobs"] + [
            {"id": 444, "title": "Deployment Lead",
             "absolute_url": "https://job-boards.greenhouse.io/exlabs/jobs/444",
             "location": {"name": "New York City, NY"},
             "departments": [{"name": "Deployment"}],
             "content": "Base salary role.", "updated_at": "2026-07-03"}]}
        r, sent = self._poll(later)
        self.assertEqual(r["baselined"], [])
        self.assertEqual(r["new"], 1)
        self.assertIn("Deployment Lead", sent[0][0])

    def test_a_full_board_closes_roles_it_never_served(self):
        """The universe is mostly roles carried in from frozen corpora that
        no snapshot ever held. A sweep that only checks what it has served
        before can never learn those died — which is exactly how dead picks
        sat in the module for weeks."""
        with mock.patch.object(jobboards, "_catalog_uids",
                               return_value={"g-exlabs-999", "as-other-1"}):
            r, _ = self._poll(GH_PAYLOAD)
        # 999 is in this board's namespace and did not come back -> gone
        self.assertEqual(r["closed"], 1)
        state = json.loads((self.dir / "state.json").read_text(
            encoding="utf-8"))
        self.assertTrue(state["roles"]["g-exlabs-999"].get("closed"))
        # another board's namespace is none of this board's business
        self.assertNotIn("as-other-1", state["roles"])

    def test_a_query_board_never_closes_outside_what_it_served(self):
        """microsoft/google are SEARCHES: a role can fall out of a result
        set while the posting is alive, so absence proves nothing about the
        wider namespace."""
        self.dir.joinpath("boards.json").write_text(json.dumps(
            {"boards": [{"company": "MS", "ats": "microsoft",
                         "query": "AI", "location": "New York"}]}),
            encoding="utf-8")
        with mock.patch.object(jobboards, "_catalog_uids",
                               return_value={"ms-4242"}), \
                mock.patch.object(jobboards, "_get",
                                  return_value={"data": {"count": 0,
                                                         "positions": []}}), \
                mock.patch("server.notify.agent_ping", lambda *a, **k: True):
            jobboards.poll_once()
        state = json.loads((self.dir / "state.json").read_text(
            encoding="utf-8"))
        self.assertNotIn("ms-4242", state["roles"])

    def test_availability_map_states(self):
        self._poll(GH_PAYLOAD)
        av = jobboards.availability_map()
        self.assertEqual(av["g-exlabs-111"]["state"], "open")
        self._poll({"jobs": [GH_PAYLOAD["jobs"][1]]})
        av = jobboards.availability_map()
        self.assertEqual(av["g-exlabs-111"]["state"], "gone")
        self.assertTrue(av["g-exlabs-111"]["since"])
        # never inferred from absence: an unseen uid is simply not in the map
        self.assertNotIn("a-1234", av)

    def test_a_stale_sighting_is_unverified_not_open(self):
        """'open' means a sweep confirmed it recently. A manual board's
        roles are written once by hand and never refetched, so a bare
        last_seen would read as live forever."""
        self._poll(GH_PAYLOAD)
        state = json.loads((self.dir / "state.json").read_text(
            encoding="utf-8"))
        state["roles"]["g-exlabs-111"]["last_seen"] = "2026-07-01T00:00:00+00:00"
        (self.dir / "state.json").write_text(json.dumps(state),
                                             encoding="utf-8")
        av = jobboards.availability_map()
        self.assertEqual(av["g-exlabs-111"]["state"], "unverified")
        self.assertEqual(av["g-exlabs-111"]["checked"],
                         "2026-07-01T00:00:00+00:00")
        # a stale sighting is never upgraded to gone — nothing checked
        self.assertNotEqual(av["g-exlabs-111"]["state"], "gone")

    def test_arm_if_stale(self):
        """Opening the module asks for a sweep only when the last one has
        aged out, and never claims to have armed one on an instance that
        runs no poller (a passive test clone) — the caller would wait
        forever on a sweep that cannot happen."""
        class FakePoller:
            def __init__(self, alive):
                self.alive, self.armed = alive, False

            def is_alive(self):
                return self.alive

            def poll_now(self):
                self.armed = True

        self._poll(GH_PAYLOAD)                      # a sweep just happened
        p = FakePoller(True)
        r = jobboards.arm_if_stale(p)
        self.assertFalse(r["stale"])
        self.assertFalse(r["armed"])
        self.assertFalse(p.armed)

        snap = json.loads((self.dir / "snapshot.json").read_text(
            encoding="utf-8"))
        snap["fetched"] = "2026-07-01T00:00:00+00:00"
        (self.dir / "snapshot.json").write_text(json.dumps(snap),
                                                encoding="utf-8")
        p2 = FakePoller(True)
        r2 = jobboards.arm_if_stale(p2)
        self.assertTrue(r2["stale"])
        self.assertTrue(r2["armed"])
        self.assertTrue(p2.armed)

        # no poller (passive): stale, but honestly not armed
        r3 = jobboards.arm_if_stale(FakePoller(False))
        self.assertTrue(r3["stale"])
        self.assertFalse(r3["armed"])
        self.assertFalse(r3["running"])
        self.assertFalse(jobboards.arm_if_stale(None)["armed"])

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
        snap = json.loads((self.dir / "snapshot.json").read_text(encoding="utf-8"))
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


class AutoScore(PollDiffAndNotify):
    """maybe_auto_score — the poller dispatches scoring itself (owner's
    ruling 2026-08-05: no Score-new button). One dispatch in flight at a
    time, the AI probe gates a fresh install, and nothing ever raises."""

    def _seed_score(self, **kw):
        entry = {"job": "j-prev", "at": jobboards._now(), "roles": 5}
        entry.update(kw)
        jobboards._record_score(entry)
        return entry

    def _state(self):
        return jobboards._read_json(jobboards._state_path(), {})

    def test_disabled_by_config(self):
        cfg = dict(NYC_CONFIG, boards_auto_score=False)
        with mock.patch.object(jobboards.settings, "raw",
                               return_value=cfg):
            r = jobboards.maybe_auto_score()
        self.assertEqual(r["reason"], "disabled")

    def test_a_live_dispatch_blocks_a_second(self):
        self._seed_score()
        fake = mock.Mock()
        fake.get.return_value = {"status": "running"}
        with mock.patch("server.session.sessions", fake):
            r = jobboards.maybe_auto_score()
        self.assertEqual(r["reason"], "in flight")
        fake.launch.assert_not_called()

    def test_no_ai_connected_means_no_dispatch(self):
        self._poll(GH_PAYLOAD)
        with mock.patch("server.routines._ai_ready", return_value=False):
            r = jobboards.maybe_auto_score()
        self.assertEqual(r["reason"], "no AI connected")

    def test_a_recent_dispatch_cools_down_before_the_next(self):
        """The spend-limit case: the account probes green while every
        launch dies in seconds, so without a floor between dispatches the
        poller mints a dead ledger job every few minutes."""
        self._seed_score()                      # at = now, job not live
        fake = mock.Mock()
        fake.get.return_value = {"status": "error"}
        with mock.patch("server.session.sessions", fake):
            r = jobboards.maybe_auto_score()
        self.assertIn("cooling down", r["reason"])
        fake.launch.assert_not_called()

    def test_an_empty_backlog_clears_the_finished_record(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=45)) \
            .isoformat(timespec="seconds")
        self._seed_score(at=old)                # past the dispatch floor
        fake = mock.Mock()
        fake.get.return_value = {"status": "done"}
        with mock.patch("server.session.sessions", fake), \
                mock.patch("server.routines._ai_ready",
                           return_value=True), \
                mock.patch.object(jobboards, "_scored_uids",
                                  return_value=set()):
            r = jobboards.maybe_auto_score()
        self.assertEqual(r["reason"], "nothing to score or rescore")
        self.assertNotIn("score", self._state())

    def test_dispatch_records_the_job_and_marks_it_machine(self):
        self._poll(GH_PAYLOAD)
        fake = mock.Mock()
        fake.get.return_value = None
        fake.launch.return_value = "j-new"
        with mock.patch("server.session.sessions", fake), \
                mock.patch("server.routines._ai_ready",
                           return_value=True), \
                mock.patch.object(jobboards, "_scored_uids",
                                  return_value=set()):
            r = jobboards.maybe_auto_score()
        self.assertTrue(r["ok"])
        self.assertEqual(r["job"], "j-new")
        self.assertEqual(r["roles"], 1)     # the AE is cut
        kw = fake.launch.call_args.kwargs
        # machine marker: the session must never park in the reply window
        self.assertEqual(kw["meta"],
                         {"kind": "board-score", "machine": True})
        self.assertEqual(kw["cwd"], str(applications.self_record()))
        self.assertEqual(self._state()["score"]["job"], "j-new")

    def test_a_full_session_cap_is_reported_not_raised(self):
        self._poll(GH_PAYLOAD)
        fake = mock.Mock()
        fake.get.return_value = None
        fake.launch.side_effect = ValueError("live-session cap reached")
        with mock.patch("server.session.sessions", fake), \
                mock.patch("server.routines._ai_ready",
                           return_value=True), \
                mock.patch.object(jobboards, "_scored_uids",
                                  return_value=set()):
            r = jobboards.maybe_auto_score()
        self.assertFalse(r["ok"])
        self.assertIn("cap", r["reason"])
        self.assertNotIn("score", self._state())

    def test_a_stale_running_record_does_not_wedge_the_pipeline(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=4)) \
            .isoformat(timespec="seconds")
        self._seed_score(at=old)
        self._poll(GH_PAYLOAD)
        fake = mock.Mock()
        # the registry still says running — the age cap wins
        fake.get.return_value = {"status": "running"}
        fake.launch.return_value = "j-new"
        with mock.patch("server.session.sessions", fake), \
                mock.patch("server.routines._ai_ready",
                           return_value=True), \
                mock.patch.object(jobboards, "_scored_uids",
                                  return_value=set()):
            r = jobboards.maybe_auto_score()
        self.assertTrue(r["ok"])
        self.assertEqual(self._state()["score"]["job"], "j-new")

    def test_a_poll_preserves_the_score_record(self):
        self._seed_score()
        self._poll(GH_PAYLOAD)
        self.assertEqual(self._state()["score"]["job"], "j-prev")

    def test_status_reports_the_scoring_pipeline(self):
        self._seed_score()
        fake = mock.Mock()
        fake.get.return_value = {"status": "running"}
        with mock.patch("server.session.sessions", fake), \
                mock.patch.object(jobboards, "_scored_uids",
                                  return_value=set()):
            s = jobboards.status()
        self.assertTrue(s["auto_score"])
        self.assertEqual(s["scoring"]["job"], "j-prev")
        self.assertTrue(s["scoring"]["live"])


WD_SLUG = "acme.wd5.myworkdayjobs.com/acme/AcmeCareers"
WD_BOARD = {"company": "Acme", "ats": "workday", "slug": WD_SLUG}


def _wd_listing(*rows):
    """rows: (jid, locationsText) -> one page of the cxs listing shape."""
    return {"total": len(rows), "jobPostings": [
        {"title": f"Engineer {jid}", "bulletFields": [jid],
         "locationsText": loc,
         "externalPath": f"/job/Somewhere/Engineer_{jid}"}
        for jid, loc in rows]}


class WorkdayFetch(unittest.TestCase):
    """The cxs walk. Its two caps and its location prefilter are the whole
    design, so they are what these pin."""

    def setUp(self):
        p = mock.patch.object(jobboards.settings, "raw",
                              return_value=NYC_CONFIG)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, listing, detail_for):
        """detail_for: jid -> jobPostingInfo. Records which jids were
        detail-fetched, which is what proves the prefilter saved the call."""
        self.fetched = []

        def fake_get(url, *a, **kw):
            jid = url.rsplit("_", 1)[-1]
            self.fetched.append(jid)
            return {"jobPostingInfo": detail_for[jid]}

        def fake_post(url, payload, *a, **kw):
            off = payload.get("offset", 0)
            page = listing["jobPostings"][off:off + payload.get("limit", 20)]
            # FAITHFUL TO THE REAL API: Workday reports `total` on the
            # FIRST page only and 0 on every page after. Reading it each
            # time ends the walk at offset 40 (40 >= 0) and silences the
            # cap note with it — a 2,000-role board reporting 40 roles and
            # claiming full coverage. A fixture that returns total on every
            # page cannot see that, which is how it reached a live run.
            return {"total": listing["total"] if off == 0 else 0,
                    "jobPostings": page}

        with mock.patch.object(jobboards.jobshared, "http_post_json",
                               side_effect=fake_post), \
                mock.patch.object(jobboards.jobshared, "http_get",
                                  side_effect=fake_get):
            return jobboards.fetch_workday(WD_BOARD)

    def test_ambiguous_locations_are_kept_and_resolved_by_detail(self):
        # "5 Locations" is what the listing says for a multi-site posting.
        # Judging that ineligible would silently hide exactly the roles
        # posted in the most places -- New York among them.
        listing = _wd_listing(("A1", "5 Locations"), ("A2", "Munich, Germany"))
        out, note = self._run(listing, {
            "A1": {"title": "Engineer A1", "location": "New York, NY",
                   "startDate": "2026-08-01", "jobDescription": "Build it."},
        })
        self.assertEqual(self.fetched, ["A1"])      # Munich never cost a call
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["locations"], ["New York, NY"])
        self.assertEqual(out[0]["published"], "2026-08-01")
        self.assertTrue(jobboards.eligible_location(out[0], NYC_RULE()))
        self.assertEqual(note, "")

    def test_uid_namespace_is_the_tenant_not_the_slug(self):
        # board_uid and uid_prefix must agree, or a board owns a namespace
        # it never mints into -- the frontier-split failure, one ATS over.
        uid = jobboards.jobshared.board_uid("workday", "A1", WD_SLUG)
        prefix = jobboards.jobshared.uid_prefix("workday", WD_SLUG)
        self.assertEqual(uid, "wd-acme-A1")
        self.assertTrue(uid.startswith(prefix))

    def test_a_capped_walk_reports_what_it_dropped(self):
        rows = [(f"J{i}", "New York, NY") for i in range(120)]
        detail = {f"J{i}": {"title": f"Engineer J{i}",
                            "location": "New York, NY",
                            "startDate": "2026-08-01",
                            "jobDescription": "Build it."} for i in range(120)}
        out, note = self._run(_wd_listing(*rows), detail)
        self.assertEqual(len(out), jobboards.WD_DETAIL_CAP)
        self.assertIn("detail cap", note)
        self.assertIn(str(len(rows)), note)

    def test_a_page_capped_walk_still_reports_the_listing_total(self):
        # The regression for the live-caught bug, and it pins the NOTE
        # rather than the walk: only page one carries `total`, so a fetcher
        # that re-reads it each page ends up holding 0 and the listing note
        # — "walked N of M" — silently never fires. The walk itself is
        # rescued by the falsy-total guard, which is exactly why asserting
        # on the roles alone passes against the bug.
        n = jobboards.WD_PAGE_CAP * jobboards.WD_PAGE + 100
        rows = [(f"J{i}", "New York, NY") for i in range(n)]
        detail = {f"J{i}": {"title": f"Engineer J{i}",
                            "location": "New York, NY",
                            "startDate": "2026-08-01",
                            "jobDescription": "Build it."} for i in range(n)}
        out, note = self._run(_wd_listing(*rows), detail)
        self.assertIn("listings", note)
        self.assertIn(str(n), note)                       # the true total
        self.assertEqual(len(out), jobboards.WD_DETAIL_CAP)

    def test_workday_is_not_a_full_board(self):
        # Both walks are capped, so absence from one sweep is never proof a
        # posting closed. Being in FULL_BOARD would close real roles.
        self.assertNotIn("workday", jobboards.FULL_BOARD)

    def test_registry_validates_the_three_part_slug(self):
        with self.assertRaises(ValueError):
            jobboards._wd_parts("acme.wd5.myworkdayjobs.com")
        self.assertEqual(jobboards._wd_parts(WD_SLUG),
                         ("acme.wd5.myworkdayjobs.com", "acme", "AcmeCareers"))


class BoardUrlParse(unittest.TestCase):

    def test_reads_each_supported_ats_off_a_url(self):
        cases = {
            "https://job-boards.greenhouse.io/databricks": ("greenhouse", "databricks"),
            "https://boards.greenhouse.io/figma/jobs/12345": ("greenhouse", "figma"),
            "https://jobs.ashbyhq.com/sierra": ("ashby", "sierra"),
            "https://jobs.ashbyhq.com/mistral.ai/some-role": ("ashby", "mistral.ai"),
            "https://jobs.lever.co/palantir/abc-123": ("lever", "palantir"),
        }
        for url, (ats, slug) in cases.items():
            got = jobboards.parse_board_url(url)
            self.assertIsNotNone(got, url)
            self.assertEqual((got["ats"], got["slug"]), (ats, slug), url)

    def test_workday_locale_segment_is_not_the_site(self):
        # .../en-US/NVIDIAExternalCareerSite -- reading the first path
        # segment as the site is the trap, and it yields a dead board.
        for url in ("https://acme.wd5.myworkdayjobs.com/AcmeCareers",
                    "https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers",
                    "https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers"
                    "/job/New-York/Engineer_JR1"):
            got = jobboards.parse_board_url(url)
            self.assertEqual(got["slug"], WD_SLUG, url)
            self.assertEqual(got["ats"], "workday")

    def test_unknown_url_is_refused_not_guessed(self):
        self.assertIsNone(jobboards.parse_board_url(
            "https://example.com/careers"))
        self.assertIsNone(jobboards.parse_board_url(""))

    def test_resolve_refuses_a_board_that_does_not_answer(self):
        with mock.patch.dict(jobboards.FETCHERS, {
                "ashby": mock.Mock(side_effect=OSError("nope"))}):
            r = jobboards.resolve_board_url("https://jobs.ashbyhq.com/nope")
        self.assertFalse(r["ok"])
        self.assertIn("did not answer", r["reason"])

    def test_resolve_confirms_with_a_count(self):
        with mock.patch.dict(jobboards.FETCHERS, {
                "ashby": mock.Mock(return_value=[{}, {}, {}])}):
            r = jobboards.resolve_board_url("https://jobs.ashbyhq.com/sierra")
        self.assertTrue(r["ok"] and r["confirmed"])
        self.assertEqual(r["count"], 3)
        self.assertEqual(r["slug"], "sierra")


class QueryBoardRemotePass(unittest.TestCase):
    """The second pass is what catches US-remote roles filed outside the
    narrowed city. It was described in the docstring from day one and never
    ran, so a work-from-home role was invisible unless it also carried the
    city."""

    def _locations(self, board):
        seen = []

        def fake_get(url, *a, **kw):
            seen.append(url)
            return {"data": {"count": 0, "positions": []}}
        with mock.patch.object(jobboards, "_get", side_effect=fake_get):
            jobboards.fetch_microsoft(board)
        return seen

    def test_a_narrowed_board_also_sweeps_remote(self):
        urls = self._locations({"company": "MS AI", "ats": "microsoft",
                                "query": "AI", "location": "New York"})
        self.assertTrue(any("New%20York" in u for u in urls))
        self.assertTrue(any("Remote" in u for u in urls))

    def test_a_board_can_opt_out(self):
        urls = self._locations({"company": "MS AI", "ats": "microsoft",
                                "query": "AI", "location": "New York",
                                "remote_pass": False})
        self.assertFalse(any("Remote" in u for u in urls))


class PartialCoverageIsReported(PollDiffAndNotify):
    """A fetcher that bounds its own coverage returns (roles, note); the
    sweep must carry that note onto the board rather than presenting a
    capped list as the whole board."""

    def test_poll_records_a_fetchers_cap_note(self):
        (self.dir / "boards.json").write_text(
            json.dumps({"boards": [dict(WD_BOARD)]}), encoding="utf-8")
        role = jobboards._norm(WD_BOARD, uid="wd-acme-A1", title="Engineer",
                               locations=["New York, NY"],
                               url="https://x/y", published="2026-08-01",
                               jd="Build it.")
        with mock.patch.dict(jobboards.FETCHERS, {
                "workday": mock.Mock(return_value=([role], "detail cap hit"))}):
            with mock.patch("server.notify.agent_ping", return_value=True):
                jobboards.poll_once()
        snap = json.loads(
            (self.dir / "snapshot.json").read_text(encoding="utf-8"))
        meta = snap["boards"][jobboards._board_id(WD_BOARD)]
        self.assertTrue(meta["ok"])
        self.assertEqual(meta["partial"], "detail cap hit")


if __name__ == "__main__":
    unittest.main()
