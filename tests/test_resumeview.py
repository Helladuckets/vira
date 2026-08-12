"""Tests for the resume viewport.

Every fixture is synthetic — a made-up person at example.com in a made-up
package folder. The real packages carry the owner's address, phone and
employment history, and the PII guard blocks them from the tracked tree.

ISOLATION: this module reads FOUR things outside its own store — the packages
root, the self-record, the applications owner state, and the brief journal —
so the base case roots all of them at one tmp fixture rather than mocking
three and letting the fourth read the live machine (the readinglist lesson).
`test_an_empty_fixture_root_finds_nothing` is the guard: a source added later
that resolves from settings instead of the fixture fails it on sight.
"""
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from server import applicationmap, applications, resumeview

NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

MASTER = """# Master History

## Building the ledger system

Between 2021 and 2024 the subject built a reconciliation ledger that ingested
counterparty statements, normalised them, and produced a cadence estimate per
merchant. The work was solo and the artifacts survive in the repository.

## Running the disposition programme

The subject coordinated a disposition programme across three regions,
sequencing dependencies between counsel, brokers and lenders.

# Endnotes

[^1]: **Ledger scope — artifact-proven.** Approved outward wording: "built a
reconciliation ledger with a deterministic cadence estimate". Do not claim
counterparty volumes; those figures are not citable.

[^2]: **Disposition programme — adjudicated.** Approved outward wording:
"coordinated a multi-region disposition programme". Never name the lenders.
"""


def _docx(path, paragraphs):
    """A minimal real .docx — the reader parses XML, so the fixture must be a
    genuine zip with word/document.xml rather than a stub."""
    body = []
    for style, text in paragraphs:
        ppr = ""
        if style == "h":
            ppr = f'<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        elif style == "li":
            ppr = '<w:pPr><w:numPr><w:ilvl w:val="0"/></w:numPr></w:pPr>'
        body.append(f'<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>')
    xml = (f'<?xml version="1.0"?><w:document xmlns:w="{NS}"><w:body>'
           + "".join(body) + "</w:body></w:document>")
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)


RESUME = [
    ("h", "AVERY STONE"),
    ("p", "avery@example.com"),
    ("h", "SELECTED BUILDS"),
    ("li", "Built a reconciliation ledger with a deterministic cadence "
           "estimate across merchant statements."),
    ("li", "Coordinated a multi-region disposition programme, sequencing "
           "dependencies between counsel and brokers."),
    ("p", "References available on request."),
]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.packages = self.tmp / "packages"
        self.package = self.packages / "acme" / "staff-engineer-2026-01-02"
        _docx(self.package / "2026-01-02_cv_acme-staff-engineer.docx", RESUME)
        _docx(self.package / "cover-letter.docx",
              [("p", "Dear Acme hiring team,"),
               ("p", "I am applying because the ledger work maps onto yours.")])
        (self.package / "V1").mkdir(parents=True, exist_ok=True)
        # Shaped like a real posting.md: find_package scores the title off the
        # FIRST line and the company off the metadata block, so a fixture that
        # skips the heading silently matches nothing.
        (self.package / "V1" / "posting.md").write_text(
            "# Staff Engineer\ncompany: acme\n"
            "Posting url: https://example.com/jobs/1\nuid: a-1\n",
            encoding="utf-8")
        (self.package / "V1" / "cover-letter.pdf").write_text("%PDF-1.4",
                                                              encoding="utf-8")

        self.record = self.tmp / "self"
        (self.record / "canon").mkdir(parents=True, exist_ok=True)
        (self.record / "canon" / "MASTER_HISTORY.md").write_text(
            MASTER, encoding="utf-8")

        self.store = self.tmp / "resume-notes.json"
        self.apps_store = self.tmp / "applications.json"
        self.role = {"uid": "a-1", "company": "Acme",
                     "title": "Staff Engineer",
                     "url": "https://example.com/jobs/1"}

        for target, value in (
                (mock.patch.object(resumeview, "STORE", self.store), None),
                (mock.patch.object(applications, "STORE", self.apps_store), None),
                (mock.patch.object(applicationmap, "packages_root",
                                   lambda: self.packages), None),
                (mock.patch.object(applications, "self_record",
                                   lambda: self.record), None)):
            target.start()
            self.addCleanup(target.stop)
        # The corpus is cached at module level on the record's mtime; a fresh
        # fixture per test must not inherit the previous one's tokens.
        resumeview._corpus_cache.update({"key": None, "nodes": [],
                                         "tokens": [], "idf": {}})
        os.environ.pop("VIRA_PASSIVE", None)


