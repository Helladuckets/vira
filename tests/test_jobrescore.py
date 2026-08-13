"""Rescoring a role's analysis against the current canon.

Everything roots at ONE tmp universe. `test_an_empty_fixture_rescores_
nothing` is the isolation guard: this module reads the universe dir, the
owner's canon (twice — once for staleness, once through resumeview's
retrieval), the boards snapshot and the role catalog, so a case added
later that resolves any of those from settings instead of the fixture
fails there rather than quietly reading the owner's real self-record.
"""
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from server import jobboards, jobrescore, jobscores

JD = {"text": "We need someone to deploy models with customers.",
      "source": "snapshot", "as_of": "2026-08-01T00:00:00+00:00",
      "reason": ""}

GOOD = json.dumps({
    "fit": 82, "screen": 55, "tier": "1", "final_tier": "1",
    "lane": "forward deployed", "why_fit": "He has done exactly this work "
    "with real customers, and the record carries the evidence for it.",
    "lead_with": "the deployment record", "caveat": "no formal ML training",
    "comp_note": "base only", "verdict": "confirm"})


def role(uid="a-1", **kw):
    base = {"uid": uid, "company": "Anthropic", "title": "Applied AI",
            "team": "", "family": "", "locations": ["New York"],
            "url": "https://example.com/j/1", "comp_kind": "base",
            "eligible": True, "cut": "", "availability": "open"}
    base.update(kw)
    return base


