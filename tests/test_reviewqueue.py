"""The needs-review queue: the aggregator behind the brief's picker.

Covers the registry contract (a source that raises costs only its own
rows), each source reader including its missing-file and empty cases, the
duplicated-proposal-id split that decides HOW a lesson is approved, and the
subprocess boundary — every ledger write goes through lessons.py, argv is
list-form and validated, and NO TEST EVER RUNS THE REAL SCRIPT: subprocess
is mocked in every case, so the owner's ledger is never touched.

Isolation follows the lessonwatch/readinglist lesson: every root this
module reads is repointed into ONE tmp fixture through the real settings
override keys, and test_every_root_points_into_the_fixture is the guard —
a root added later that reads the real machine fails it on sight.

Run: .venv/bin/python -m unittest discover tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import ideas, reviewqueue


PROPOSAL_A = "Never merge a branch without running the suite in the live tree."
PROPOSAL_B = "Say merged or not merged in one plain sentence, first line."
PROPOSAL_C = "Reproduce a retrieval miss with a literal grep before ranking."
PROPOSAL_D = "Never delete owner data without an explicit instruction."

FACTS_DOC = """# FACTS

## Some other section

- Not a flag at all.

## Open reconciliation flags (awaiting a ruling)

- **Comp band ambiguity.** The posting says one thing and the dossier
  another. Resolve at first contact.
- **~~Old question~~ - RESOLVED (2026-08-10).** Already ruled on.
- **Second live flag.** Still open.

## Privacy quarantine

- Nothing here should surface.
"""

HISTORY_DOC = """# Part IV. Something

- Not a flag.

# Part V. Open questions and contradictions

These issues remain visible.

- **Disposition reconciliation.** Two aggregates disagree.

# Part VI. Provenance

