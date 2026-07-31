"""Lesson recurrence: the corrections-ledger read-back.

Covers the two-rung split: ledger parse, the active_from ladder, evidence
gathering (results JSON primary, markdown fallback, both dedupe axes), the
no-model restatement path, the grounded adjudication contract, distinct-
session counting, owner overrides, promotion proposals (minted once, never
a ledger write), tier-1 flagging, retirement, and the route layer.

Isolation is the readinglist lesson (2026-07-28): every source root is a
named module global, ALL of them are repointed at ONE tmp fixture here,
and test_an_empty_fixture_root_counts_nothing is the guard — a source
added later that reads the real machine fails it on sight. Patching a
store isolates what a function WRITES; the roots are what it READS.

Run: .venv/bin/python -m unittest discover tests
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import ideas, lessonwatch


RULE_A = "Run the unit test suite in the live tree before merging any branch."
RULE_B = "Never write directly to the production data store from a session."
RULE_C = ("Check the corpus with a literal grep before blaming ranking "
          "for a retrieval miss.")

BREAK_A = ("Skipped the unit test suite in the live tree before merging "
           "the branch.")
BREAK_B = ("Wrote directly to the production data store, corrupting a "
           "profile.")
BREAK_C = ("Blamed ranking for the retrieval miss without a literal grep "
           "over the corpus.")
RESTATE_A = ("Run the unit test suite in the live tree before merging any "
             "branch of the repo.")
UNRELATED = "Spent an hour debugging a stylesheet color token by hand."


def write_ledger(path, lines):
    body = "# LESSONS\n\nheader prose\n\n" + "\n".join(lines) + "\n"
    path.write_text(body, encoding="utf-8")


def rule_line(tier, text, stamp="2026-07-01", source="manual"):
    return f"- [tier {tier}] {text}  ({stamp}) <{source}>"


def write_result(results_dir, day, sid, project="alpha",
                 reversals=(), lessons=()):
    d = results_dir / day
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.json").write_text(json.dumps({
        "row": {"session_id": sid, "day": day, "project": project},
        "obj": {"reversals": list(reversals),
                "lessons": [{"text": t} for t in lessons]},
    }), encoding="utf-8")


def write_session_retro(dir_, name, day, sid, project="alpha",
                        reversals=(), heading="Reversals and dead ends"):
    bullets = "\n".join(f"- {t}" for t in reversals) or "_none_"
    (Path(dir_) / name).write_text(f"""---
tags: [session, retrospective, auto]
project: {project}
date: {day}
session_id: {sid}
---

# {project} - {day}

## Goal

Do the work.

## {heading}

{bullets}

## Open

_none_
""", encoding="utf-8")


class _Case(unittest.TestCase):
    """One tmp fixture; every lessonwatch source root repointed into it.
    The model, the tier-1 ping, and the embedder are stubbed in setUp so
    no test can call out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ledger = root / "LESSONS.md"
        self.state = root / "sessions"
        self.results = self.state / "results"
        self.results.mkdir(parents=True)
        self.retros = root / "retros"
        self.retros.mkdir()
        self.repo_retros = root / "repo-retros"
        self.repo_retros.mkdir()
        self.store = root / "lesson-recurrence.json"
        self.ideas_store = root / "ideas.json"
        for target, value in (
            ("LEDGER_PATH", self.ledger),
            ("STATE_DIR", self.state),
            ("RESULTS_DIR", self.results),
            ("PROPOSED_PATH", self.state / "lessons-proposed.jsonl"),
            ("DECIDED_PATH", self.state / "lessons-decided.jsonl"),
            ("RETRO_DIRS", [self.retros, self.repo_retros]),
            ("STORE", self.store),
        ):
            p = mock.patch.object(lessonwatch, target, value)
            p.start()
            self.addCleanup(p.stop)
        ip = mock.patch.object(ideas, "STORE", self.ideas_store)
        ip.start()
        self.addCleanup(ip.stop)
        ep = mock.patch.object(lessonwatch, "_embed", return_value=None)
        ep.start()
        self.addCleanup(ep.stop)
        self.pings = []
        pp = mock.patch.object(lessonwatch, "_ping_tier1",
                               side_effect=lambda r, c:
                               self.pings.append((r["id"], c)))
        pp.start()
        self.addCleanup(pp.stop)
        # default model: adjudicates every candidate as a grounded break,
        # and answers the mechanism prompt with a plausible paragraph
        cp = mock.patch("server.suggest.complete",
                        side_effect=self._model)
        cp.start()
        self.addCleanup(cp.stop)

    def _model(self, prompt):
        if "Recommend, in ONE short paragraph" in prompt:
            return ("Add a preflight row that runs the suite in the live "
                    "tree and refuses the merge when it is red.")
        import re
        verdicts = []
        for m in re.finditer(r"\[(ev_[0-9a-f]+)\] \([^)]*\)\n(.+)", prompt):
            verdicts.append({"id": m.group(1), "breaks": True,
                             "quote": m.group(2).strip(),
                             "why": "same mistake"})
        return json.dumps({"verdicts": verdicts})