def score(uid="a-1", **kw):
    base = {"uid": uid, "fit": 60, "screen": 40, "tier": "2",
            "why_fit": "an earlier read of the same role, written in July",
            "scored_at": "2026-07-01T00:00:00+00:00", "canon": ""}
    base.update(kw)
    return base


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.udir = Path(self._tmp.name) / "universe"
        (self.udir / "candidate-universe" / "role").mkdir(parents=True)
        self.boards = Path(self._tmp.name) / "boards"
        self.boards.mkdir()
        self.addCleanup(self._tmp.cleanup)
        self.record = Path(self._tmp.name) / "record"
        (self.record / "canon").mkdir(parents=True)
        for target, value in (
                ("server.applications.self_record", self.record),
                ("server.applications.universe_dir", self.udir),
                ("server.jobboards.boards_dir", self.boards)):
            p = mock.patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        # A rescore must never be able to act on the world from a test.
        os.environ.pop("VIRA_PASSIVE", None)
        p = mock.patch("server.settings.fixture_mode", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    # ------------------------------------------------------------ fixture
    def canon(self, text="# History\n\nHe deployed models with customers.\n",
              when=None):
        f = self.record / "canon" / "MASTER_HISTORY.md"
        f.write_text(text, encoding="utf-8")
        if when is not None:
            os.utime(f, (when, when))
        return f

    def put_score(self, entry):
        d = self.udir / "scores"
        d.mkdir(exist_ok=True)
        (d / f"{entry['uid']}.json").write_text(json.dumps(entry),
                                                encoding="utf-8")

    def role_file(self, uid):
        (self.udir / "candidate-universe" / "role" / f"{uid}.json").write_text(
            json.dumps({"uid": uid}), encoding="utf-8")

    def universe(self, roles):
        p = mock.patch("server.applications.load_universe", return_value=roles)
        p.start()
        self.addCleanup(p.stop)

    def run_rescore(self, uid="a-1", mode="current", reply=GOOD, jd=None):
        with mock.patch("server.applications.find_role",
                        return_value=role(uid)), \
             mock.patch("server.jobdesc.describe",
                        return_value=jd or JD) as desc, \
             mock.patch("server.suggest.complete",
                        return_value=reply) as complete:
            out = jobrescore.rescore(uid, mode)
        return out, desc, complete


class Isolation(Base):
    def test_an_empty_fixture_rescores_nothing(self):
        self.universe([])
        self.assertEqual(jobrescore.queue(), [])
        self.assertEqual(jobrescore.batch_prompt(), ("", 0))
        self.assertEqual(jobrescore.status()["rescore_queue"], 0)


class CleanTests(Base):
    def test_the_prior_entrys_own_fields_ride_forward(self):
        prior = score(_fulluid="a-1-full", served="2026-06-01",
                      source_file="v2-raw-scores.json")
        out = jobrescore._clean(json.loads(GOOD), "a-1", prior, "current")
        self.assertEqual(out["_fulluid"], "a-1-full")
        self.assertEqual(out["served"], "2026-06-01")

    def test_the_earlier_passs_provenance_does_not(self):
        prior = score(source_file="v2-raw-scores.json",
                      scored_at="2026-07-01T00:00:00+00:00", canon="x",
                      prev={"fit": 1})
        out = jobrescore._clean(json.loads(GOOD), "a-1", prior, "current")
        for field in jobrescore.DROP_ON_REWRITE:
            self.assertNotIn(field, out)

    def test_the_server_decides_which_role_was_rescored(self):
        raw = json.loads(GOOD)
        raw["uid"] = "somebody-else"
        out = jobrescore._clean(raw, "a-1", None, "current")
        self.assertEqual(out["uid"], "a-1")

    def test_a_zero_screen_is_a_score_not_an_absence(self):
        raw = json.loads(GOOD)
        raw["screen"] = 0
        out = jobrescore._clean(raw, "a-1", None, "current")
        self.assertEqual(out["screen"], 0)

    def test_a_field_the_rescore_did_not_restate_is_dropped(self):
        # A score is ONE judgment, not a bag of independent fields: a July
        # caveat sitting beside an August fit would read as current when
        # nothing re-made it. The prompt asks for every field, so an
        # omission is the model declining to answer, never "unchanged".
        raw = json.loads(GOOD)
        del raw["caveat"]
        out = jobrescore._clean(raw, "a-1", score(caveat="the old caveat"),
                                "current")
        self.assertNotIn("caveat", out)

    def test_a_field_outside_the_vocabulary_is_ignored(self):
        raw = json.loads(GOOD)
        raw["scored_at"] = "2099-01-01T00:00:00+00:00"
        raw["nonsense"] = "x"
        out = jobrescore._clean(raw, "a-1", None, "current")
        self.assertNotIn("scored_at", out)
        self.assertNotIn("nonsense", out)

    def test_a_trivial_rescore_is_refused(self):
        raw = json.loads(GOOD)
        raw["why_fit"] = "Strong fit."
        with self.assertRaises(jobrescore.RescoreError):
            jobrescore._clean(raw, "a-1", None, "current")

    def test_a_non_object_reply_is_refused(self):
        with self.assertRaises(jobrescore.RescoreError):
            jobrescore._clean("not json", "a-1", None, "current")

    def test_a_single_object_wrapped_in_a_list_is_accepted(self):
        out = jobrescore._clean([json.loads(GOOD)], "a-1", None, "current")
        self.assertEqual(out["fit"], 82)

    def test_the_mode_is_recorded(self):
        out = jobrescore._clean(json.loads(GOOD), "a-1", None, "refetch")
        self.assertEqual(out["rescored_from"], "refetch")


class RescoreTests(Base):
    def setUp(self):
        super().setUp()
        self.canon()
        self.role_file("a-1")

    def test_it_writes_through_the_store_and_keeps_the_prior_one_deep(self):
        self.put_score(score())
        out, _desc, _c = self.run_rescore()
        self.assertEqual(out["status"], "ok")
        rec = jobscores.load(self.udir)["a-1"]
        self.assertEqual(rec["fit"], 82)
        self.assertEqual(rec["prev"]["fit"], 60)
        self.assertTrue(rec["scored_at"])
        self.assertTrue(rec["canon"])

    def test_the_fresh_score_is_not_stale(self):
        self.put_score(score())
        self.run_rescore()
        rec = jobscores.load(self.udir)["a-1"]
        self.assertFalse(jobscores.is_stale(rec, jobscores.canon_at(self.udir)))

    def test_it_reports_what_moved(self):
        self.put_score(score())
        out, _d, _c = self.run_rescore()
        self.assertEqual(out["was"]["fit"], 60)
        self.assertEqual(out["score"]["fit"], 82)

    def test_refetch_asks_for_a_fresh_posting_and_current_does_not(self):
        self.put_score(score())
        _out, desc, _c = self.run_rescore(mode="refetch")
        self.assertTrue(desc.call_args.kwargs["refresh"])
        _out, desc, _c = self.run_rescore(mode="current")
        self.assertFalse(desc.call_args.kwargs["refresh"])

    def test_a_junk_reply_leaves_the_score_on_disk_untouched(self):
        self.put_score(score())
        before = (self.udir / "scores" / "a-1.json").read_bytes()
        with self.assertRaises(jobrescore.RescoreError):
            self.run_rescore(reply='{"why_fit": "no"}')
        self.assertEqual((self.udir / "scores" / "a-1.json").read_bytes(),
                         before)

    def test_an_unknown_role_raises(self):
        with mock.patch("server.applications.find_role", return_value=None):
            with self.assertRaises(KeyError):
                jobrescore.rescore("nope")

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(jobrescore.RescoreError):
            jobrescore.rescore("a-1", "sideways")

    def test_fixture_mode_is_dormant(self):
        with mock.patch("server.settings.fixture_mode", return_value=True):
            self.assertEqual(jobrescore.rescore("a-1")["status"], "empty")

    def test_a_passive_instance_refuses_before_it_spends_the_model_call(self):
        os.environ["VIRA_PASSIVE"] = "1"
        self.addCleanup(os.environ.pop, "VIRA_PASSIVE", None)
        with mock.patch("server.suggest.complete") as complete:
            with self.assertRaises(PermissionError):
                jobrescore.rescore("a-1")
        complete.assert_not_called()


class PromptTests(Base):
    def setUp(self):
        super().setUp()
        self.canon()

    def compose(self, prior=None, jd=None, anchors=()):
        return jobrescore.prompt(role(), jd or JD, prior, list(anchors),
                                 jobrescore._ruling_lines(self.udir))

    def test_it_names_the_role_and_demands_strict_json(self):
        text = self.compose()
        self.assertIn("a-1", text)
        self.assertIn("STRICT JSON", text)
        self.assertIn("screen", text)

    def test_it_carries_the_prior_judgment(self):
        text = self.compose(prior=score())
        self.assertIn("an earlier read of the same role", text)
        self.assertIn("60", text)

    def test_a_long_prior_reasoning_is_truncated_and_says_so(self):
        text = self.compose(prior=score(why_fit="x" * 4000))
        self.assertIn(f"first {jobrescore.PRIOR_WHY_IN_PROMPT} characters",
                      text)
        self.assertNotIn("x" * (jobrescore.PRIOR_WHY_IN_PROMPT + 1), text)

    def test_an_undated_prior_score_says_undated_rather_than_guessing(self):
        text = self.compose(prior=score(scored_at=""))
        self.assertIn("before scores were dated", text)

    def test_a_missing_posting_is_named_not_hidden(self):
        text = self.compose(jd={"text": "", "source": "", "as_of": "",
                                "reason": "the board publishes no API."})
        self.assertIn("the board publishes no API.", text)
        self.assertIn("could not be read", text)

    def test_a_blurb_says_it_is_only_an_excerpt(self):
        text = self.compose(jd={**JD, "source": "blurb"})
        self.assertIn("OPENING EXCERPT ONLY", text)

    def test_the_standing_ruling_is_stated_inline(self):
        (self.udir / "owner-adjudication.json").write_text(json.dumps({
            "shortlist": [{"uid": "a-9"}],
            "cut": {"comp": ["ote"], "title_patterns": ["account executive"],
                    "reason_comp": "no commission",
                    "reason_title": "no sales"}}), encoding="utf-8")
        text = self.compose()
        self.assertIn("no commission", text)
        self.assertIn("account executive", text)
        self.assertIn("function label", text)

    def test_anchors_are_labelled_and_the_gate_ones_are_marked(self):
        text = self.compose(anchors=[
            {"text": "he deployed models", "heading": "Work", "gate": False},
            {"text": "say 'led', never 'owned'", "heading": "", "gate": True}])
        self.assertIn("[1] RECORD — Work: he deployed models", text)
        self.assertIn("[2] GATE:", text)
        self.assertIn("ANSWER ONLY FROM THESE", text)

    def test_no_anchors_is_stated_rather_than_left_blank(self):
        text = self.compose()
        self.assertIn("no passages could be retrieved", text)

    def test_the_retrieval_really_reaches_the_fixture_record(self):
        # _anchors swallows failures by design, so a broken pin would show up
        # as an empty anchor list rather than an error. This is the case that
        # would catch it — and it asserts on the fixture's OWN wording, so a
        # leak from the owner's real record fails here too.
        #
        # The record needs several passages to retrieve from at all: idf is
        # log(N / (1 + matches)), so on a one-passage corpus every token
        # scores negative and `_anchors_for` correctly returns nothing. The
        # real record carries 510.
        self.canon("\n".join([
            "# History", "",
            "## Deployment", "",
            "He ran customer deployments of large language models end to "
            "end, repeatedly, across regulated industries.", "",
            "## Trading", "",
            "He priced convertible bonds on a listed desk.", "",
            "## Writing", "",
            "He edited a quarterly investor letter for eight years.", "",
            "## Teaching", "",
            "He taught statistics to undergraduates.", "",
            "## Operations", "",
            "He rebuilt a vendor onboarding process end to end.", "",
            "## Product", "",
            "He shipped an internal search tool used daily.", "",
        ]))
        got = jobrescore._anchors(
            role(), "customer deployments of large language models")
        self.assertTrue(got, "retrieval returned nothing from the fixture "
                             "record — the resumeview corpus pin is broken")
        self.assertIn("deployments", got[0]["text"])


class QueueTests(Base):
    def setUp(self):
        super().setUp()
        self.canon(when=2_000_000_000)   # canon moved well after the scores

    def test_it_holds_only_scored_stale_actionable_roles(self):
        self.universe([
            role("keep"), role("unscored"),
            role("cut-role", cut="no commission"),
            role("ineligible", eligible=False),
            role("gone-role", availability="gone"),
            role("current-score"),
        ])
        for uid in ("keep", "cut-role", "ineligible", "gone-role"):
            self.put_score(score(uid, scored_at="2026-07-01T00:00:00+00:00"))
        self.put_score(score("current-score",
                             scored_at="2099-01-01T00:00:00+00:00"))
        self.assertEqual([r["uid"] for r, _e in jobrescore.queue()], ["keep"])

    def test_an_undated_score_is_in_the_queue(self):
        self.universe([role("undated")])
        self.put_score(score("undated", scored_at=""))
        self.assertEqual(len(jobrescore.queue()), 1)

    def test_an_unverified_posting_stays_in_the_queue(self):
        self.universe([role("maybe", availability="unverified")])
        self.put_score(score("maybe"))
        self.assertEqual(len(jobrescore.queue()), 1)

    def test_tier_leads_then_oldest_first(self):
        self.universe([role(u) for u in ("p", "t3", "t1", "t2", "t1old")])
        self.put_score(score("p", tier="pass",
                             scored_at="2020-01-01T00:00:00+00:00"))
        self.put_score(score("t3", tier="3"))
        self.put_score(score("t1", tier="1",
                             scored_at="2026-07-05T00:00:00+00:00"))
        self.put_score(score("t2", tier="2"))
        self.put_score(score("t1old", tier="1",
                             scored_at="2026-06-01T00:00:00+00:00"))
        self.assertEqual([r["uid"] for r, _e in jobrescore.queue()],
                         ["t1old", "t1", "t2", "t3", "p"])

    def test_final_tier_outranks_tier(self):
        self.universe([role("a"), role("b")])
        self.put_score(score("a", tier="1", final_tier="pass"))
        self.put_score(score("b", tier="pass", final_tier="1"))
        self.assertEqual([r["uid"] for r, _e in jobrescore.queue()],
                         ["b", "a"])

    def test_the_limit_takes_the_top_of_the_order(self):
        self.universe([role("t1"), role("t3")])
        self.put_score(score("t1", tier="1"))
        self.put_score(score("t3", tier="3"))
        rows = jobrescore.queue(limit=1)
        self.assertEqual([r["uid"] for r, _e in rows], ["t1"])

    def test_status_reports_the_queue_and_the_switch(self):
        self.universe([role("t1")])
        self.put_score(score("t1", tier="1"))
        st = jobrescore.status()
        self.assertEqual(st["rescore_queue"], 1)
        self.assertTrue(st["auto_rescore"])
        self.assertEqual(st["rescore_next"], ["t1"])


class BatchPromptTests(Base):
    def setUp(self):
        super().setUp()
        self.canon(when=2_000_000_000)
        self.universe([role("a-1"), role("a-2")])
        self.put_score(score("a-1", tier="1"))
        self.put_score(score("a-2", tier="3"))

    def test_it_files_through_the_write_tool_and_never_by_hand(self):
        text, n = jobrescore.batch_prompt()
        self.assertEqual(n, 2)
        self.assertIn("mcp__vira__record_role_scores", text)
        self.assertIn("Do NOT write or edit any score file yourself", text)
        self.assertNotIn("raw-scores.json", text)

    def test_it_carries_the_prior_score_and_its_date(self):
        text, _n = jobrescore.batch_prompt()
        self.assertIn("prior_why_fit", text)
        self.assertIn("2026-07-01", text)

    def test_it_names_the_canon_and_the_two_score_discipline(self):
        text, _n = jobrescore.batch_prompt()
        self.assertIn("MASTER_HISTORY.md", text)
        self.assertIn("TWO-SCORE", text)

    def test_the_batch_is_capped(self):
        text, n = jobrescore.batch_prompt(limit=1)
        self.assertEqual(n, 1)
        self.assertIn("ROLES TO RE-SCORE (1)", text)

    def test_an_empty_queue_composes_nothing(self):
        with mock.patch.object(jobrescore, "queue", return_value=[]):
            self.assertEqual(jobrescore.batch_prompt(), ("", 0))


class SchedulerTests(Base):
    """The second phase of jobboards.maybe_auto_score."""

    def setUp(self):
        super().setUp()
        for target in ("server.routines._ai_ready",):
            p = mock.patch(target, return_value=True)
            p.start()
            self.addCleanup(p.stop)

    def dispatch(self, unscored=0, queued=0, rescore_on=True):
        with mock.patch.object(jobboards, "score_prompt",
                               return_value=("score me", unscored)), \
             mock.patch.object(jobrescore, "batch_prompt",
                               return_value=("rescore me", queued)), \
             mock.patch.object(jobrescore, "auto_rescore_enabled",
                               return_value=rescore_on), \
             mock.patch("server.session.sessions.launch",
                        return_value="job1") as launch:
            out = jobboards.maybe_auto_score()
        return out, launch

    def test_unscored_roles_come_first(self):
        out, launch = self.dispatch(unscored=4, queued=99)
        self.assertEqual(out["kind"], "board-score")
        self.assertIn("score me", launch.call_args.args[0])

    def test_the_stale_queue_drains_once_nothing_is_unscored(self):
        out, launch = self.dispatch(unscored=0, queued=7)
        self.assertTrue(out["ok"])
        self.assertEqual(out["kind"], "board-rescore")
        self.assertEqual(out["roles"], 7)
        self.assertIn("rescore me", launch.call_args.args[0])
        self.assertEqual(launch.call_args.kwargs["meta"],
                         {"kind": "board-rescore", "machine": True})

    def test_the_dispatch_record_names_the_phase(self):
        self.dispatch(unscored=0, queued=7)
        st = jobboards.status()
        self.assertEqual(st["scoring"]["kind"], "board-rescore")

    def test_switching_the_drain_off_stops_it(self):
        out, launch = self.dispatch(unscored=0, queued=7, rescore_on=False)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "nothing to score or rescore")
        launch.assert_not_called()

    def test_both_phases_share_one_in_flight_guard(self):
        self.dispatch(unscored=0, queued=7)     # records a live dispatch
        with mock.patch.object(jobboards, "_score_job_live",
                               return_value=True):
            out, launch = self.dispatch(unscored=3, queued=7)
        self.assertEqual(out["reason"], "in flight")
        launch.assert_not_called()