- Not a flag either.
"""


def proposal(pid, text, day="2026-07-25", tier=2, status="proposed"):
    return {"id": pid, "text": text, "tier": tier, "why": "because",
            "day": day, "project": "vira", "status": status}


class _Case(unittest.TestCase):
    """One tmp fixture; every reviewqueue root repointed into it through the
    real settings override keys, so the override path itself is exercised."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "sessions"
        self.state.mkdir()
        self.record = self.root / "self"
        (self.record / "inbox" / "notes").mkdir(parents=True)
        (self.record / "canon").mkdir(parents=True)
        self.script = self.root / "lessons.py"
        self.script.write_text("# stub\n", encoding="utf-8")
        self.cfg = {
            "lessons_script_path": str(self.script),
            "lessons_state_dir": str(self.state),
            "self_record": str(self.record),
        }
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(reviewqueue.settings, "raw", lambda: self.cfg)
        p.start()
        self.addCleanup(p.stop)
        # applications.self_record() reads the same config object
        from server import applications
        p2 = mock.patch.object(applications.settings, "raw", lambda: self.cfg)
        p2.start()
        self.addCleanup(p2.stop)
        p3 = mock.patch.object(reviewqueue, "DECISIONS",
                               self.root / "review-decided.json")
        p3.start()
        self.addCleanup(p3.stop)
        p4 = mock.patch.object(ideas, "STORE", self.root / "ideas.json")
        p4.start()
        self.addCleanup(p4.stop)

    # ---------------------------------------------------------- helpers

    def write_proposals(self, rows):
        (self.state / "lessons-proposed.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def write_decided(self, rows):
        (self.state / "lessons-decided.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def by_source(self, key):
        return [i for i in reviewqueue.items()["items"] if i["source"] == key]

    def run_ok(self, stdout="done"):
        return mock.Mock(returncode=0, stdout=stdout, stderr="")


class RegistryTests(_Case):

    def test_the_four_sources_are_registered(self):
        self.assertEqual(set(reviewqueue.SOURCES),
                         {"lessons", "inbox", "flags", "ideas"})

    def test_every_root_points_into_the_fixture(self):
        for name, path in reviewqueue.roots().items():
            self.assertTrue(str(path).startswith(str(self.root)),
                            f"{name} reads outside the fixture: {path}")

    def test_a_cold_machine_has_an_empty_quiet_queue(self):
        q = reviewqueue.items()
        self.assertEqual(q["total"], 0)
        self.assertEqual(q["errors"], {})
        self.assertIsNone(reviewqueue.summary())

    def test_a_broken_source_costs_only_its_own_rows(self):
        self.write_proposals([proposal("L1", PROPOSAL_A)])
        boom = mock.Mock(side_effect=OSError("store on fire"))
        with mock.patch.object(reviewqueue.SOURCES["inbox"], "reader", boom):
            q = reviewqueue.items()
        self.assertEqual(q["total"], 1)
        self.assertIn("inbox", q["errors"])
        self.assertIn("store on fire", q["errors"]["inbox"])

    def test_summary_leads_with_the_oldest_and_caps_the_top(self):
        self.write_proposals([
            proposal(f"L{i}", f"Proposal number {i} about something.",
                     day=f"2026-07-{10 + i:02d}") for i in range(8)])
        s = reviewqueue.summary()
        self.assertEqual(s["total"], 8)
        self.assertEqual(len(s["top"]), reviewqueue.BRIEF_TOP)
        self.assertEqual(s["top"][0]["date"], "2026-07-10")

    def test_act_rejects_an_unknown_source_and_a_read_only_one(self):
        with self.assertRaises(KeyError):
            reviewqueue.act("nosuch:1", "approve")
        with self.assertRaises(KeyError):
            reviewqueue.act("lessons", "approve")
        (self.record / "inbox" / "notes" / "2026-08-01-a-note.md").write_text(
            "a thought\n", encoding="utf-8")
        item = self.by_source("inbox")[0]
        with self.assertRaises(ValueError):
            reviewqueue.act(item["id"], "approve")


class LessonSourceTests(_Case):

    def test_only_undecided_proposals_are_listed(self):
        self.write_proposals([
            proposal("L1", PROPOSAL_A),
            proposal("L2", PROPOSAL_B),
            proposal("L3", PROPOSAL_C, status="approved"),
        ])
        self.write_decided([{"id": "L2", "status": "approved",
                             "at": "2026-08-01"}])
        rows = self.by_source("lessons")
        self.assertEqual([r["title"] for r in rows], [PROPOSAL_A])

    def test_a_missing_proposals_file_is_an_empty_store(self):
        self.assertEqual(self.by_source("lessons"), [])

    def test_a_malformed_line_never_costs_the_rest(self):
        (self.state / "lessons-proposed.jsonl").write_text(
            json.dumps(proposal("L1", PROPOSAL_A)) + "\n{ not json\n"
            + json.dumps(proposal("L2", PROPOSAL_B)) + "\n", encoding="utf-8")
        self.assertEqual(len(self.by_source("lessons")), 2)

    def test_rows_sharing_an_id_are_all_shown_and_flagged(self):
        """The defect this queue had to be built around: the generator
        repeats ids, so keying on the id would hide most of the backlog."""
        self.write_proposals([
            proposal("L1", PROPOSAL_A),
            proposal("L1", PROPOSAL_B),
            proposal("L2", PROPOSAL_C),
        ])
        rows = self.by_source("lessons")
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r["id"] for r in rows}), 3)
        flagged = [r for r in rows if r["note"]]
        self.assertEqual({r["title"] for r in flagged},
                         {PROPOSAL_A, PROPOSAL_B})
        self.assertIn("dup id", [r["ref"] for r in flagged][0])
        self.assertEqual([r["note"] for r in rows if r["title"] == PROPOSAL_C],
                         [""])

    def test_a_unique_id_is_approved_by_id(self):
        self.write_proposals([proposal("L1", PROPOSAL_A)])
        item = self.by_source("lessons")[0]
        with mock.patch.object(reviewqueue.subprocess, "run",
                               return_value=self.run_ok()) as run:
            out = reviewqueue.act(item["id"], "approve")
        self.assertEqual(out["mode"], "id")
        self.assertEqual(run.call_args[0][0],
                         [sys.executable, str(self.script), "approve", "L1"])
        self.assertFalse(run.call_args[1].get("shell", False))

    def test_a_unique_id_is_dropped_by_id(self):
        self.write_proposals([proposal("L1", PROPOSAL_A)])
        item = self.by_source("lessons")[0]
        with mock.patch.object(reviewqueue.subprocess, "run",
                               return_value=self.run_ok()) as run:
            reviewqueue.act(item["id"], "drop")
        self.assertEqual(run.call_args[0][0][2:], ["drop", "L1"])

    def test_a_duplicated_id_is_approved_by_its_exact_text(self):
        self.write_proposals([proposal("L1", PROPOSAL_A, tier=3),
                              proposal("L1", PROPOSAL_B)])
        item = [r for r in self.by_source("lessons")
                if r["title"] == PROPOSAL_A][0]
        with mock.patch.object(reviewqueue.subprocess, "run",
                               return_value=self.run_ok()) as run:
            out = reviewqueue.act(item["id"], "approve")
        self.assertEqual(out["mode"], "text")
        self.assertEqual(run.call_args[0][0][2:],
                         ["add", PROPOSAL_A, "--tier", "3"])
        # and it leaves the queue without the jsonl being rewritten
        left = [r["title"] for r in self.by_source("lessons")]
        self.assertEqual(left, [PROPOSAL_B])
        self.assertIn(PROPOSAL_A,
                      (self.state / "lessons-proposed.jsonl")
                      .read_text(encoding="utf-8"))

    def test_a_duplicated_id_is_never_dropped_upstream(self):
        """drop <id> would bury every sibling row, so the decision is banked
        locally instead and the siblings stay pending."""
        self.write_proposals([proposal("L1", PROPOSAL_A),
                              proposal("L1", PROPOSAL_B)])
        item = [r for r in self.by_source("lessons")
                if r["title"] == PROPOSAL_A][0]
        with mock.patch.object(reviewqueue.subprocess, "run") as run:
            out = reviewqueue.act(item["id"], "drop")
        run.assert_not_called()
        self.assertEqual(out["mode"], "local")
        self.assertEqual([r["title"] for r in self.by_source("lessons")],
                         [PROPOSAL_B])

    def test_an_unsafe_id_never_reaches_a_subprocess(self):
        self.write_proposals([proposal("L1; rm -rf ~", PROPOSAL_D)])
        item = self.by_source("lessons")[0]
        with mock.patch.object(reviewqueue.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                reviewqueue.act(item["id"], "approve")
        run.assert_not_called()

    def test_an_unknown_action_never_reaches_a_subprocess(self):
        self.write_proposals([proposal("L1", PROPOSAL_A)])
        item = self.by_source("lessons")[0]
        with mock.patch.object(reviewqueue.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                reviewqueue.act(item["id"], "delete")
        run.assert_not_called()

    def test_a_stale_item_id_raises_rather_than_guessing(self):
        self.write_proposals([proposal("L1", PROPOSAL_A)])
        with self.assertRaises(KeyError):
            reviewqueue.act("lessons:deadbeefdead", "approve")

    def test_a_failing_cli_surfaces_its_error(self):
        self.write_proposals([proposal("L1", PROPOSAL_A)])
        item = self.by_source("lessons")[0]
        failed = mock.Mock(returncode=2, stdout="", stderr="not pending")
        with mock.patch.object(reviewqueue.subprocess, "run",
                               return_value=failed):
            with self.assertRaises(RuntimeError) as cm:
                reviewqueue.act(item["id"], "approve")
        self.assertIn("not pending", str(cm.exception))

    def test_a_missing_cli_is_a_named_failure(self):
        self.script.unlink()
        self.write_proposals([proposal("L1", PROPOSAL_A)])
        item = self.by_source("lessons")[0]
        with self.assertRaises(FileNotFoundError):
            reviewqueue.act(item["id"], "approve")


class InboxSourceTests(_Case):

    def test_the_documented_steady_state_shows_nothing(self):
        self.assertEqual(self.by_source("inbox"), [])

    def test_a_missing_inbox_is_not_an_error(self):
        import shutil
        shutil.rmtree(self.record / "inbox")
        self.assertEqual(self.by_source("inbox"), [])
        self.assertEqual(reviewqueue.items()["errors"], {})

    def test_notes_surface_oldest_first_and_read_only(self):
        d = self.record / "inbox" / "notes"
        (d / "2026-08-09-a-thing.md").write_text(
            "# Heading\n\nThe first real line.\n", encoding="utf-8")
        (d / "2026-08-02-older.md").write_text("Older note.\n",
                                               encoding="utf-8")
        (d / "README.md").write_text("how this folder works\n",
                                     encoding="utf-8")
        (d / ".DS_Store").write_text("x", encoding="utf-8")
        rows = self.by_source("inbox")
        self.assertEqual([r["date"] for r in rows],
                         ["2026-08-02", "2026-08-09"])
        self.assertEqual(rows[1]["why"], "The first real line.")
        self.assertEqual(rows[0]["actions"], [])


class FlagSourceTests(_Case):

    def write_canon(self):
        (self.record / "canon" / "FACTS.md").write_text(FACTS_DOC,
                                                        encoding="utf-8")
        (self.record / "canon" / "MASTER_HISTORY.md").write_text(
            HISTORY_DOC, encoding="utf-8")

    def test_missing_canon_files_are_an_empty_store(self):
        self.assertEqual(self.by_source("flags"), [])

    def test_open_flags_from_both_documents_are_surfaced(self):
        self.write_canon()
        rows = self.by_source("flags")
        self.assertEqual([r["title"] for r in rows],
                         ["Comp band ambiguity", "Second live flag",
                          "Disposition reconciliation"])
        self.assertTrue(all(r["actions"] == [] for r in rows))

    def test_a_resolved_flag_is_not_waiting_on_anyone(self):
        self.write_canon()
        self.assertNotIn("Old question",
                         [r["title"] for r in self.by_source("flags")])

    def test_a_wrapped_bullet_is_one_item(self):
        self.write_canon()
        row = self.by_source("flags")[0]
        self.assertIn("Resolve at first contact", row["why"])

    def test_the_section_ends_at_the_next_heading(self):
        self.write_canon()
        titles = " ".join(r["title"] for r in self.by_source("flags"))
        self.assertNotIn("Nothing here", titles)
        self.assertNotIn("Not a flag", titles)

    def test_ids_are_stable_across_reads(self):
        self.write_canon()
        self.assertEqual([r["id"] for r in self.by_source("flags")],
                         [r["id"] for r in self.by_source("flags")])


class IdeaSourceTests(_Case):

    def test_only_proposed_ideas_are_queued(self):
        ideas.add("An approved thing", status="open")
        prop = ideas.add("A staged proposal", status="proposed")
        rows = self.by_source("ideas")
        self.assertEqual([r["title"] for r in rows], ["A staged proposal"])
        self.assertEqual(rows[0]["id"], "ideas:" + prop["id"])

    def test_approving_opens_it_without_dispatching_a_build(self):
        prop = ideas.add("A staged proposal", status="proposed")
        reviewqueue.act("ideas:" + prop["id"], "approve")
        got = [i for i in ideas.list_items() if i["id"] == prop["id"]][0]
        self.assertEqual(got["status"], "open")
        self.assertEqual(self.by_source("ideas"), [])

    def test_dropping_records_who_decided_it(self):
        prop = ideas.add("A staged proposal", status="proposed")
        reviewqueue.act("ideas:" + prop["id"], "drop")
        got = [i for i in ideas.list_items() if i["id"] == prop["id"]][0]
        self.assertEqual(got["status"], "dropped")
        self.assertIn("declined by the owner", got["note"])


class BriefSectionTests(_Case):

    def test_the_brief_section_is_quiet_when_nothing_waits(self):
        from server import brief
        self.assertIsNone(brief._review_section())

    def test_the_brief_never_breaks_on_this_section(self):
        from server import brief
        with mock.patch.object(reviewqueue, "summary",
                               side_effect=RuntimeError("boom")):
            self.assertIsNone(brief._review_section())

    def test_the_brief_section_carries_the_head_of_the_queue(self):
        from server import brief
        self.write_proposals([proposal("L1", PROPOSAL_A)])
        section = brief._review_section()
        self.assertEqual(section["total"], 1)
        self.assertEqual(section["top"][0]["title"], PROPOSAL_A)
        self.assertEqual([s["key"] for s in section["sources"]], ["lessons"])


if __name__ == "__main__":
    unittest.main()
