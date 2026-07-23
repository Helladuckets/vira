"""The contact card: the owner-editable top pane of every CRM person.

Covers the overlay merge and its provenance, the two independent handle
dimensions (rank — including the single-primary rule and what `archived` does
and does not change — and use), the two write-through paths into the CRM
registry, the passive-instance refusal, the change list, and the journal
hand-off that makes saving a card the same act as telling Vira.

Everything runs against a synthetic CRM in a temp dir — no real people.json
is read or written, and every fixture name/handle is invented (the PII guard
allowlists the NANP 555-01xx fiction block).

Run: .venv/bin/python -m unittest tests.test_contactcard
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import contactcard, data as crm, journal, triage

PID = "p_aaaaaaaaaaaa"
OTHER = "p_bbbbbbbbbbbb"


def _people():
    return {"people": [
        {"id": PID, "name": "Dana Vega", "class_hint": "unknown",
         "refs": {}, "handles": {
             "imessage": ["dana@example.com", "2025550188"],
             "phones10": ["2025550188"],
             "emails": ["dana@example.com", "d.vega@work.example.com"]},
         "master_tier": "active", "profile_tier": "active",
         "activity": {"imsg_n": 40, "imsg_last": "2026-07-20"}},
        {"id": OTHER, "name": "Reed Okafor", "class_hint": "unknown",
         "refs": {}, "handles": {"imessage": ["reed@example.com"],
                                 "phones10": [], "emails": ["reed@example.com"]},
         "master_tier": "active", "profile_tier": "active", "activity": {}},
    ]}


def _master():
    return [{"id": PID, "full_name": "Dana Vega", "company": "Northwind",
             "title": "Head of Ops", "relationship": "Former colleague",
             "evidence": "", "tier": "active",
             "emails": ["dana@example.com"], "phones": []}]


class CardCase(unittest.TestCase):
    """A synthetic CRM root plus an isolated overlay store."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "crm"
        (root / "profiles").mkdir(parents=True)
        (root / "people.json").write_text(json.dumps(_people()))
        (root / "master.json").write_text(json.dumps(_master()))
        self.root = root

        self.store = Path(self.tmp.name) / "contact-cards.json"
        for patch in (mock.patch.object(contactcard, "STORE", self.store),
                      mock.patch.object(crm, "_crm", lambda: root),
                      mock.patch.dict("os.environ", {}, clear=False)):
            patch.start()
            self.addCleanup(patch.stop)
        # journal integration is an AI pass; the card's contract is that a
        # note is FILED, not what the model then does with it
        self.notes = []
        p = mock.patch.object(journal, "add", side_effect=self._note)
        p.start()
        self.addCleanup(p.stop)
        crm.invalidate()
        self.addCleanup(crm.invalidate)

    def _note(self, text, person_id=None, **kw):
        entry = {"id": f"note_{len(self.notes)}", "text": text,
                 "person_id": person_id, **kw}
        self.notes.append(entry)
        return entry

    def person(self):
        return json.loads((self.root / "people.json").read_text())["people"]

    def save(self, **draft):
        draft.setdefault("fields", {})
        return contactcard.save(PID, draft)


class ComposeTests(CardCase):
    def test_derives_from_the_crm_and_says_so(self):
        card = contactcard.compose(PID)
        by = {f["key"]: f for f in card["fields"]}
        self.assertEqual(by["display_name"]["value"], "Dana Vega")
        self.assertEqual(by["company"]["value"], "Northwind")
        self.assertEqual(by["company"]["source"], "crm")
        self.assertEqual(by["pronouns"]["value"], "")
        self.assertEqual(by["pronouns"]["source"], "empty")

    def test_owner_value_wins_and_is_marked_owner(self):
        self.save(fields={"company": "Aurora Labs", "pronouns": "she/her"})
        by = {f["key"]: f for f in contactcard.compose(PID)["fields"]}
        self.assertEqual(by["company"]["value"], "Aurora Labs")
        self.assertEqual(by["company"]["source"], "owner")
        # the derived value is still carried, so the UI can offer it back
        self.assertEqual(by["company"]["derived"], "Northwind")
        self.assertEqual(by["pronouns"]["source"], "owner")

    def test_clearing_an_override_falls_back_to_the_crm(self):
        self.save(fields={"company": "Aurora Labs"})
        self.save(fields={"company": ""})
        by = {f["key"]: f for f in contactcard.compose(PID)["fields"]}
        self.assertEqual(by["company"]["value"], "Northwind")
        self.assertEqual(by["company"]["source"], "crm")

    def test_a_crm_resynthesis_cannot_touch_owner_edits(self):
        self.save(fields={"title": "Chief of Staff"})
        # the CRM regenerates master.json from evidence
        (self.root / "master.json").write_text(json.dumps(
            [{**_master()[0], "title": "Head of Operations",
              "company": "Northwind Group"}]))
        crm.invalidate()
        by = {f["key"]: f for f in contactcard.compose(PID)["fields"]}
        self.assertEqual(by["title"]["value"], "Chief of Staff")   # owner's
        self.assertEqual(by["company"]["value"], "Northwind Group")  # derived

    def test_unknown_person(self):
        self.assertIsNone(contactcard.compose("p_nope"))

    def test_custom_fields_round_trip_and_cap(self):
        self.save(custom=[{"label": "Kids", "value": "two"},
                          {"label": "", "value": "dropped"}])
        card = contactcard.compose(PID)
        self.assertEqual([(c["label"], c["value"]) for c in card["custom"]],
                         [("Kids", "two")])
        self.assertTrue(card["custom"][0]["id"].startswith("f_"))