class ScorePromptCarryover(Base):
    """The unscored pass files through the tool too — the last place that
    taught a session to hand-write a score file."""

    def test_it_points_at_the_write_tool_and_names_no_output_file(self):
        (self.boards / "boards.json").write_text(
            json.dumps({"boards": []}), encoding="utf-8")
        (self.boards / "snapshot.json").write_text(json.dumps({"roles": {
            "a-1": {"uid": "a-1", "company": "Anthropic", "title": "X",
                    "eligible": True, "cut": "", "closed": ""}}}),
            encoding="utf-8")
        text, n = jobboards.score_prompt()
        self.assertEqual(n, 1)
        self.assertIn("mcp__vira__record_role_scores", text)
        self.assertIn("screen (0-100)", text)
        self.assertNotIn("append a score entry", text)
        self.assertNotIn("raw-scores.json", text)


class BulkBase(Base):
    """The bulk runner: `rescore()` N times, a few at a time.

    Module state is global (one run at a time by construction), so every
    case resets it — otherwise a run left `running` by a failing case would
    make every later `bulk_start` refuse.
    """

    def setUp(self):
        super().setUp()
        self.reset_bulk()
        self.addCleanup(self.reset_bulk)

    def reset_bulk(self):
        jobrescore._bulk_cancel.clear()
        with jobrescore._bulk_lock:
            jobrescore._bulk = None

    def wait(self, timeout=10.0):
        """Block until the run is finished. Polls rather than joining: the
        pool's threads are not exposed, and this is what the UI does too."""
        end = time.time() + timeout
        while time.time() < end:
            st = jobrescore.bulk_status()
            if not st.get("running"):
                return st
            time.sleep(0.01)
        raise AssertionError("the bulk run never finished")

    def bulk(self, uids, mode="current", replies=None, fail=(), fits=None):
        """Run the real bulk over `uids` with the model stubbed.

        `fail` names uids whose model call raises; `fits` maps a uid to the
        fit its rescore returns, which is what drives the `moved` list.
        """
        for uid in uids:
            self.role_file(uid)
            self.put_score(score(uid))
        self.canon()

        def complete(prompt, **kw):
            uid = next((u for u in uids if u in prompt), "")
            if uid in fail:
                raise RuntimeError("the model refused")
            raw = json.loads(GOOD)
            if fits and uid in fits:
                raw["fit"] = fits[uid]
            return json.dumps(raw)

        with mock.patch("server.applications.find_role",
                        side_effect=lambda u: role(u)), \
             mock.patch("server.jobdesc.describe", return_value=JD), \
             mock.patch("server.suggest.complete", side_effect=complete):
            jobrescore.bulk_start(uids, mode)
            return self.wait()