class DocumentTests(Base):
    def test_reads_the_root_word_copy(self):
        doc = resumeview.document(self.role, "resume")
        self.assertTrue(doc["found"])
        self.assertTrue(doc["path"].endswith(".docx"))
        self.assertTrue(doc["editable"], "the root copy is the editable one")
        self.assertEqual(doc["blocks"][0]["type"], "h")
        self.assertEqual(doc["blocks"][0]["text"], "AVERY STONE")
        self.assertIn("li", [b["type"] for b in doc["blocks"]])

    def test_cover_letter_is_its_own_kind(self):
        doc = resumeview.document(self.role, "cover")
        self.assertTrue(doc["found"])
        self.assertIn("Dear Acme hiring team,",
                      [b["text"] for b in doc["blocks"]])
        self.assertEqual(doc["pdf"], "cover-letter.pdf")

    def test_absent_package_is_reported_not_raised(self):
        doc = resumeview.document({"uid": "z", "company": "Nowhere",
                                   "title": "Nobody"}, "resume")
        self.assertFalse(doc["found"])
        self.assertIn("package", doc["reason"].lower())
        self.assertEqual(doc["blocks"], [])

    def test_unknown_kind_refuses(self):
        with self.assertRaises(resumeview.ViewError):
            resumeview.document(self.role, "invoice")

    def test_block_ids_survive_an_insertion_above_them(self):
        """The load-bearing property: identity is the TEXT, not the position,
        so inserting a line does not orphan every note below it."""
        before = {b["text"]: b["id"]
                  for b in resumeview.document(self.role, "resume")["blocks"]}
        _docx(self.package / "2026-01-02_cv_acme-staff-engineer.docx",
              [("p", "NEW LINE AT THE TOP")] + RESUME)
        after = {b["text"]: b["id"]
                 for b in resumeview.document(self.role, "resume")["blocks"]}
        for text, bid in before.items():
            self.assertEqual(after.get(text), bid,
                             f"id moved for {text!r} after an insertion")

    def test_duplicate_lines_get_distinct_ids(self):
        blocks = resumeview._blocks("same\n\nsame\n", "resume")
        self.assertEqual(len(blocks), 2)
        self.assertNotEqual(blocks[0]["id"], blocks[1]["id"])

    def test_source_path_refuses_an_escape(self):
        self.assertIsNone(resumeview.source_path(self.role, "resume",
                                                 "../../secrets.txt"))
        self.assertIsNotNone(resumeview.source_path(
            self.role, "resume", "cover-letter.pdf"))


