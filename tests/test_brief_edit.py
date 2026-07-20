"""Interactive-brief tests: targeted loop close/edit (the write path the
brief rows use), owner-told facts (stamped for refresh survival), the
dismissed-row store's re-arming keys, and the journal's plan application.
The AI planning step is mocked — _apply is deterministic and is what must
never mangle the CRM.

Run: .venv/bin/python -m unittest discover tests
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import briefstate, data as crm, journal


def _seed_crm(root):
    root = Path(root)
    (root / "profiles").mkdir(parents=True)
    people = {"people": [
        {"id": "p_test00000001", "name": "Casey Example",
         "handles": {"imessage": [], "emails": [], "phones10": []}},
        {"id": "p_test00000002", "name": "Drew Sample",
         "handles": {"imessage": [], "emails": [], "phones10": []}},
    ]}
    (root / "people.json").write_text(json.dumps(people))
    (root / "master.json").write_text("[]")
    prof = {"name": "Casey Example",
            "open_loops": [
                {"what": "Dinner was proposed but never scheduled",
                 "owed_by": "me", "since": "2024-01-01",
                 "channel": "imessage", "quote": "dinner soon",
                 "status": "open"},
                {"what": "Casey offered to lend the drill",
                 "owed_by": "them", "since": "2024-02-01",
                 "channel": "imessage", "quote": "drill",
                 "status": "open"},
            ],
            "personal_facts": [
                {"fact": "Casey lives in Queens", "as_of": "2024-01-01",
                 "source": "imessage"},
            ]}
    (root / "profiles" / "p_test00000001.json").write_text(json.dumps(prof))
    return root


class BriefEditBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _seed_crm(self.tmp.name)
        self.patcher = mock.patch("server.data.settings.crm_root",
                                  return_value=self.root)
        self.patcher.start()
        crm.invalidate()

    def tearDown(self):
        self.patcher.stop()
        crm.invalidate()
        self.tmp.cleanup()

    def _profile(self):
        return json.loads(
            (self.root / "profiles" / "p_test00000001.json").read_text())


class TestLoopActions(BriefEditBase):
    def test_close_marks_status_and_date(self):
        loop = crm.update_loop("p_test00000001",
                               "Dinner was proposed but never scheduled",
                               "close")
        self.assertEqual(loop["status"], "closed")
        self.assertIn("closed_on", loop)
        saved = self._profile()
        closed = [l for l in saved["open_loops"] if l["status"] == "closed"]
        self.assertEqual(len(closed), 1)
        # the untouched loop stays open
        self.assertEqual(saved["open_loops"][1]["status"], "open")
        self.assertIn("open_loops_updated_by_vira", saved)

    def test_close_matches_case_and_spacing_insensitively(self):
        loop = crm.update_loop("p_test00000001",
                               "  dinner WAS proposed but never scheduled ",
                               "close")
        self.assertEqual(loop["status"], "closed")

    def test_edit_rewrites_and_stamps(self):
        loop = crm.update_loop("p_test00000001",
                               "Casey offered to lend the drill",
                               "edit", "Casey lent the drill — return it")
        self.assertEqual(loop["what"], "Casey lent the drill — return it")
        self.assertIn("edited", loop)

    def test_close_missing_loop_raises_lookup(self):
        with self.assertRaises(LookupError):
            crm.update_loop("p_test00000001", "no such loop", "close")

    def test_closed_loop_not_closable_again(self):
        crm.update_loop("p_test00000001",
                        "Dinner was proposed but never scheduled", "close")
        with self.assertRaises(LookupError):
            crm.update_loop("p_test00000001",
                            "Dinner was proposed but never scheduled",
                            "close")

    def test_unknown_person_raises_key(self):
        with self.assertRaises(KeyError):
            crm.update_loop("p_nope", "x", "close")

    def test_add_loop_shape_survives_refresh_predicate(self):
        entry = crm.add_loop("p_test00000002", "Follow up on the intro",
                             "me")
        # hand-added shape: no quote/channel -> vira_touched_loop is true
        self.assertNotIn("quote", entry)
        self.assertNotIn("channel", entry)
        self.assertEqual(entry["status"], "open")
        saved = json.loads(
            (self.root / "profiles" / "p_test00000002.json").read_text())
        self.assertEqual(len(saved["open_loops"]), 1)


class TestFacts(BriefEditBase):
    def test_add_fact_stamped_vira(self):
        entry = crm.add_fact("p_test00000001", "Casey started a new job")
        self.assertEqual(entry["source"], "vira")
        saved = self._profile()
        self.assertEqual(len(saved["personal_facts"]), 2)
        self.assertIn("personal_facts_updated_by_vira", saved)

    def test_empty_fact_rejected(self):
        with self.assertRaises(ValueError):
            crm.add_fact("p_test00000001", "   ")


class TestBriefState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patcher = mock.patch.object(
            briefstate, "STORE", Path(self.tmp.name) / "brief-state.json")
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_dismiss_restore_roundtrip(self):
        briefstate.dismiss("quiet:p_x:2026-07-01")
        self.assertIn("quiet:p_x:2026-07-01", briefstate.dismissed_keys())
        briefstate.restore("quiet:p_x:2026-07-01")
        self.assertNotIn("quiet:p_x:2026-07-01", briefstate.dismissed_keys())

    def test_prune_keeps_newest(self):
        for i in range(briefstate.MAX_KEYS + 20):
            briefstate.dismiss(f"k:{i}")
        keys = briefstate.dismissed_keys()
        self.assertLessEqual(len(keys), briefstate.MAX_KEYS)
        self.assertIn(f"k:{briefstate.MAX_KEYS + 19}", keys)

    def test_empty_key_rejected(self):
        with self.assertRaises(ValueError):
            briefstate.dismiss("")


class JournalBase(BriefEditBase):
    def setUp(self):
        super().setUp()
        self.jtmp = tempfile.TemporaryDirectory()
        self.jpatch = mock.patch.object(
            journal, "STORE", Path(self.jtmp.name) / "brief-journal.json")
        self.jpatch.start()

    def tearDown(self):
        self.jpatch.stop()
        self.jtmp.cleanup()
        super().tearDown()


class TestJournal(JournalBase):

    def test_add_saves_verbatim_before_integration(self):
        entry = journal.add("Casey and I finally had that dinner",
                            person_id="p_test00000001", integrate=False)
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["person_name"], "Casey Example")
        self.assertEqual(journal.recent()[0]["id"], entry["id"])

    def test_add_rejects_empty_and_unknown_person(self):
        with self.assertRaises(ValueError):
            journal.add("   ")
        with self.assertRaises(KeyError):
            journal.add("hello", person_id="p_nope")

    def test_apply_closes_loop_adds_fact_and_new_loop(self):
        plan = {
            "loop_actions": [
                {"person_id": "p_test00000001",
                 "match_what": "Dinner was proposed but never scheduled",
                 "action": "close"}],
            "new_loops": [
                {"person_id": "p_test00000002",
                 "what": "Send Drew the deck", "owed_by": "me"}],
            "facts": [
                {"person_id": "p_test00000001",
                 "fact": "Casey got a promotion"}],
            "summary": "did things",
        }
        actions = journal._apply(plan)
        self.assertEqual(len(actions), 3)
        self.assertTrue(any(a.startswith("Closed loop") for a in actions))
        self.assertTrue(any(a.startswith("New loop") for a in actions))
        self.assertTrue(any(a.startswith("Fact saved") for a in actions))
        prof = self._profile()
        self.assertEqual(prof["open_loops"][0]["status"], "closed")
        self.assertEqual(prof["personal_facts"][-1]["source"], "vira")

    def test_apply_reports_misses_never_raises(self):
        plan = {"loop_actions": [
            {"person_id": "p_test00000001", "match_what": "ghost loop",
             "action": "close"}],
            "facts": [{"person_id": "p_nope", "fact": "x"}]}
        actions = journal._apply(plan)
        self.assertEqual(len(actions), 2)
        self.assertTrue(all("Skipped" in a for a in actions))

    def test_integrate_end_to_end_with_mocked_model(self):
        entry = journal.add("dinner happened", person_id="p_test00000001",
                            integrate=False)
        plan = {"loop_actions": [
            {"person_id": "p_test00000001",
             "match_what": "Dinner was proposed but never scheduled",
             "action": "close"}],
            "new_loops": [], "facts": [],
            "summary": "closed the dinner loop"}
        with mock.patch("server.suggest.complete",
                        return_value=json.dumps(plan)):
            journal._integrate(entry["id"])
        e = journal.recent()[0]
        self.assertEqual(e["status"], "integrated")
        self.assertEqual(e["result"]["summary"], "closed the dinner loop")
        self.assertEqual(len(e["result"]["actions"]), 1)

    def test_integrate_failure_keeps_note(self):
        entry = journal.add("some note", integrate=False)
        with mock.patch("server.suggest.complete",
                        side_effect=RuntimeError("model down")):
            journal._integrate(entry["id"])
        e = journal.recent()[0]
        self.assertEqual(e["status"], "failed")
        self.assertEqual(e["text"], "some note")
        self.assertIn("note kept in journal", e["result"]["summary"])

    def test_add_stores_click_context(self):
        entry = journal.add("this is not an overlap", integrate=False,
                            context="Daily Brief · \"4:00 PM Odile OVERLAP\"")
        self.assertIn("Odile", entry["context"])
        # and the integration prompt carries it
        with mock.patch("server.suggest.complete",
                        return_value='{"summary": "s"}') as m:
            journal._integrate(entry["id"])
        self.assertIn("Odile", m.call_args[0][0])

    def test_clean_unapplied_validates_and_caps(self):
        plan = {"unapplied": [
            {"instruction": "Merge contact A into contact B", "area": "contacts"},
            {"instruction": "   "},          # empty -> dropped
            "not a dict",                    # wrong shape -> dropped
            {"instruction": "x" * 700},      # capped, area defaults
        ]}
        out = journal._clean_unapplied(plan)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["area"], "contacts")
        self.assertEqual(out[1]["area"], "other")
        self.assertEqual(len(out[1]["instruction"]), 600)

    def test_integrate_keeps_unapplied_on_result(self):
        entry = journal.add("merge that unidentified contact with Casey",
                            integrate=False)
        plan = {"loop_actions": [], "new_loops": [], "facts": [],
                "unapplied": [{"instruction":
                               "Merge placeholder p_x into p_test00000001",
                               "area": "contacts"}],
                "summary": "needs a session"}
        with mock.patch("server.suggest.complete",
                        return_value=json.dumps(plan)):
            journal._integrate(entry["id"])
        e = journal.recent()[0]
        self.assertEqual(e["status"], "noted")
        self.assertEqual(len(e["result"]["unapplied"]), 1)

    def test_export_prompt_covers_unapplied_notes(self):
        self.assertEqual(journal.export_prompt()["count"], 0)
        entry = journal.add("this isn't an overlap because Momo's visit is all day",
                            integrate=False,
                            context="Daily Brief · \"5:00 PM Momo & Bumbo\"")
        journal._update_entry(entry["id"], status="noted", result={
            "summary": "s", "actions": [],
            "unapplied": [{"instruction": "Mark the 5pm event non-overlapping",
                           "area": "calendar"}]})
        journal.add("plain note, fully integrated", integrate=False)
        ex = journal.export_prompt()
        self.assertEqual(ex["count"], 1)
        self.assertIn("Mark the 5pm event non-overlapping", ex["prompt"])
        self.assertIn("Momo", ex["prompt"])          # note text + context ride along
        self.assertIn("written from", ex["prompt"])


class TestJournalPidVerification(JournalBase):
    """The 2026-07-16 incident class: a note naming an entity (an automated
    U.S. Bank message) was mapped onto an unrelated person's pid. Every
    model-guessed pid must now be backed by ground truth — the person's
    CRM record, enrichment verdict, or recent chat.db messages — or be
    corrected / held / flagged instead of trusted."""

    NOTE = ("This is an automated message from U.S. Bank. Flag the sender "
            "as a company that needs a CRM profile.")

    def setUp(self):
        super().setUp()
        # never let a test read the machine's real chat.db
        self.msgs = mock.patch.object(journal, "_recent_texts",
                                      return_value=[])
        self.msgs.start()

    def tearDown(self):
        self.msgs.stop()
        super().tearDown()

    def _add_bank_person(self):
        doc = json.loads((self.root / "people.json").read_text())
        doc["people"].append(
            {"id": "p_ab12cd34ef56", "name": "U.S. Bank",
             "class_hint": "company",
             "handles": {"imessage": [], "emails": [],
                         "phones10": ["8336721483"]}})
        (self.root / "people.json").write_text(json.dumps(doc))
        crm.invalidate()

    def _give_handle(self, pid, handle):
        doc = json.loads((self.root / "people.json").read_text())
        person = next(p for p in doc["people"] if p["id"] == pid)
        person["handles"]["imessage"] = [handle]
        (self.root / "people.json").write_text(json.dumps(doc))
        crm.invalidate()

    def test_entity_extraction(self):
        ents = journal._entities(self.NOTE)
        self.assertEqual([journal._norm(e) for e, _ in ents], ["us bank"])
        # sentence-case openers and plain notes yield nothing to verify
        self.assertEqual(journal._entities("dinner happened"), [])
        self.assertEqual(journal._entities("Dinner was great."), [])
        # a forced-caps verb keeps a variant without itself
        self.assertEqual(journal._entities("Met Casey for coffee"),
                         [("Met Casey", "Casey")])

    def test_unverifiable_entity_holds_writes(self):
        entry = journal.add(self.NOTE, integrate=False)
        plan = {"facts": [{"person_id": "p_test00000001",
                           "fact": "Sender is an automated U.S. Bank number"}],
                "new_loops": [{"person_id": "p_test00000002",
                               "what": "Set up the U.S. Bank profile",
                               "owed_by": "me"}]}
        actions = journal._apply(plan, entry)
        self.assertEqual(len(actions), 2)
        self.assertTrue(all(a.startswith("Held") for a in actions))
        self.assertEqual(len(self._profile()["personal_facts"]), 1)
        self.assertFalse(
            (self.root / "profiles" / "p_test00000002.json").exists())

    def test_fact_corrected_to_exact_name_match(self):
        self._add_bank_person()
        entry = journal.add(self.NOTE, integrate=False)
        plan = {"facts": [{"person_id": "p_test00000001",
                           "fact": "Automated loan-notification sender"}]}
        actions = journal._apply(plan, entry)
        self.assertIn("Fact saved to U.S. Bank", actions[0])
        self.assertIn("person corrected", actions[0])
        saved = json.loads(
            (self.root / "profiles" / "p_ab12cd34ef56.json").read_text())
        self.assertEqual(saved["personal_facts"][0]["source"], "vira")
        # the wrongly-guessed person's profile is untouched
        self.assertEqual(len(self._profile()["personal_facts"]), 1)

    def test_recent_messages_verify_mapping(self):
        entry = journal.add(self.NOTE, integrate=False)
        plan = {"facts": [{"person_id": "p_test00000001",
                           "fact": "Automated sender"}]}
        with mock.patch.object(
                journal, "_recent_texts",
                return_value=["U.S. Bank: your closing docs are ready"]):
            actions = journal._apply(plan, entry)
        self.assertIn("Fact saved to Casey Example", actions[0])
        self.assertNotIn("corrected", actions[0])

    def test_enrichment_verdict_verifies_mapping(self):
        (self.root / "imessage-enrichment.json").write_text(json.dumps(
            {"verdicts": [{"handle": "alerts@usbank.example.com",
                           "confirmed_name": None,
                           "relationship": "U.S. Bank loan-application "
                                           "notifications",
                           "evidence": "Automated."}]}))
        self._give_handle("p_test00000002", "alerts@usbank.example.com")
        entry = journal.add(self.NOTE, integrate=False)
        plan = {"facts": [{"person_id": "p_test00000002",
                           "fact": "Automated sender"}]}
        actions = journal._apply(plan, entry)
        self.assertIn("Fact saved to Drew Sample", actions[0])

    def test_owner_scoped_note_is_trusted(self):
        entry = journal.add(self.NOTE, person_id="p_test00000001",
                            integrate=False)
        plan = {"facts": [{"person_id": "p_test00000001",
                           "fact": "Forwarded me a U.S. Bank notice"}]}
        actions = journal._apply(plan, entry)
        self.assertIn("Fact saved to Casey Example", actions[0])

    def test_vira_written_facts_are_not_evidence(self):
        # the incident's own bad write must not vouch for the next one:
        # a source:"vira" fact naming the entity does not verify the pid
        crm.add_fact("p_test00000001",
                     "This sender is an automated U.S. Bank message")
        entry = journal.add(self.NOTE, integrate=False)
        plan = {"facts": [{"person_id": "p_test00000001",
                           "fact": "Automated sender"}]}
        actions = journal._apply(plan, entry)
        self.assertIn("Held a fact", actions[0])

    def test_loop_action_held_when_unverified(self):
        entry = journal.add(self.NOTE, integrate=False)
        plan = {"loop_actions": [
            {"person_id": "p_test00000001",
             "match_what": "Dinner was proposed but never scheduled",
             "action": "close"}]}
        actions = journal._apply(plan, entry)
        self.assertIn("Held a loop action", actions[0])
        self.assertEqual(self._profile()["open_loops"][0]["status"], "open")

    def test_unapplied_pid_flagged_unverified(self):
        entry = journal.add(self.NOTE, integrate=False)
        plan = {"unapplied": [{"instruction":
                "For the triage entry at person_id p_test00000001 (an "
                "automated U.S. Bank message), create a company contact.",
                "area": "contacts"}]}
        out = journal._clean_unapplied(plan, entry)
        self.assertEqual(out[0]["pid_check"], "unverified")
        self.assertIn("UNVERIFIED", out[0]["instruction"])

    def test_unapplied_pid_corrected(self):
        self._add_bank_person()
        entry = journal.add(self.NOTE, integrate=False)
        plan = {"unapplied": [{"instruction":
                "Resolve the triage entry at person_id p_test00000001 as "
                "the business sender U.S. Bank.", "area": "contacts"}]}
        out = journal._clean_unapplied(plan, entry)
        self.assertEqual(out[0]["pid_check"], "corrected")
        self.assertIn("p_ab12cd34ef56", out[0]["instruction"])
        self.assertIn("person_id corrected", out[0]["instruction"])

    def test_unapplied_without_entities_untouched(self):
        entry = journal.add("merge those two contacts", integrate=False)
        plan = {"unapplied": [{"instruction":
                "Merge p_test00000001 into p_test00000002",
                "area": "contacts"}]}
        out = journal._clean_unapplied(plan, entry)
        self.assertEqual(out[0]["instruction"],
                         "Merge p_test00000001 into p_test00000002")
        self.assertEqual(out[0]["pid_check"], "ok")

    def test_export_rechecks_legacy_entries(self):
        entry = journal.add(self.NOTE, integrate=False)
        journal._update_entry(entry["id"], status="noted", result={
            "summary": "s", "actions": [],
            "unapplied": [{"instruction":
                           "Resolve triage entry p_test00000001 as U.S. Bank",
                           "area": "contacts"}]})  # legacy: no pid_check
        self.assertIn("UNVERIFIED", journal.export_prompt()["prompt"])
        # once the entity exists in the CRM, the export corrects instead
        self._add_bank_person()
        ex = journal.export_prompt()
        self.assertIn("p_ab12cd34ef56", ex["prompt"])
        self.assertNotIn("UNVERIFIED", ex["prompt"])

    def test_integrate_end_to_end_holds_and_flags(self):
        entry = journal.add(self.NOTE, integrate=False)
        plan = {"loop_actions": [], "new_loops": [],
                "facts": [{"person_id": "p_test00000001",
                           "fact": "U.S. Bank sender"}],
                "unapplied": [{"instruction":
                               "Fix triage entry p_test00000001",
                               "area": "contacts"}],
                "summary": "mapped the bank note"}
        with mock.patch("server.suggest.complete",
                        return_value=json.dumps(plan)):
            journal._integrate(entry["id"])
        e = journal.recent()[0]
        self.assertIn("Held a fact", e["result"]["actions"][0])
        self.assertEqual(e["result"]["unapplied"][0]["pid_check"],
                         "unverified")
        self.assertEqual(len(self._profile()["personal_facts"]), 1)