class HandleTests(CardCase):
    def test_lists_registry_handles_with_formatted_phones(self):
        rows = contactcard.compose(PID)["handles"]
        self.assertEqual([r["kind"] for r in rows], ["email", "email", "phone"])
        phone = next(r for r in rows if r["kind"] == "phone")
        self.assertEqual(phone["display"], "(202) 555-0188")
        self.assertEqual(phone["value"], "2025550188")

    def test_rank_reorders_and_primary_is_unique_per_kind(self):
        k1 = contactcard.handle_key("email", "dana@example.com")
        k2 = contactcard.handle_key("email", "d.vega@work.example.com")
        self.save(handles={k1: {"rank": "primary"}})
        self.save(handles={k2: {"rank": "primary"}})
        rows = {r["key"]: r for r in contactcard.compose(PID)["handles"]}
        self.assertEqual(rows[k2]["rank"], "primary")
        self.assertEqual(rows[k1]["rank"], "secondary")  # demoted, not dropped
        self.assertEqual(contactcard.compose(PID)["handles"][0]["key"], k2)

    def test_primary_handle_picks_the_marked_one(self):
        k2 = contactcard.handle_key("email", "d.vega@work.example.com")
        self.assertEqual(contactcard.primary_handle(PID, "email"),
                         "dana@example.com")           # registry order
        self.save(handles={k2: {"rank": "primary"}})
        self.assertEqual(contactcard.primary_handle(PID, "email"),
                         "d.vega@work.example.com")    # the owner's choice

    def test_archived_is_skipped_for_outbound_but_kept_and_still_resolves(self):
        k1 = contactcard.handle_key("email", "dana@example.com")
        self.save(handles={k1: {"rank": "archived"}})
        self.assertEqual(contactcard.primary_handle(PID, "email"),
                         "d.vega@work.example.com")
        keys = [r["key"] for r in contactcard.compose(PID)["handles"]]
        self.assertIn(k1, keys)                        # kept on the card
        self.assertEqual(keys[-1], k1)                 # sorted last
        crm.invalidate()
        self.assertEqual(crm.resolve_handle("dana@example.com"), PID)

    def test_primary_handle_returns_none_when_everything_is_archived(self):
        self.save(handles={
            contactcard.handle_key("email", "dana@example.com"): {"rank": "archived"},
            contactcard.handle_key("email", "d.vega@work.example.com"):
                {"rank": "archived"}})
        self.assertIsNone(contactcard.primary_handle(PID, "email"))

    def test_a_store_written_before_the_rename_still_means_archived(self):
        """`former` was this rank's first name. A store carrying it keeps
        meaning what it meant rather than silently reading as unranked —
        which would put a retired address back in the outbound rotation."""
        k1 = contactcard.handle_key("email", "dana@example.com")
        self.store.write_text(json.dumps(
            {"cards": {PID: {"handles": {k1: {"rank": "former"}}}}}))
        row = next(r for r in contactcard.compose(PID)["handles"]
                   if r["key"] == k1)
        self.assertEqual(row["rank"], "archived")
        self.assertEqual(contactcard.archived_handles(PID), {"dana@example.com"})
        self.assertEqual(contactcard.primary_handle(PID, "email"),
                         "d.vega@work.example.com")

    def test_archived_handles_lists_only_the_archived_ones(self):
        self.save(handles={
            contactcard.handle_key("email", "dana@example.com"): {"rank": "archived"},
            contactcard.handle_key("phone", "2025550188"): {"rank": "primary"}})
        self.assertEqual(contactcard.archived_handles(PID), {"dana@example.com"})

    def test_send_prefers_the_pinned_phone_and_skips_an_archived_one(self):
        """The rank has to govern outbound or it is decoration: the
        recent-thread signal would otherwise keep texting a number the owner
        had just archived."""
        from server import send
        with mock.patch.object(send.imessage, "thread_for_person",
                               return_value=[{"handle": "+12025550188"}]):
            self.assertEqual(send.best_handle(PID), "+12025550188")
            self.save(handles={contactcard.handle_key("phone", "2025550188"):
                               {"rank": "archived"}})
            # the archived number is skipped; the email handle is what is left
            self.assertEqual(send.best_handle(PID), "dana@example.com")
            self.save(added=[{"kind": "phone", "value": "2025550199"}],
                      handles={contactcard.handle_key("phone", "2025550199"):
                               {"rank": "primary"}})
            self.assertEqual(send.best_handle(PID), "+12025550199")

    def test_use_is_a_second_dimension_independent_of_rank(self):
        """personal/work and primary/secondary/archived are orthogonal: a work
        address can be the primary one, and marking a handle personal must not
        disturb which one Vira writes to."""
        work = contactcard.handle_key("email", "d.vega@work.example.com")
        home = contactcard.handle_key("email", "dana@example.com")
        self.save(handles={work: {"rank": "primary", "use": "work"},
                           home: {"use": "personal"}})
        rows = {r["key"]: r for r in contactcard.compose(PID)["handles"]}
        self.assertEqual((rows[work]["rank"], rows[work]["use"]),
                         ("primary", "work"))
        self.assertEqual((rows[home]["rank"], rows[home]["use"]),
                         ("", "personal"))
        self.assertEqual(contactcard.primary_handle(PID, "email"),
                         "d.vega@work.example.com")
        # clearing the use leaves the rank alone
        self.save(handles={work: {"use": ""}})
        rows = {r["key"]: r for r in contactcard.compose(PID)["handles"]}
        self.assertEqual((rows[work]["rank"], rows[work]["use"]), ("primary", ""))

    def test_sorting_ignores_use_so_tagging_does_not_reshuffle(self):
        before = [r["key"] for r in contactcard.compose(PID)["handles"]]
        self.save(handles={contactcard.handle_key("email", "d.vega@work.example.com"):
                           {"use": "work"}})
        self.assertEqual([r["key"] for r in contactcard.compose(PID)["handles"]],
                         before)

    def test_a_junk_use_is_dropped_not_stored(self):
        k = contactcard.handle_key("phone", "2025550188")
        self.save(handles={k: {"use": "whatever"}})
        row = next(r for r in contactcard.compose(PID)["handles"]
                   if r["key"] == k)
        self.assertEqual(row["use"], "")