class BulkRuns(BulkBase):
    def test_every_selected_role_is_rescored(self):
        st = self.bulk(["a-1", "a-2", "a-3"])
        self.assertEqual((st["total"], st["done"], st["ok"], st["failed"]),
                         (3, 3, 3, 0))
        for uid in ("a-1", "a-2", "a-3"):
            self.assertEqual(jobscores.load(self.udir)[uid]["fit"], 82)

    def test_the_prior_score_is_kept_one_deep(self):
        self.bulk(["a-1"])
        self.assertEqual(jobscores.load(self.udir)["a-1"]["prev"]["fit"], 60)

    def test_a_repeated_uid_is_rescored_once(self):
        st = self.bulk(["a-1", "a-1", "a-1"])
        self.assertEqual(st["total"], 1)

    def test_one_failing_role_never_stops_the_run(self):
        st = self.bulk(["a-1", "a-2", "a-3"], fail={"a-2"})
        self.assertEqual((st["ok"], st["failed"]), (2, 1))
        self.assertEqual([e["uid"] for e in st["errors"]], ["a-2"])
        self.assertEqual(jobscores.load(self.udir)["a-3"]["fit"], 82)
        # the one that failed keeps the read it already had
        self.assertEqual(jobscores.load(self.udir)["a-2"]["fit"], 60)

    def test_a_moved_fit_is_reported_and_an_unmoved_one_is_not(self):
        st = self.bulk(["a-1", "a-2"], fits={"a-1": 60, "a-2": 91})
        self.assertEqual(st["moved_total"], 1)
        self.assertEqual(st["moved"][0]["uid"], "a-2")
        self.assertEqual((st["moved"][0]["was"], st["moved"][0]["now"]),
                         (60, 91))

    def test_the_run_finishes_and_stamps_itself(self):
        st = self.bulk(["a-1"])
        self.assertFalse(st["running"])
        self.assertTrue(st["finished"])
        self.assertEqual(st["current"], [])

    def test_the_mode_reaches_the_posting_fetch(self):
        for uid in ("a-1",):
            self.role_file(uid)
            self.put_score(score(uid))
        self.canon()
        with mock.patch("server.applications.find_role",
                        side_effect=lambda u: role(u)), \
             mock.patch("server.jobdesc.describe",
                        return_value=JD) as desc, \
             mock.patch("server.suggest.complete", return_value=GOOD):
            jobrescore.bulk_start(["a-1"], "refetch")
            self.wait()
        self.assertTrue(desc.call_args.kwargs["refresh"])