class TermTests(Base):
    def test_term_key_folds_case_and_punctuation(self):
        self.assertEqual(resumeview.term_key("Evidence  Gate!"),
                         resumeview.term_key("evidence-gate"))

    def test_global_gloss_is_the_default_and_role_overrides_it(self):
        resumeview.set_term("a-1", "cadence", "what it means generally",
                            scope="global")
        ann = resumeview.annotations("a-1")
        self.assertEqual(ann["terms"][0]["note"], "what it means generally")
        self.assertEqual(ann["terms"][0]["scope"], "global")
        resumeview.set_term("a-1", "cadence", "what it means here",
                            scope="role")
        ann = resumeview.annotations("a-1")
        self.assertEqual(ann["terms"][0]["note"], "what it means here")
        self.assertEqual(ann["terms"][0]["scope"], "role")
        self.assertEqual(ann["terms"][0]["global_note"],
                         "what it means generally")

    def test_a_global_gloss_reaches_another_role_once_pinned(self):
        resumeview.set_term("a-1", "cadence", "durable", scope="global")
        self.assertEqual(resumeview.annotations("a-2")["terms"], [],
                         "an unpinned term must not appear on another rail")
        resumeview.set_term("a-2", "cadence", "", scope="role")
        ann = resumeview.annotations("a-2")
        self.assertEqual(ann["terms"][0]["note"], "durable")

    def test_unpinning_keeps_the_global_gloss(self):
        resumeview.set_term("a-1", "cadence", "durable", scope="global")
        resumeview.clear_term("a-1", "cadence")
        self.assertEqual(resumeview.annotations("a-1")["terms"], [])
        resumeview.set_term("a-1", "cadence", "", scope="role")
        self.assertEqual(resumeview.annotations("a-1")["terms"][0]["note"],
                         "durable")

    def test_a_sentence_is_refused_as_a_term(self):
        with self.assertRaises(resumeview.ViewError):
            resumeview.set_term("a-1", "this is a whole sentence that goes on "
                                       "well past any reasonable term", "x")


class StalenessTests(Base):
    def test_a_line_note_reports_stale_when_its_wording_is_gone(self):
        doc = resumeview.document(self.role, "resume")
        block = [b for b in doc["blocks"] if b["type"] == "li"][0]
        resumeview.set_line_note("a-1", block["id"], "check this figure",
                                 block["text"])
        fresh = resumeview.annotations("a-1", doc["blocks"])
        self.assertFalse(fresh["lines"][0]["stale"])

        rewritten = list(RESUME)
        rewritten[3] = ("li", "Built a different thing entirely.")
        _docx(self.package / "2026-01-02_cv_acme-staff-engineer.docx",
              rewritten)
        doc2 = resumeview.document(self.role, "resume")
        stale = resumeview.annotations("a-1", doc2["blocks"])
        self.assertTrue(stale["lines"][0]["stale"])
        self.assertIn("reconciliation ledger", stale["lines"][0]["quote"])

    def test_the_other_document_s_notes_are_absent_not_stale(self):
        """Switching to the cover letter used to report every resume note as
        stale, which says the wording changed. It did not — the note simply
        belongs to the other artifact, and the block id's kind prefix is what
        tells the two apart."""
        resume = resumeview.document(self.role, "resume")
        line = [b for b in resume["blocks"] if b["type"] == "li"][0]
        resumeview.set_line_note("a-1", line["id"], "lead with this",
                                 line["text"])
        cover = resumeview.document(self.role, "cover")
        rail = resumeview.annotations("a-1", cover["blocks"], "cover")
        self.assertEqual(rail["lines"], [],
                         "a resume note has no business on the cover rail")
        back = resumeview.annotations("a-1", resume["blocks"], "resume")
        self.assertEqual(len(back["lines"]), 1)
        self.assertFalse(back["lines"][0]["stale"])

    def test_a_term_stays_on_both_documents(self):
        resumeview.set_term("a-1", "cadence", "durable", scope="global")
        for kind in ("resume", "cover"):
            doc = resumeview.document(self.role, kind)
            rail = resumeview.annotations("a-1", doc["blocks"], kind)
            self.assertEqual(len(rail["terms"]), 1,
                             f"the term vanished on {kind}")

    def test_staleness_is_unknown_without_a_document(self):
        doc = resumeview.document(self.role, "resume")
        block = doc["blocks"][0]
        resumeview.set_line_note("a-1", block["id"], "note", block["text"])
        self.assertFalse(resumeview.annotations("a-1")["lines"][0]["stale"],
                         "no blocks given means staleness is not asserted")


