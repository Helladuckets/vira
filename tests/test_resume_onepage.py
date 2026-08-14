"""The one-page companion resume: told apart, never confused with the record.

The package ships two resumes now. Everything here pins the ONE rule that
keeps them apart — `applicationmap.is_one_pager` — and the two readers that
consume it, because a mix-up is silent in both directions: the Map would
double-count every surviving bullet, and the Read pane would show whichever
file happened to sort first.

Rooted at one tmp packages root, like test_resumeview.
"""

import re
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from server import applicationmap, resumeview

NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx(path, lines):
    body = "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in lines)
    xml = (f'<?xml version="1.0"?><w:document xmlns:w="{NS}"><w:body>'
           + body + "</w:body></w:document>")
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)


TWO_PAGE = [
    "AVERY STONE",
    "Built a reconciliation ledger with a deterministic cadence estimate.",
    "Coordinated a multi-region disposition programme across three regions.",
    "Ran the analyst desk and rebuilt its intake from first principles.",
]
ONE_PAGE = [
    "AVERY STONE",
    "Built a reconciliation ledger with a deterministic cadence estimate.",
]


class NameRule(unittest.TestCase):
    def test_the_suffix_is_what_marks_the_companion(self):
        self.assertTrue(applicationmap.is_one_pager("2026-01-02_cv_acme_1p.docx"))
        self.assertTrue(applicationmap.is_one_pager("2026-01-02_cv_acme_1p.md"))
        self.assertTrue(applicationmap.is_one_pager("2026-01-02_cv_acme_1p.pdf"))
        self.assertFalse(applicationmap.is_one_pager("2026-01-02_cv_acme.docx"))

    def test_a_full_path_reads_the_same_as_a_bare_name(self):
        self.assertTrue(applicationmap.is_one_pager(
            Path("/pkg/V1/2026-01-02_cv_acme_1p.md")))

    def test_the_marker_only_counts_at_the_end_of_the_stem(self):
        # A target slug that happens to contain the token is not a one-pager;
        # only the suffix immediately before the extension marks one.
        self.assertFalse(applicationmap.is_one_pager("2026-01-02_cv_1provider.docx"))
        self.assertFalse(applicationmap.is_one_pager("2026-01-02_cv_1p_acme.docx"))

    def test_case_does_not_decide_it(self):
        self.assertTrue(applicationmap.is_one_pager("2026-01-02_cv_acme_1P.docx"))


class _PackageCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="onepage-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.packages = self.tmp / "packages"
        self.package = self.packages / "acme" / "staff-engineer-2026-01-02"
        _docx(self.package / "2026-01-02_cv_acme.docx", TWO_PAGE)
        _docx(self.package / "2026-01-02_cv_acme_1p.docx", ONE_PAGE)
        v1 = self.package / "V1"
        v1.mkdir(parents=True, exist_ok=True)
        (v1 / "posting.md").write_text(
            "# Staff Engineer\ncompany: acme\nuid: a-1\n", encoding="utf-8")
        (v1 / "2026-01-02_cv_acme.pdf").write_text("%PDF", encoding="utf-8")
        (v1 / "2026-01-02_cv_acme_1p.pdf").write_text("%PDF", encoding="utf-8")
        p = mock.patch.object(applicationmap, "packages_root",
                              lambda: self.packages)
        p.start()
        self.addCleanup(p.stop)
        self.role = {"uid": "a-1", "company": "Acme", "title": "Staff Engineer"}


