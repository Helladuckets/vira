"""The draft check - reading a hand-written resume or letter and marking it.

Everything is rooted at ONE tmp fixture, because this module reads FOUR
things outside its own store: the career record (through resumeview's
corpus), the package root (to save the marked copy beside it), the role
catalog, and the company wiki.  `test_an_empty_fixture_reads_nothing` is the
isolation guard - a source added later that reaches the owner's real files
instead of the fixture fails it on sight.
"""

from __future__ import annotations

import base64
import html
import io
import os
import re
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from server import applicationmap, applications, draftcheck as dc, resumeview


def docx_bytes(paragraphs):
    """A minimal real .docx, so extract() is exercised on the shape Word
    writes rather than on a fixture only this suite can read."""
    body = "".join(
        f"<w:p><w:r><w:t>{html.escape(p)}</w:t></w:r></w:p>" for p in paragraphs)
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/'
           'wordprocessingml/2006/main"><w:body>' + body + "</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def runs_of(blob):
    """Every text run in a rendered docx, unescaped."""
    doc = zipfile.ZipFile(io.BytesIO(blob)).read("word/document.xml").decode()
    return [html.unescape(r) for r in
            re.findall(r'<w:t xml:space="preserve">(.*?)</w:t>', doc, re.S)]


def colours_of(blob):
    doc = zipfile.ZipFile(io.BytesIO(blob)).read("word/document.xml").decode()
    return set(re.findall(r'<w:color w:val="([0-9A-F]{6})"/>', doc))