class AnchorTests(Base):
    def test_the_governing_endnote_is_offered_for_a_real_claim(self):
        doc = resumeview.document(self.role, "resume")
        line = [b for b in doc["blocks"] if "reconciliation" in b["text"]][0]
        anchors = resumeview._anchors_for(line["text"])
        self.assertTrue(anchors)
        self.assertTrue(any(a["gate"] for a in anchors),
                        "a claim must be offered its claim-gate wording")
        self.assertTrue(any("cadence" in a["text"] for a in anchors))

    def test_one_shared_token_is_never_evidence(self):
        """Coverage is a ratio, so a short unrelated line reads as well
        covered on a single lucky token. Both floors exist for this."""
        self.assertEqual(
            resumeview._anchors_for("Please arrange a convenient time"), [])

    def test_a_greeting_anchors_nothing(self):
        self.assertEqual(resumeview._anchors_for("Dear hiring team,"), [])

    def test_gate_passages_get_reserved_slots(self):
        anchors = resumeview._anchors_for(
            "Coordinated a multi-region disposition programme sequencing "
            "dependencies between counsel and brokers")
        self.assertTrue(any(a["gate"] for a in anchors))

    def test_the_floors_scale_with_the_record(self):
        """The floors are multiples of the corpus's own median IDF, so a SHORT
        career record must behave like a long one. An absolute floor read as
        1.2 typical tokens on the real 510-passage record and 8.7 on a
        4-passage one, which would have answered "nothing supports this" for
        every line of a record that was merely short."""
        claim = ("Built a reconciliation ledger with a deterministic cadence "
                 "estimate across merchant statements")
        small = resumeview._anchors_for(claim)
        self.assertTrue(small, "a short record must still anchor a real claim")

        padding = "\n".join(
            f"## Unrelated chapter {i}\n\nThis passage concerns gardening, "
            f"tides, and the number {i}, and shares no language with the "
            f"career claims under test.\n" for i in range(60))
        (self.record / "canon" / "MASTER_HISTORY.md").write_text(
            MASTER + "\n" + padding, encoding="utf-8")
        resumeview._corpus_cache.update({"key": None, "nodes": [],
                                         "tokens": [], "idf": {}})
        big = resumeview._anchors_for(claim)
        self.assertTrue(big, "a longer record must still anchor the same claim")
        self.assertEqual(
            resumeview._anchors_for("Please arrange a convenient time"), [],
            "and must still reject chatter at the larger size")

    def test_the_corpus_cache_follows_the_record(self):
        first = resumeview._anchors_for("reconciliation ledger cadence "
                                        "estimate merchant statements")
        self.assertTrue(first)
        (self.record / "canon" / "MASTER_HISTORY.md").write_text(
            "# Master History\n\nNothing relevant here at all.\n",
            encoding="utf-8")
        second = resumeview._anchors_for("reconciliation ledger cadence "
                                         "estimate merchant statements")
        self.assertEqual(second, [],
                         "an edited record must not answer from the cache")


class AskTests(Base):
    def _ask(self, payload):
        with mock.patch.object(resumeview.suggest, "complete",
                               return_value=json.dumps(payload)):
            doc = resumeview.document(self.role, "resume")
            block = [b for b in doc["blocks"] if "reconciliation" in b["text"]][0]
            return resumeview.ask(self.role, "resume", "what backs this up?",
                                  block["id"])

    def test_an_answer_cites_only_anchors_it_was_given(self):
        out = self._ask({"answer": "The ledger work is artifact-proven.",
                         "anchors": [0], "supported": True})
        self.assertTrue(out["supported"])
        self.assertEqual(len(out["anchors"]), 1)

    def test_an_invented_anchor_index_is_dropped(self):
        out = self._ask({"answer": "Sure.", "anchors": [0, 99, -3],
                         "supported": True})
        self.assertEqual(len(out["anchors"]), 1,
                         "only real anchor indexes survive")

    def test_unparseable_output_degrades_rather_than_raising(self):
        with mock.patch.object(resumeview.suggest, "complete",
                               return_value="I could not answer that."):
            doc = resumeview.document(self.role, "resume")
            block = [b for b in doc["blocks"]
                     if "reconciliation" in b["text"]][0]
            out = resumeview.ask(self.role, "resume", "why?", block["id"])
        self.assertEqual(out["answer"], "")
        self.assertFalse(out["supported"])

    def test_no_anchors_says_so_without_calling_the_model(self):
        with mock.patch.object(resumeview.suggest, "complete") as m:
            out = resumeview.ask(self.role, "resume",
                                 "Please arrange a convenient time", "")
        m.assert_not_called()
        self.assertFalse(out["supported"])

    def test_a_blank_question_refuses(self):
        with self.assertRaises(resumeview.ViewError):
            resumeview.ask(self.role, "resume", "   ", "")