class BulkRefusals(BulkBase):
    def test_an_empty_selection_is_refused(self):
        with self.assertRaises(jobrescore.RescoreError):
            jobrescore.bulk_start([])

    def test_a_blank_uid_does_not_count_as_a_selection(self):
        with self.assertRaises(jobrescore.RescoreError):
            jobrescore.bulk_start(["", "   "])

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(jobrescore.RescoreError):
            jobrescore.bulk_start(["a-1"], "sideways")

    def test_a_second_run_is_refused_while_one_is_live(self):
        with jobrescore._bulk_lock:
            jobrescore._bulk = {"running": True, "done": 1, "total": 4,
                                "current": [], "errors": [], "moved": []}
        with self.assertRaises(jobrescore.RescoreError) as cm:
            jobrescore.bulk_start(["a-1"])
        self.assertIn("already running", str(cm.exception))

    def test_passive_refuses_before_any_model_call(self):
        os.environ["VIRA_PASSIVE"] = "1"
        self.addCleanup(lambda: os.environ.pop("VIRA_PASSIVE", None))
        with mock.patch("server.suggest.complete") as complete:
            with self.assertRaises(PermissionError):
                jobrescore.bulk_start(["a-1"])
        complete.assert_not_called()
        self.assertFalse(jobrescore.bulk_status()["running"])

    def test_fixture_mode_is_dormant(self):
        with mock.patch("server.settings.fixture_mode", return_value=True):
            with self.assertRaises(jobrescore.RescoreError):
                jobrescore.bulk_start(["a-1"])