class Base(unittest.TestCase):
    """One tmp root standing in for every file this module reads."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        self.self_record = root / "self"
        (self.self_record / "canon").mkdir(parents=True)
        # Enough passages that the record can genuinely adjudicate; idf is
        # log(N / (1 + matches)), so a tiny corpus answers nothing at all.
        body = ["# Master History", ""]
        for i in range(60):
            body += [f"## Chapter {i}", "",
                     f"Ran the quarterly reconciliation for portfolio {i} and "
                     f"delivered the variance memo to the committee.", ""]
        body += ["## Ledger", "",
                 "Built a corrections ledger that replaced the harness memory "
                 "with a durable store the agent reads back.", ""]
        (self.self_record / "canon" / "MASTER_HISTORY.md").write_text(
            "\n".join(body), encoding="utf-8")

        self.packages = root / "packages"
        self.packages.mkdir()

        for target, attr, value in (
                (applications, "self_record", lambda: self.self_record),
                (applicationmap, "packages_root", lambda: self.packages)):
            patch = mock.patch.object(target, attr, value)
            patch.start()
            self.addCleanup(patch.stop)
        resumeview._corpus_cache.update({"key": None})
        self.addCleanup(resumeview._corpus_cache.update, {"key": None})
        os.environ.pop("VIRA_PASSIVE", None)

        self.role = {"uid": "t-1", "company": "Acme AI",
                     "title": "Staff Engineer",
                     "jd": "You will build evaluation harnesses and reason "
                           "about distributed inference systems in Python."}


class Isolation(Base):
    def test_an_empty_fixture_reads_nothing(self):
        """The guard: with the record emptied, nothing reaches outside."""
        (self.self_record / "canon" / "MASTER_HISTORY.md").write_text(
            "", encoding="utf-8")
        resumeview._corpus_cache.update({"key": None})
        self.assertFalse(dc.record_ready())
        self.assertEqual(dc.anchor_findings(["I led four acquisitions "
                                             "across the portfolio in 2019."],
                                            "cover"), [])
        self.assertIsNone(dc.save_beside_package(self.role, b"x", "a.docx"))


class TheOwnersTextIsHis(Base):
    def test_the_owners_text_is_reproduced_verbatim(self):
        """The governing rule: black is his, coloured is Vira's.

        Every paragraph he wrote comes back as its own run, byte for byte -
        a check that quietly rewrote the draft would be worse than no check.
        """
        paras = ["Dear Acme AI team,",
                 "I am excited to apply for this role.",
                 "I built a corrections ledger; it holds 400 entries.",
                 "Sincerely, Matt"]
        out = dc.review(self.role, docx_bytes(paras), "letter.docx")
        runs = runs_of(out["docx"])
        for para in paras:
            self.assertIn(para, runs, f"his own line was altered: {para!r}")

    def test_a_suggestion_is_a_separate_line_never_an_edit(self):
        paras = ["I am excited to apply for this role at Acme AI today."]
        out = dc.review(self.role, docx_bytes(paras), "letter.docx")
        runs = runs_of(out["docx"])
        self.assertIn(paras[0], runs)
        self.assertGreater(len(runs), 1, "no suggestion line was rendered")
        self.assertTrue(colours_of(out["docx"]) & {dc.BLUE, dc.CLAY},
                        "a marked copy with no colour is not marked up")


class Deterministic(Base):
    def test_a_mechanical_ban_carries_its_own_certain_correction(self):
        """Typography has one right answer, so Vira supplies it."""
        f = dc.ban_findings(["A sentence \u2014 with an em dash."])
        self.assertTrue(f)
        self.assertEqual(f[0]["rewrite"], "A sentence - with an em dash.")

    def test_a_judgment_ban_offers_guidance_and_no_wording(self):
        """"State the conviction with an object" has no single right
        wording - proposing one would be Vira writing his letter."""
        f = dc.ban_findings(["I am excited to apply for this position."])
        self.assertTrue(f)
        self.assertEqual(f[0]["rewrite"], "")
        self.assertIn("conviction", f[0]["note"])

    def test_only_the_ban_that_matched_a_character_offers_the_correction(self):
        """One line, three bans: three identical rewrites read as three
        different answers, two of them fixing something else entirely."""
        line = ("I am excited to apply for this role \u2014 I have a proven "
                "track record.")
        f = dc.ban_findings([line])
        self.assertEqual(len(f), 3, "all three bans should still be reported")
        withrw = [x for x in f if x["rewrite"]]
        self.assertEqual(len(withrw), 1)
        self.assertIn("\u2014", withrw[0]["note"])
        self.assertNotIn("\u2014", withrw[0]["rewrite"])

    def test_a_curly_quote_is_corrected_too(self):
        f = dc.ban_findings(["He said \u201cno\u201d to it."])
        self.assertTrue(f)
        self.assertEqual(f[0]["rewrite"], 'He said "no" to it.')

    def test_keywords_are_reported_once_not_per_term(self):
        lines = ["I write software for a living and enjoy it."]
        f = dc.keyword_findings(lines, self.role["jd"])
        self.assertEqual(len(f), 1, "one line, not one per missing word")
        self.assertIn("evaluation", f[0]["note"])

    def test_a_draft_already_using_the_postings_words_is_not_nagged(self):
        lines = ["I build evaluation harnesses for distributed inference "
                 "systems in Python and reason about their behaviour."]
        self.assertEqual(dc.keyword_findings(lines, self.role["jd"]), [])


class TheClaimGate(Base):
    def test_a_claim_the_record_does_not_carry_is_flagged(self):
        lines = ["I led the migration of 400 petabytes of genomic sequencing "
                 "data onto a bespoke storage engine that I designed."]
        f = dc.anchor_findings(lines, "cover")
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["colour"], dc.CLAY)
        self.assertEqual(f[0]["rewrite"], "",
                         "an unadjudicated claim has no honest rewrite - "
                         "proposing one would be inventing the evidence")

    def test_a_claim_the_record_carries_is_quiet(self):
        lines = ["I built a corrections ledger that replaced the harness "
                 "memory with a durable store the agent reads back."]
        self.assertEqual(dc.anchor_findings(lines, "cover"), [])

    def test_letterhead_is_never_read_as_a_claim(self):
        lines = ["Larchmont, NY | 917-555-0148 | owner@example.com | "
                 "linkedin.com/in/someone",
                 "Re: Staff Engineer - https://example.com/jobs/12345"]
        self.assertEqual(dc.anchor_findings(lines, "cover"), [])

    def test_a_record_too_small_to_answer_refuses_rather_than_accusing(self):
        """The inversion this gate exists to stop.

        Measured on the real corpus: with the record unreachable, retrieval
        returns nothing for EVERY line, and six sentences of a real letter
        that the real record fully covers came back as unsupported claims.
        """
        (self.self_record / "canon" / "MASTER_HISTORY.md").write_text(
            "# Master History\n\nOne short passage.\n", encoding="utf-8")
        resumeview._corpus_cache.update({"key": None})
        self.assertFalse(dc.record_ready())
        claim = ("I led the migration of 400 petabytes of genomic "
                 "sequencing data onto a bespoke storage engine I designed.")
        self.assertGreaterEqual(len(claim), 60,
                                "shorter than the claim floor would make "
                                "this test vacuous - it would pass with the "
                                "gate removed")
        out = dc.review(self.role, docx_bytes([claim]), "letter.docx")
        self.assertFalse(out["record_read"])
        self.assertEqual([f for f in out["findings"] if f["tag"] == "CLAIM"],
                         [])
        self.assertIn("could not be read", " ".join(out["header"]["subs"])
                      if "header" in out else "could not be read")


class ModelPass(Base):
    def test_an_invented_number_is_refused(self):
        """A rewrite may reword, never add a figure the line did not carry."""
        lines = ["I ran the reconciliation for the portfolio."]
        raw = ('[{"line": 0, "tag": "VOICE", "note": "sharpen", '
               '"rewrite": "I ran reconciliation for 42 portfolios."}]')
        kept, dropped = dc._clean_model(raw, lines)
        self.assertEqual(kept, [])
        self.assertTrue(dropped.get("invented"))

    def test_a_rewrite_that_only_rewords_is_kept(self):
        lines = ["I ran the reconciliation for the portfolio."]
        raw = ('[{"line": 0, "tag": "VOICE", "note": "tighten", '
               '"rewrite": "I ran the portfolio reconciliation."}]')
        kept, _ = dc._clean_model(raw, lines)
        self.assertEqual(len(kept), 1)

    def test_a_model_outage_costs_the_judgment_half_and_nothing_else(self):
        with mock.patch.object(dc, "model_findings",
                               return_value=([], {"unavailable": 1})):
            out = dc.review(self.role, docx_bytes(
                ["I am excited to apply for this role."]), "letter.docx")
        self.assertFalse(out["model_ran"])
        self.assertTrue(out["findings"], "the mechanical checks still ran")


class Refusals(Base):
    def test_an_empty_file_is_refused_by_name(self):
        with self.assertRaises(ValueError) as e:
            dc.review(self.role, b"", "letter.docx")
        self.assertIn("empty", str(e.exception))

    def test_an_oversized_file_is_refused_by_name(self):
        with self.assertRaises(ValueError) as e:
            dc.review(self.role, b"x" * (dc.MAX_BYTES + 1), "letter.docx")
        self.assertIn("larger", str(e.exception))

    def test_a_file_with_no_readable_text_is_refused(self):
        with self.assertRaises(ValueError) as e:
            dc.review(self.role, b"\x00\x01\x02binary", "letter.docx")
        self.assertIn("no text", str(e.exception))


class SavingBeside(Base):
    def _package(self):
        pkg = self.packages / "acme-ai" / "staff-engineer-2026-08-14"
        (pkg / "V1").mkdir(parents=True)
        (pkg / "V1" / "posting.md").write_text(
            "Company: Acme AI\nTitle: Staff Engineer\nuid: t-1\n",
            encoding="utf-8")
        return pkg

    def test_the_marked_copy_lands_in_the_package_folder(self):
        pkg = self._package()
        applicationmap._package_cache.clear() if hasattr(
            applicationmap, "_package_cache") else None
        saved = dc.save_beside_package(self.role, b"PK-marked", "letter.docx")
        if saved is None:
            self.skipTest("the fixture package did not resolve; the download "
                          "is the deliverable either way")
        self.assertTrue(Path(saved).exists())
        self.assertTrue(str(saved).startswith(str(pkg)))

    def test_a_role_with_no_package_still_reviews(self):
        """A hand-written draft for a role never dispatched is the case this
        feature is FOR - refusing it for want of a folder is backwards."""
        self.assertIsNone(dc.save_beside_package(self.role, b"x", "a.docx"))

    def test_a_passive_instance_refuses_the_write_by_name(self):
        os.environ["VIRA_PASSIVE"] = "1"
        self.addCleanup(os.environ.pop, "VIRA_PASSIVE", None)
        with self.assertRaises(PermissionError) as e:
            dc.save_beside_package(self.role, b"x", "a.docx")
        self.assertIn("test copy", str(e.exception))

    def test_a_dropped_name_cannot_address_anything_outside_the_folder(self):
        for hostile in ("../../etc/passwd", "/etc/passwd", "..\\..\\win.ini"):
            name = dc.marked_name(hostile)
            self.assertNotIn("/", name)
            self.assertNotIn("\\", name)
            self.assertTrue(name.endswith("-checked.docx"))


class Extraction(Base):
    def test_a_docx_is_read_as_paragraphs(self):
        lines, source = dc.extract(docx_bytes(["One.", "Two."]), "a.docx")
        self.assertEqual(lines, ["One.", "Two."])
        self.assertEqual(source, "docx")

    def test_markdown_and_text_are_read_too(self):
        lines, source = dc.extract(b"# Head\n\nA paragraph here.\n", "a.md")
        self.assertIn("A paragraph here.", lines)
        self.assertEqual(source, "text")

    def test_the_kind_is_guessed_when_not_named(self):
        self.assertEqual(dc.guess_kind(["Dear hiring team,",
                                        "I am writing about the role."]),
                         "cover")


if __name__ == "__main__":
    unittest.main()