# ---------------------------------------------------------------- ledger

class LedgerParse(_Case):
    def test_lines_parse_and_malformed_are_ignored(self):
        write_ledger(self.ledger, [
            rule_line(2, RULE_A, "2026-07-01"),
            rule_line(1, RULE_B, "2026-07-02", source="manual"),
            "- [tier x] not a real line",
            "- just prose",
        ])
        rules = lessonwatch.ledger()
        self.assertEqual(len(rules), 2)
        a = next(r for r in rules if r["text"] == RULE_A)
        self.assertEqual(a["tier"], 2)
        self.assertEqual(a["written"], "2026-07-01")
        self.assertEqual(a["source"], "manual")
        self.assertEqual(a["active_from"], "2026-07-01")
        self.assertEqual(a["active_from_source"], "line")

    def test_missing_ledger_is_dormant_not_an_exception(self):
        self.assertEqual(lessonwatch.ledger(), [])
        out = lessonwatch.run_pass(adjudicate=False)
        self.assertTrue(out.get("dormant"))
        rep = lessonwatch.report()
        self.assertEqual(rep["rules"], [])
        self.assertFalse(rep["status"]["ledger_exists"])

    def test_active_from_prefers_the_decided_join(self):
        write_ledger(self.ledger, [rule_line(2, RULE_C, "2026-07-05")])
        (self.state / "lessons-proposed.jsonl").write_text(
            json.dumps({"id": "L1", "text": RULE_C}) + "\n",
            encoding="utf-8")
        (self.state / "lessons-decided.jsonl").write_text(
            json.dumps({"id": "L1", "status": "approved",
                        "at": "2026-07-10"}) + "\n", encoding="utf-8")
        r = lessonwatch.ledger()[0]
        self.assertEqual(r["active_from"], "2026-07-10")
        self.assertEqual(r["active_from_source"], "decided")

    def test_a_break_before_active_from_is_not_counted(self):
        write_ledger(self.ledger, [rule_line(2, RULE_A, "2026-07-01")])
        write_result(self.results, "2026-06-20", "s0",
                     reversals=[BREAK_A])
        lessonwatch.run_pass(adjudicate=True)
        rep = lessonwatch.report()
        self.assertEqual(rep["rules"][0]["count"], 0)


# ---------------------------------------------------------------- evidence

class EvidenceGather(_Case):
    def test_results_json_is_read(self):
        write_result(self.results, "2026-07-15", "s1",
                     reversals=[BREAK_A], lessons=[RESTATE_A])
        items = lessonwatch._evidence()
        kinds = sorted(i["kind"] for i in items)
        self.assertEqual(kinds, ["restated", "reversal"])

    def test_markdown_fallback_and_hand_written_heading(self):
        write_session_retro(self.retros, "2026-07-15 0900 alpha.md",
                            "2026-07-15", "s1", reversals=[BREAK_A])
        write_session_retro(self.repo_retros, "hand.md", "2026-07-16",
                            "hand-1", reversals=[BREAK_C],
                            heading="What Didn't Work (Lessons Learned)")
        items = lessonwatch._evidence()
        self.assertEqual(len(items), 2)
        self.assertEqual({i["kind"] for i in items}, {"reversal"})

    def test_same_reversal_in_results_and_rendered_markdown_counts_once(self):
        write_result(self.results, "2026-07-15", "s1", reversals=[BREAK_A])
        write_session_retro(self.retros, "2026-07-15 0900 alpha.md",
                            "2026-07-15", "s1", reversals=[BREAK_A])
        items = lessonwatch._evidence()
        self.assertEqual(len(items), 1)
        # results is the primary — the markdown is a rendering of it
        self.assertEqual(items[0]["retro"], "")

    def test_a_day_retro_rerendering_a_bullet_does_not_double_count(self):
        write_result(self.results, "2026-07-16", "s2", project="beta",
                     reversals=[BREAK_A])
        # the day aggregate re-renders the bullet verbatim under its own
        # file identity — a different session key, same text
        write_session_retro(self.retros, "2026-07-16 day beta.md",
                            "2026-07-16", "day-beta",
                            reversals=[BREAK_A], heading="Reversals")
        items = lessonwatch._evidence()
        self.assertEqual(len(items), 1)