class MapLane(_PackageCase):
    def test_the_resume_lane_reads_the_two_page_record(self):
        nodes, path = applicationmap._lane_artifact(self.package, "resume")
        self.assertTrue(path.name.endswith("_cv_acme.docx"), path.name)
        self.assertFalse(applicationmap.is_one_pager(path))

    def test_the_lane_reads_the_whole_record(self):
        nodes, _ = applicationmap._lane_artifact(self.package, "resume")
        texts = [n["text"] for n in nodes]
        # The line the one-pager drops must still be in the map.
        self.assertTrue(any("disposition programme" in t for t in texts))
        self.assertEqual(len(texts), len(set(texts)))

    def test_an_unreadable_record_never_falls_through_to_the_companion(self):
        # `_read_artifact` takes the first candidate that yields NODES, so
        # without the filter an empty two-pager silently substitutes the
        # distillation and the Map reports on a SUBSET as though it were the
        # record.  Mutation-checked: deleting the filter fails this case.
        _docx(self.package / "2026-01-02_cv_acme.docx", [])
        nodes, path = applicationmap._lane_artifact(self.package, "resume")
        self.assertEqual(nodes, [])
        self.assertIsNone(path)

    def test_a_package_with_only_a_companion_maps_nothing(self):
        # The record is what the Map maps; a package shipping only the
        # companion has none, and saying so beats mapping the wrong document.
        (self.package / "2026-01-02_cv_acme.docx").unlink()
        nodes, path = applicationmap._lane_artifact(self.package, "resume")
        self.assertEqual(nodes, [])
        self.assertIsNone(path)

    def test_the_cover_lane_is_untouched_by_the_filter(self):
        _docx(self.package / "cover-letter.docx", ["Dear Acme hiring team,"])
        nodes, path = applicationmap._lane_artifact(self.package, "cover")
        self.assertEqual(path.name, "cover-letter.docx")


class ReadPane(_PackageCase):
    def test_both_forms_are_offered(self):
        self.assertIn("resume", resumeview.KINDS)
        self.assertIn("resume1p", resumeview.KINDS)
        self.assertEqual(resumeview.KIND_LABEL["resume1p"], "Resume (1 page)")

    def test_each_kind_serves_its_own_file(self):
        two = resumeview.document(self.role, "resume")
        one = resumeview.document(self.role, "resume1p")
        self.assertTrue(two["path"].endswith("_cv_acme.docx"), two["path"])
        self.assertTrue(one["path"].endswith("_cv_acme_1p.docx"), one["path"])
        self.assertNotEqual(two["path"], one["path"])

    def test_each_kind_links_its_own_pdf(self):
        self.assertEqual(resumeview.document(self.role, "resume")["pdf"],
                         "2026-01-02_cv_acme.pdf")
        self.assertEqual(resumeview.document(self.role, "resume1p")["pdf"],
                         "2026-01-02_cv_acme_1p.pdf")

    def test_annotations_cannot_cross_between_the_two_resumes(self):
        # A bullet surviving into the one-pager is the SAME sentence, so the
        # kind prefix is the only thing keeping their notes apart — without it
        # a note on one would render as an anchor on the other.
        two = {b["text"]: b["id"]
               for b in resumeview.document(self.role, "resume")["blocks"]}
        one = {b["text"]: b["id"]
               for b in resumeview.document(self.role, "resume1p")["blocks"]}
        shared = set(two) & set(one)
        self.assertTrue(shared, "fixture must share a line")
        for text in shared:
            self.assertNotEqual(two[text], one[text])
        self.assertTrue(all(i.startswith("resume-") for i in two.values()))
        self.assertTrue(all(i.startswith("resume1p-") for i in one.values()))

    def test_a_missing_companion_reports_it_rather_than_serving_the_record(self):
        (self.package / "2026-01-02_cv_acme_1p.docx").unlink()
        out = resumeview.document(self.role, "resume1p")
        self.assertFalse(out["found"])
        self.assertIn("resume (1 page)", out["reason"].lower())
        self.assertEqual(out["path"], "")

    def test_the_cover_letter_is_filtered_by_neither_form(self):
        self.assertIsNone(resumeview._one_page_filter("cover"))
        self.assertIs(resumeview._one_page_filter("resume"), False)
        self.assertIs(resumeview._one_page_filter("resume1p"), True)


class Markup(unittest.TestCase):
    def test_every_kind_has_a_button_and_every_button_a_kind(self):
        root = Path(__file__).resolve().parent.parent
        html = (root / "static" / "index.html").read_text(encoding="utf-8")
        block = html.split('id="doc-kind"', 1)[1].split("</div>", 1)[0]
        shown = set(re.findall(r'data-kind="([^"]+)"', block))
        self.assertEqual(shown, set(resumeview.KINDS))


if __name__ == "__main__":
    unittest.main()