class WriteThroughTests(CardCase):
    def test_rename_writes_the_registry_not_the_overlay(self):
        res = self.save(fields={"display_name": "Dana Vega-Ruiz"})
        self.assertEqual(self.person()[0]["name"], "Dana Vega-Ruiz")
        self.assertNotIn("display_name",
                         contactcard.raw(PID)["fields"])
        self.assertEqual(res["card"]["fields"][0]["value"], "Dana Vega-Ruiz")
        # people.json is backed up before every registry write
        self.assertTrue(list((self.root / "backups").glob("people-*.json")))

    def test_added_handle_lands_in_the_registry_and_resolves(self):
        res = self.save(added=[{"kind": "email", "value": "Dana@New.Example.com"}])
        self.assertEqual(res["warnings"], [])
        self.assertIn("dana@new.example.com", self.person()[0]["handles"]["emails"])
        self.assertIn("dana@new.example.com",
                      self.person()[0]["handles"]["imessage"])
        crm.invalidate()
        self.assertEqual(crm.resolve_handle("dana@new.example.com"), PID)

    def test_added_phone_normalizes_to_ten_digits(self):
        self.save(added=[{"kind": "phone", "value": "+1 (202) 555-0199"}])
        self.assertIn("2025550199", self.person()[0]["handles"]["phones10"])

    def test_a_handle_owned_by_someone_else_is_refused_not_moved(self):
        res = self.save(added=[{"kind": "email", "value": "reed@example.com"}])
        self.assertTrue(any("already belongs" in w for w in res["warnings"]))
        self.assertNotIn("reed@example.com",
                         self.person()[0]["handles"]["emails"])
        self.assertEqual(crm.resolve_handle("reed@example.com"), OTHER)

    def test_malformed_handles_are_rejected_with_a_reason(self):
        res = self.save(added=[{"kind": "email", "value": "not-an-email"},
                               {"kind": "phone", "value": "123"}])
        self.assertEqual(len(res["warnings"]), 2)
        self.assertEqual(len(contactcard.compose(PID)["handles"]), 3)

    def test_re_adding_a_known_handle_is_a_no_op(self):
        self.save(added=[{"kind": "email", "value": "dana@example.com"}])
        self.assertEqual(self.person()[0]["handles"]["emails"].count(
            "dana@example.com"), 1)

    def test_passive_instance_never_writes_the_real_registry(self):
        """A test clone edits its own overlay and nothing else. The three
        things that reach outside the cloned data/ — the rename, the new
        handle, and the journal note whose integration pass writes CRM
        profiles — are each refused, and each says so."""
        with mock.patch.dict("os.environ", {"VIRA_PASSIVE": "1"}):
            res = self.save(fields={"display_name": "Renamed On A Clone",
                                    "location": "Portland"},
                            added=[{"kind": "email",
                                    "value": "clone@example.com"}])
        self.assertEqual(self.person()[0]["name"], "Dana Vega")
        self.assertNotIn("clone@example.com",
                         self.person()[0]["handles"]["emails"])
        self.assertEqual(self.notes, [])
        self.assertEqual(len([w for w in res["warnings"]
                              if "test instance" in w]), 3)
        # the overlay half still applies, so the surface stays testable
        by = {f["key"]: f for f in res["card"]["fields"]}
        self.assertEqual(by["location"]["value"], "Portland")

    def test_added_handle_resolves_even_if_the_registry_write_failed(self):
        with mock.patch.object(triage, "add_handles",
                               side_effect=OSError("read-only")):
            res = self.save(added=[{"kind": "email",
                                    "value": "kept@example.com"}])
        self.assertTrue(any("could not add" in w for w in res["warnings"]))
        crm.invalidate()
        self.assertEqual(crm.resolve_handle("kept@example.com"), PID)