# ---------------------------------------------------------------- rung 1

class Restatements(_Case):
    def test_restatement_counts_with_no_model_at_all(self):
        write_ledger(self.ledger, [rule_line(2, RULE_A, "2026-07-01")])
        write_result(self.results, "2026-07-15", "s1",
                     lessons=[RESTATE_A])
        with mock.patch("server.suggest.complete",
                        side_effect=RuntimeError("no AI connected")):
            out = lessonwatch.run_pass(adjudicate=True)
        self.assertFalse(out.get("dormant"))
        rep = lessonwatch.report()
        self.assertEqual(rep["rules"][0]["count"], 1)
        self.assertEqual(rep["rules"][0]["breaks"][0]["kind"], "restated")

    def test_candidates_pend_when_the_model_is_down(self):
        write_ledger(self.ledger, [rule_line(2, RULE_A, "2026-07-01")])
        write_result(self.results, "2026-07-15", "s1",
                     reversals=[BREAK_A])
        with mock.patch("server.suggest.complete",
                        side_effect=RuntimeError("down")):
            lessonwatch.run_pass(adjudicate=True)
        st = lessonwatch.status()
        self.assertEqual(st["pending_adjudication"], 1)
        # nothing is counted on candidate score alone
        self.assertEqual(lessonwatch.report()["rules"][0]["count"], 0)


# ---------------------------------------------------------------- rung 2

class VerdictContract(unittest.TestCase):
    CANDS = [{"id": "ev_aaaaaaaaaaaa", "text": "The tree broke on merge."},
             {"id": "ev_bbbbbbbbbbbb", "text": "A different mistake."}]

    def test_ungrounded_quote_is_demoted_and_counted(self):
        raw = {"verdicts": [
            {"id": "ev_aaaaaaaaaaaa", "breaks": True,
             "quote": "words that are not in the item", "why": "w"}]}
        out, ung = lessonwatch._clean_verdicts(raw, self.CANDS)
        self.assertEqual(ung, 1)
        self.assertFalse(out["ev_aaaaaaaaaaaa"]["breaks"])

    def test_unknown_and_duplicate_ids_are_dropped(self):
        raw = {"verdicts": [
            {"id": "ev_zzzzzzzzzzzz", "breaks": True, "quote": "x"},
            {"id": "ev_aaaaaaaaaaaa", "breaks": True,
             "quote": "tree broke", "why": ""},
            {"id": "ev_aaaaaaaaaaaa", "breaks": False, "quote": ""}]}
        out, _ = lessonwatch._clean_verdicts(raw, self.CANDS)
        self.assertNotIn("ev_zzzzzzzzzzzz", out)
        self.assertTrue(out["ev_aaaaaaaaaaaa"]["breaks"])  # first wins

    def test_an_omitted_candidate_is_false_not_absent(self):
        out, _ = lessonwatch._clean_verdicts({"verdicts": []}, self.CANDS)
        self.assertEqual(len(out), 2)
        self.assertFalse(out["ev_bbbbbbbbbbbb"]["breaks"])

    def test_a_grounded_quote_survives_whitespace_drift(self):
        raw = {"verdicts": [
            {"id": "ev_aaaaaaaaaaaa", "breaks": True,
             "quote": "tree  broke on\nmerge", "why": ""}]}
        out, ung = lessonwatch._clean_verdicts(raw, self.CANDS)
        self.assertEqual(ung, 0)
        self.assertTrue(out["ev_aaaaaaaaaaaa"]["breaks"])


# ---------------------------------------------------------------- counting