class BulkCancel(BulkBase):
    def test_cancelling_skips_the_rest_and_keeps_what_landed(self):
        uids = [f"a-{i}" for i in range(1, 13)]
        for uid in uids:
            self.role_file(uid)
            self.put_score(score(uid))
        self.canon()
        seen = []

        def complete(prompt, **kw):
            seen.append(1)
            if len(seen) == 1:
                jobrescore.bulk_cancel()
            return GOOD

        with mock.patch("server.applications.find_role",
                        side_effect=lambda u: role(u)), \
             mock.patch("server.jobdesc.describe", return_value=JD), \
             mock.patch("server.suggest.complete", side_effect=complete):
            jobrescore.bulk_start(uids)
            st = self.wait()
        self.assertTrue(st["cancelled"])
        # every uid is resolved, so progress reaches its own total
        self.assertEqual(st["done"], st["total"])
        self.assertEqual(st["ok"] + st["failed"] + st["skipped"], st["total"])
        self.assertTrue(st["skipped"])
        # the passes already in flight were left to land, never discarded
        self.assertTrue(st["ok"])
        self.assertLess(len(seen), len(uids))

    def test_cancelling_when_nothing_runs_is_harmless(self):
        self.assertFalse(jobrescore.bulk_cancel()["running"])


class BulkStatusShape(BulkBase):
    def test_workers_rides_the_idle_answer_too(self):
        # the client quotes an estimate before any run exists, so a second
        # copy of this number in the frontend would be one that can drift
        self.assertEqual(jobrescore.bulk_status()["workers"],
                         jobrescore.BULK_WORKERS)

    def test_the_snapshot_is_a_copy_not_the_live_record(self):
        st = self.bulk(["a-1"])
        st["errors"].append({"uid": "invented"})
        self.assertEqual(jobrescore.bulk_status()["errors"], [])

    def test_the_error_list_is_capped_but_the_count_is_true(self):
        with jobrescore._bulk_lock:
            jobrescore._bulk = {
                "running": False, "total": 0, "done": 0, "current": [],
                "errors": [{"uid": str(i)}
                           for i in range(jobrescore.ERR_CAP)],
                "moved": [], "errors_total": 500, "moved_total": 0}
        st = jobrescore.bulk_status()
        self.assertEqual(len(st["errors"]), jobrescore.ERR_CAP)
        self.assertEqual(st["errors_total"], 500)


if __name__ == "__main__":
    unittest.main()