class FeedbackTests(Base):
    def test_role_feedback_lands_on_the_role(self):
        out = resumeview.feedback("a-1", "role", "the summary is too long")
        self.assertEqual(out["routed"], "application")
        state = json.loads(self.apps_store.read_text(encoding="utf-8"))
        self.assertIn("the summary is too long",
                      state["roles"]["a-1"]["comments"][0]["text"])

    def test_role_feedback_reaches_the_next_package_build(self):
        """The UI promises this feedback is read back into the next build, so
        it must actually reach apply_prompt. It did not: find_role returns the
        CATALOG record and never merges owner state, so reading comments off
        the role dict would have been a silent no-op."""
        resumeview.feedback("a-1", "role", "drop the second bullet entirely")
        prompt = applications.apply_prompt(self.role)
        self.assertIn("drop the second bullet entirely", prompt)
        self.assertIn("never evidence for a claim", prompt,
                      "feedback must be framed as drafting instruction only")

    def test_a_role_with_no_feedback_adds_no_block(self):
        self.assertNotIn("OWNER FEEDBACK",
                         applications.apply_prompt(self.role))

    def test_broader_feedback_goes_to_the_journal_not_the_record(self):
        with mock.patch("server.journal.add",
                        return_value={"id": "j1"}) as add:
            out = resumeview.feedback("a-1", "broader",
                                      "I never want that framing again")
        add.assert_called_once()
        self.assertEqual(out["routed"], "journal")
        self.assertEqual(add.call_args.args[0],
                         "I never want that framing again",
                         "the note reaches the journal verbatim")
        self.assertIn("viewport",
                      add.call_args.kwargs.get("context", "").lower())
        self.assertEqual((self.record / "canon" / "MASTER_HISTORY.md")
                         .read_text(encoding="utf-8"), MASTER,
                         "the career record is never written here")

    def test_a_passive_instance_refuses_a_broader_note(self):
        os.environ["VIRA_PASSIVE"] = "1"
        self.addCleanup(os.environ.pop, "VIRA_PASSIVE", None)
        with self.assertRaises(resumeview.ViewError):
            resumeview.feedback("a-1", "broader", "something durable")

    def test_a_passive_instance_still_takes_role_feedback(self):
        os.environ["VIRA_PASSIVE"] = "1"
        self.addCleanup(os.environ.pop, "VIRA_PASSIVE", None)
        out = resumeview.feedback("a-1", "role", "tighten the opening")
        self.assertEqual(out["routed"], "application")

    def test_empty_and_unknown_scope_refuse(self):
        with self.assertRaises(resumeview.ViewError):
            resumeview.feedback("a-1", "role", "  ")
        with self.assertRaises(resumeview.ViewError):
            resumeview.feedback("a-1", "elsewhere", "text")


class IsolationGuard(Base):
    def test_an_empty_fixture_root_finds_nothing(self):
        """If a source ever resolves from settings instead of the fixture,
        this test starts reading the real machine and fails."""
        empty = self.tmp / "empty"
        empty.mkdir()
        with mock.patch.object(applicationmap, "packages_root",
                               lambda: empty), \
             mock.patch.object(applications, "self_record",
                               lambda: empty):
            resumeview._corpus_cache.update({"key": None, "nodes": [],
                                             "tokens": [], "idf": {}})
            doc = resumeview.document(self.role, "resume")
            self.assertFalse(doc["found"])
            self.assertEqual(resumeview._anchors_for("reconciliation ledger "
                                                     "cadence merchant"), [])


if __name__ == "__main__":
    unittest.main()