class Counting(_Case):
    def _three_sessions(self):
        write_ledger(self.ledger, [rule_line(2, RULE_A, "2026-07-01")])
        write_result(self.results, "2026-07-15", "s1",
                     reversals=[BREAK_A,
                                "Also merged a second branch with the "
                                "unit test suite never run in the live "
                                "tree.",
                                UNRELATED])
        write_result(self.results, "2026-07-16", "s2", project="beta",
                     reversals=[BREAK_A + " Again."])
        write_result(self.results, "2026-07-17", "s3", project="gamma",
                     reversals=["Shipped after skipping the unit test "
                                "suite in the live tree before merging "
                                "the branch once more."])

    def test_one_session_three_bullets_counts_once(self):
        write_ledger(self.ledger, [rule_line(2, RULE_A, "2026-07-01")])
        write_result(self.results, "2026-07-15", "s1",
                     reversals=[BREAK_A,
                                "Also merged a second branch with the "
                                "unit test suite never run in the live "
                                "tree.",
                                "Then skipped the unit test suite in "
                                "the live tree on a third branch merge."])
        lessonwatch.run_pass(adjudicate=True)
        rep = lessonwatch.report()
        self.assertEqual(rep["rules"][0]["count"], 1)
        self.assertGreaterEqual(len(rep["rules"][0]["breaks"]), 2)

    def test_three_sessions_cross_the_threshold_and_propose_once(self):
        self._three_sessions()
        out = lessonwatch.run_pass(adjudicate=True)
        self.assertEqual(out["proposals"], 1)
        rep = lessonwatch.report()
        row = rep["rules"][0]
        self.assertEqual(row["count"], 3)
        self.assertTrue(row["at_threshold"])
        self.assertTrue(row["proposal"])
        staged = [i for i in ideas.list_items()
                  if i["source"] == "lesson-recurrence"]
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]["status"], "proposed")
        self.assertIn("unit test suite", staged[0]["text"])
        # citations verified: the note names a file that exists
        for p in staged[0]["note"].replace("evidence: ", "").split("; "):
            if p:
                self.assertTrue(Path(p).exists(), p)
        # a second pass does not re-mint while the proposal is live
        out2 = lessonwatch.run_pass(adjudicate=True)
        self.assertEqual(out2["proposals"], 0)
        self.assertEqual(len([i for i in ideas.list_items()
                              if i["source"] == "lesson-recurrence"]), 1)

    def test_a_declined_proposal_waits_for_the_count_to_grow(self):
        self._three_sessions()
        lessonwatch.run_pass(adjudicate=True)
        staged = [i for i in ideas.list_items()
                  if i["source"] == "lesson-recurrence"][0]
        ideas.update(staged["id"], status="dropped")
        # count unchanged -> no re-mint
        out = lessonwatch.run_pass(adjudicate=True)
        self.assertEqual(out["proposals"], 0)

    def test_owner_override_survives_a_rerun(self):
        write_ledger(self.ledger, [rule_line(2, RULE_A, "2026-07-01")])
        write_result(self.results, "2026-07-15", "s1", reversals=[BREAK_A])
        lessonwatch.run_pass(adjudicate=True)
        row = lessonwatch.report()["rules"][0]
        self.assertEqual(row["count"], 1)
        ev_id = row["breaks"][0]["id"]
        lessonwatch.set_break(row["id"], ev_id, False)
        self.assertEqual(lessonwatch.report()["rules"][0]["count"], 0)
        lessonwatch.run_pass(adjudicate=True)
        after = lessonwatch.report()["rules"][0]
        self.assertEqual(after["count"], 0)
        s = json.loads(self.store.read_text(encoding="utf-8"))
        v = s["verdicts"][f"{row['id']}:{ev_id}"]
        self.assertTrue(v["owner"])
        self.assertFalse(v["breaks"])


# ---------------------------------------------------------------- tier 1

class TierOne(_Case):
    def test_tier1_flags_and_pings_but_never_proposes(self):
        write_ledger(self.ledger, [rule_line(1, RULE_B, "2026-07-01")])
        write_result(self.results, "2026-07-18", "s5", reversals=[BREAK_B])
        out = lessonwatch.run_pass(adjudicate=True)
        self.assertEqual(out["flags"], 1)
        self.assertEqual(out["proposals"], 0)
        self.assertEqual(len(self.pings), 1)
        row = lessonwatch.report()["rules"][0]
        self.assertTrue(row["guard_did_not_hold"])
        self.assertFalse(row["at_threshold"])
        self.assertEqual(ideas.list_items(), [])
        with self.assertRaises(ValueError):
            lessonwatch.force_propose(row["id"])

    def test_tier1_failures_sort_first_in_the_report(self):
        write_ledger(self.ledger, [rule_line(2, RULE_A, "2026-07-01"),
                                   rule_line(1, RULE_B, "2026-07-01")])
        write_result(self.results, "2026-07-15", "s1", reversals=[BREAK_A])
        write_result(self.results, "2026-07-18", "s5", reversals=[BREAK_B])
        lessonwatch.run_pass(adjudicate=True)
        rows = lessonwatch.report()["rules"]
        self.assertEqual(rows[0]["tier"], 1)
        self.assertTrue(rows[0]["guard_did_not_hold"])


# ---------------------------------------------------------------- lifecycle