class SaveTests(CardCase):
    def test_change_list_reads_as_the_owner_would_say_it(self):
        k = contactcard.handle_key("email", "dana@example.com")
        k2 = contactcard.handle_key("email", "d.vega@work.example.com")
        res = self.save(fields={"title": "Chief of Staff", "pronouns": "she/her"},
                        handles={k: {"rank": "archived"}, k2: {"use": "work"}})
        joined = " | ".join(res["changes"])
        self.assertIn('Title changed from "Head of Ops" to "Chief of Staff"',
                      joined)
        self.assertIn('Pronouns set to "she/her"', joined)
        self.assertIn("archived — out of date, kept on file", joined)
        self.assertIn("is a work email", joined)

    def test_saving_a_card_files_a_journal_note_scoped_to_the_person(self):
        res = self.save(fields={"company": "Aurora Labs"},
                        note="She started there last month.")
        self.assertEqual(len(self.notes), 1)
        note = self.notes[0]
        self.assertEqual(note["person_id"], PID)
        self.assertEqual(res["note_id"], note["id"])
        self.assertIn("Aurora Labs", note["text"])
        self.assertIn("She started there last month.", note["text"])
        # the context tells the integration pass the edit is already applied,
        # so it files what the change MEANS instead of asking for it again
        self.assertIn("already applied", note["context"])

    def test_a_no_op_save_files_nothing(self):
        res = self.save(fields={"company": "Northwind"})
        self.assertEqual(res["changes"], [])
        self.assertIsNone(res["note_id"])
        self.assertEqual(self.notes, [])

    def test_the_card_still_saves_when_the_journal_refuses(self):
        with mock.patch.object(journal, "add", side_effect=ValueError("nope")):
            res = self.save(fields={"location": "Portland"})
        self.assertTrue(any("journal" in w for w in res["warnings"]))
        by = {f["key"]: f for f in contactcard.compose(PID)["fields"]}
        self.assertEqual(by["location"]["value"], "Portland")

    def test_values_are_trimmed_and_bounded(self):
        self.save(fields={"location": "  Portland,   OR  ",
                          "note": "x" * 900})
        by = {f["key"]: f for f in contactcard.compose(PID)["fields"]}
        self.assertEqual(by["location"]["value"], "Portland, OR")
        self.assertEqual(len(by["note"]["value"]), contactcard.MAX_VALUE)

    def test_unknown_field_keys_are_ignored(self):
        self.save(fields={"salary": "lots"})
        self.assertNotIn("salary", contactcard.raw(PID)["fields"])

    def test_unknown_person_raises(self):
        with self.assertRaises(KeyError):
            contactcard.save("p_nope", {"fields": {}})

    def test_added_handles_map_lists_only_card_added_ones(self):
        self.save(handles={contactcard.handle_key("email", "dana@example.com"):
                           {"rank": "primary"}},
                  added=[{"kind": "email", "value": "new@example.com"}])
        self.assertEqual(contactcard.added_handles(),
                         {"new@example.com": PID})


class CorruptStoreTests(CardCase):
    def test_a_junk_overlay_degrades_to_the_derived_card(self):
        self.store.write_text("{ not json")
        card = contactcard.compose(PID)
        by = {f["key"]: f for f in card["fields"]}
        self.assertEqual(by["company"]["value"], "Northwind")
        self.assertEqual(contactcard.added_handles(), {})


if __name__ == "__main__":
    unittest.main()
