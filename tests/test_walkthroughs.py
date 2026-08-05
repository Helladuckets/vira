"""Session walkthrough discovery — the films off lab_root.

Every case roots the module at a tmp fixture rather than letting it resolve
lab_root from settings. That is not ceremony: the live lab holds 32 real
films, and a suite that reads it would pass or fail on what happens to be on
this machine (the readinglist isolation lesson, one module over).
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import walkthroughs


def _film(root, slug, *, title="", motion=True, thumb=True, index=True):
    d = root / slug
    d.mkdir(parents=True)
    if index:
        head = f"<title>{title}</title>" if title else ""
        (d / "index.html").write_text(f"<html><head>{head}</head></html>",
                                      encoding="utf-8")
    if motion:
        (d / "motion.mp4").write_bytes(b"\x00")
    if thumb:
        (d / "thumb.jpg").write_bytes(b"\x00")
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lab = Path(self.tmp.name) / "lab"
        self.films = self.lab / "walkthroughs"
        self.films.mkdir(parents=True)

    def registry(self, rows):
        (self.lab / "prototypes.json").write_text(
            json.dumps(rows), encoding="utf-8")


class SlugParsing(Base):
    def test_date_is_found_anywhere_in_the_slug(self):
        # `vira-2026-07-13-test-badge` puts the date in the MIDDLE.
        self.assertEqual(walkthroughs._date_of("vira-reader-2026-07-27"),
                         "2026-07-27")
        self.assertEqual(walkthroughs._date_of("vira-2026-07-13-test-badge"),
                         "2026-07-13")
        self.assertEqual(walkthroughs._date_of("no-date-here"), "")

    def test_project_is_the_leading_token(self):
        self.assertEqual(walkthroughs.project_of("vira-reader-2026-07-27"),
                         "vira")
        self.assertEqual(walkthroughs.project_of("qocha-2026-07-20"), "qocha")

    def test_subject_is_what_sits_between_project_and_date(self):
        self.assertEqual(walkthroughs.subject_of("vira-group-profiles-2026-07-28"),
                         "group profiles")

    def test_a_slug_with_no_subject_invents_none(self):
        # `vira-2026-07-13` predates the naming convention. Empty is honest.
        self.assertEqual(walkthroughs.subject_of("vira-2026-07-13"), "")

    def test_a_subject_after_the_date_is_still_found(self):
        self.assertEqual(walkthroughs.subject_of("vira-2026-07-13-test-badge"),
                         "test badge")


class Discovery(Base):
    def test_a_directory_without_an_index_is_not_a_film(self):
        _film(self.films, "vira-good-2026-07-01")
        _film(self.films, "vira-bad-2026-07-02", index=False)
        got = walkthroughs.films(self.films)
        self.assertEqual([f["slug"] for f in got], ["vira-good-2026-07-01"])

    def test_underscore_and_dot_directories_are_skipped(self):
        _film(self.films, "vira-real-2026-07-01")
        _film(self.films, "_src")
        _film(self.films, ".cache")
        self.assertEqual(len(walkthroughs.films(self.films)), 1)

    def test_films_are_newest_first(self):
        _film(self.films, "vira-old-2026-07-01")
        _film(self.films, "vira-new-2026-07-30")
        got = [f["slug"] for f in walkthroughs.films(self.films)]
        self.assertEqual(got, ["vira-new-2026-07-30", "vira-old-2026-07-01"])

    def test_motion_and_thumb_report_what_is_actually_on_disk(self):
        _film(self.films, "vira-a-2026-07-01", motion=False, thumb=False)
        f = walkthroughs.films(self.films)[0]
        self.assertFalse(f["motion"])
        self.assertEqual(f["thumb"], "")

    def test_dormant_without_a_root(self):
        self.assertEqual(walkthroughs.films(self.lab / "nope"), [])

    def test_an_explicit_root_outranks_settings(self):
        """The isolation seam. A caller passing a directory must never have
        settings' lab_root read instead — that is what leaks the real lab."""
        _film(self.films, "vira-a-2026-07-01")
        with mock.patch.object(walkthroughs.settings, "raw",
                               return_value={"lab_root": "/nonexistent/lab"}):
            self.assertEqual(len(walkthroughs.films(self.films)), 1)


class Metadata(Base):
    def test_the_registry_name_wins(self):
        _film(self.films, "vira-a-2026-07-01", title="From the page")
        self.registry([{"path": "walkthroughs/vira-a-2026-07-01/",
                        "name": "From the registry",
                        "description": "what it is"}])
        f = walkthroughs.films(self.films)[0]
        self.assertEqual(f["title"], "From the registry")
        self.assertEqual(f["description"], "what it is")
        self.assertTrue(f["registered"])

    def test_the_page_title_is_the_fallback(self):
        _film(self.films, "vira-a-2026-07-01", title="Session Walkthrough")
        f = walkthroughs.films(self.films)[0]
        self.assertEqual(f["title"], "Session Walkthrough")
        self.assertFalse(f["registered"])

    def test_an_unregistered_film_still_lists(self):
        """The registry is a publication record. A film that was never
        registered is still a film."""
        _film(self.films, "vira-reader-2026-07-27")
        self.registry([{"path": "walkthroughs/other/", "name": "Other"}])
        got = walkthroughs.films(self.films)
        self.assertEqual(len(got), 1)
        self.assertFalse(got[0]["registered"])
        # Falls back to project + subject off the slug: "Walkthrough — Vira reader"
        self.assertIn("reader", got[0]["title"])

    def test_a_malformed_registry_costs_metadata_not_rows(self):
        _film(self.films, "vira-a-2026-07-01", title="Page title")
        (self.lab / "prototypes.json").write_text("{{ not json",
                                                  encoding="utf-8")
        got = walkthroughs.films(self.films)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["title"], "Page title")

    def test_non_walkthrough_registry_rows_are_ignored(self):
        _film(self.films, "vira-a-2026-07-01")
        self.registry([{"path": "brain/", "name": "Brain"},
                       {"path": "walkthroughs/vira-a-2026-07-01/",
                        "name": "Mine"}])
        self.assertEqual(walkthroughs.films(self.films)[0]["title"], "Mine")


class Rows(Base):
    def test_created_is_the_films_own_date_not_the_mtime(self):
        """These are rsynced, so mtime is when the copy landed. Reading it
        would file all 32 as brand new and queue every one of them."""
        _film(self.films, "vira-a-2026-07-01")
        row = walkthroughs.rows(self.films)[0]
        self.assertTrue(row["created"].startswith("2026-07-01"))

    def test_rows_carry_no_ref(self):
        """readinglist stores a 64-char string there, not a record. The film's
        metadata is joined at the route layer instead, so recapturing a film
        updates the row with nothing to re-register."""
        _film(self.films, "vira-a-2026-07-01")
        self.assertNotIn("ref", walkthroughs.rows(self.films)[0])

    def test_rows_are_the_readinglist_contract(self):
        _film(self.films, "vira-a-2026-07-01")
        row = walkthroughs.rows(self.films)[0]
        self.assertEqual(row["kind"], "walkthrough")
        self.assertEqual(row["locator_kind"], "url")
        self.assertEqual(row["locator"], "/walkthroughs/vira-a-2026-07-01/")

    def test_dormant_rows_are_empty(self):
        self.assertEqual(walkthroughs.rows(self.lab / "nope"), [])


if __name__ == "__main__":
    unittest.main()