class Retirement(_Case):
    def test_a_rule_dropped_from_the_ledger_keeps_its_counts(self):
        write_ledger(self.ledger, [rule_line(2, RULE_A, "2026-07-01"),
                                   rule_line(2, RULE_C, "2026-07-01")])
        write_result(self.results, "2026-07-15", "s1", reversals=[BREAK_A])
        lessonwatch.run_pass(adjudicate=True)
        rid = lessonwatch.rule_id(RULE_A)
        write_ledger(self.ledger, [rule_line(2, RULE_C, "2026-07-01")])
        lessonwatch.run_pass(adjudicate=True)
        rows = {r["id"]: r for r in lessonwatch.report()["rules"]}
        self.assertTrue(rows[rid]["retired"])
        self.assertEqual(rows[rid]["count"], 1)

    def test_dismissed_rule_never_proposes(self):
        write_ledger(self.ledger, [rule_line(2, RULE_A, "2026-07-01")])
        write_result(self.results, "2026-07-15", "s1", reversals=[BREAK_A])
        lessonwatch.run_pass(adjudicate=False)
        lessonwatch.set_dismissed(lessonwatch.rule_id(RULE_A), True)
        write_result(self.results, "2026-07-16", "s2", reversals=[BREAK_A])
        write_result(self.results, "2026-07-17", "s3", reversals=[BREAK_A])
        out = lessonwatch.run_pass(adjudicate=True)
        self.assertEqual(out["proposals"], 0)


class NeverWritesTheLedger(_Case):
    def test_the_pass_never_writes_the_ledger(self):
        write_ledger(self.ledger, [rule_line(2, RULE_A, "2026-07-01")])
        proposed = self.state / "lessons-proposed.jsonl"
        decided = self.state / "lessons-decided.jsonl"
        proposed.write_text(json.dumps({"id": "L9", "text": "x"}) + "\n",
                            encoding="utf-8")
        decided.write_text("", encoding="utf-8")
        # distinct texts: identical prose across sessions is deduped by
        # design (the day-retro re-render case), so each break differs
        for day, sid, extra in (("2026-07-15", "s1", ""),
                                ("2026-07-16", "s2", " Again."),
                                ("2026-07-17", "s3", " A third time.")):
            write_result(self.results, day, sid,
                         reversals=[BREAK_A + extra])
        before = (self.ledger.read_bytes(), proposed.read_bytes(),
                  decided.read_bytes())
        out = lessonwatch.run_pass(adjudicate=True)
        self.assertEqual(out["proposals"], 1)   # the full path ran
        after = (self.ledger.read_bytes(), proposed.read_bytes(),
                 decided.read_bytes())
        self.assertEqual(before, after)


class EmptyFixture(_Case):
    def test_an_empty_fixture_root_counts_nothing(self):
        # the isolation guard: everything repointed at an empty fixture
        # must observe an empty world — a later source that reads the
        # real machine fails here on sight
        self.assertEqual(lessonwatch._evidence(), [])
        self.assertEqual(lessonwatch.ledger(), [])
        self.assertTrue(lessonwatch.run_pass(adjudicate=False)["dormant"])
        self.assertEqual(lessonwatch.report()["rules"], [])


# ---------------------------------------------------------------- routes

class Routes(_Case):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from server import main
        cls.client = TestClient(main.app)

    def test_report_route(self):
        write_ledger(self.ledger, [rule_line(2, RULE_A, "2026-07-01")])
        r = self.client.get("/api/lessons")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("rules", body)
        self.assertIn("status", body)
        self.assertTrue(body["status"]["ledger_exists"])

    def test_refresh_route_starts_a_thread(self):
        r = self.client.post("/api/lessons/refresh",
                             json={"adjudicate": False})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["started"])

    def test_dismiss_and_break_routes(self):
        write_ledger(self.ledger, [rule_line(2, RULE_A, "2026-07-01")])
        write_result(self.results, "2026-07-15", "s1", reversals=[BREAK_A])
        lessonwatch.run_pass(adjudicate=True)
        rid = lessonwatch.rule_id(RULE_A)
        r = self.client.post(f"/api/lessons/{rid}/dismiss",
                             json={"dismissed": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            self.client.post("/api/lessons/nope/dismiss",
                             json={}).status_code, 404)
        ev_id = lessonwatch.report()["rules"][0]["breaks"][0]["id"]
        r = self.client.post("/api/lessons/break",
                             json={"rule_id": rid, "evidence_id": ev_id,
                                   "breaks": False})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["owner"])
        self.assertEqual(
            self.client.post("/api/lessons/break",
                             json={"rule_id": rid,
                                   "evidence_id": "ev_missing",
                                   "breaks": False}).status_code, 404)

    def test_propose_route_conflicts(self):
        self.assertEqual(
            self.client.post("/api/lessons/nope/propose").status_code, 404)


if __name__ == "__main__":
    unittest.main()
