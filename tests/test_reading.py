"""Reading-room done-marks: per-list JSON stores under data/reading/,
toggle idempotence, legacy-set merge, and list-name validation.

Run: .venv/bin/python -m unittest tests.test_reading
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import reading


class ReadingDoneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(
            reading, "STORE_DIR", Path(self.tmp.name) / "reading")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_empty_list_is_empty(self):
        self.assertEqual(reading.get_done("anthropic-universe"), [])

    def test_toggle_roundtrip(self):
        out = reading.set_done("mylist", "abc123", True)
        self.assertEqual(out, ["abc123"])
        self.assertEqual(reading.get_done("mylist"), ["abc123"])
        out = reading.set_done("mylist", "abc123", False)
        self.assertEqual(out, [])
        self.assertEqual(reading.get_done("mylist"), [])

    def test_toggle_idempotent(self):
        reading.set_done("mylist", "x", True)
        reading.set_done("mylist", "x", True)
        self.assertEqual(reading.get_done("mylist"), ["x"])
        reading.set_done("mylist", "x", False)
        reading.set_done("mylist", "x", False)
        self.assertEqual(reading.get_done("mylist"), [])

    def test_merge_unions_without_clobbering(self):
        reading.set_done("mylist", "a", True)
        out = reading.merge_done("mylist", ["b", "c", "a"])
        self.assertEqual(sorted(out), ["a", "b", "c"])
        # merge never removes
        out = reading.merge_done("mylist", [])
        self.assertEqual(sorted(out), ["a", "b", "c"])

    def test_lists_are_isolated(self):
        reading.set_done("one", "a", True)
        reading.set_done("two", "b", True)
        self.assertEqual(reading.get_done("one"), ["a"])
        self.assertEqual(reading.get_done("two"), ["b"])

    def test_bad_names_rejected(self):
        for bad in ("", "UPPER", "has space", "a/b", "../etc", "-lead", None):
            with self.assertRaises(ValueError):
                reading.get_done(bad)


GENERATOR_PAGE = """<!DOCTYPE html><html><head><title>Test Room</title></head>
<body><header><h1>Test Room</h1><p>Everything on one subject.</p></header>
<main id="list"></main>
<footer>Built 2026-07-20 by Vira.</footer>
<script>window.ROOM={"slug":"test-room"};window.DATA=[{"id":"aaa111","title":"A"},{"id":"bbb222","title":"B"},{"id":"ccc333","title":"C"}];</script>
</body></html>"""

HANDBUILT_PAGE = """<!DOCTYPE html><html><head><title>Hand Room</title></head>
<body><header><h1>Hand Room</h1><p>The full universe. Built 2026-07-17.</p></header>
<script>
const DATA = [{"id": "d1", "title": "One"}, {"id": "d2", "title": "Two"}];
const API = "/api/reading/hand-room/done";
</script></body></html>"""


class PageDetailsTest(unittest.TestCase):
    """The card-facing view: subtitle, item count, done count, built date —
    parsed from both generations of room page (window.DATA and const DATA)."""

    def setUp(self):
        from server import readingroom
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        for attr, sub in (("STORE_DIR", "data"), ("PAGES_DIR", "pages")):
            p = mock.patch.object(reading, attr, base / sub)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(readingroom, "ROOMS_DIR", base / "rooms")
        p.start()
        self.addCleanup(p.stop)
        (base / "pages").mkdir()
        self.addCleanup(self.tmp.cleanup)

    def page(self, name, text):
        (reading.PAGES_DIR / f"{name}.html").write_text(text, encoding="utf-8")

    def test_generator_page_yields_every_field(self):
        self.page("test-room", GENERATOR_PAGE)
        row = reading.page_details()[0]
        self.assertEqual(row["title"], "Test Room")
        self.assertEqual(row["subtitle"], "Everything on one subject.")
        self.assertEqual(row["built"], "2026-07-20")
        self.assertEqual(row["items"], 3)
        self.assertEqual(row["done"], 0)

    def test_handbuilt_page_parses_too(self):
        self.page("hand-room", HANDBUILT_PAGE)
        row = reading.page_details()[0]
        self.assertEqual(row["items"], 2)
        self.assertEqual(row["built"], "2026-07-17")

    def test_done_counts_only_marks_the_page_still_carries(self):
        # A rebuilt room may have dropped an item; its orphaned mark must not
        # overcount the card.
        self.page("test-room", GENERATOR_PAGE)
        reading.set_done("test-room", "aaa111", True)
        reading.set_done("test-room", "gone999", True)
        row = reading.page_details()[0]
        self.assertEqual(row["done"], 1)

    def test_unparseable_page_omits_items_never_claims_zero(self):
        self.page("odd", "<html><head><title>Odd</title></head><body></body></html>")
        reading.set_done("odd", "x", True)
        row = reading.page_details()[0]
        self.assertNotIn("items", row)
        self.assertEqual(row["done"], 1)   # raw store count, unintersected

    def test_bad_ids_rejected(self):
        with self.assertRaises(ValueError):
            reading.set_done("mylist", "", True)
        with self.assertRaises(ValueError):
            reading.set_done("mylist", None, True)

    def test_long_ids_truncated_consistently(self):
        long_id = "z" * 200
        reading.set_done("mylist", long_id, True)
        self.assertEqual(reading.get_done("mylist"), ["z" * 64])
        reading.set_done("mylist", long_id, False)
        self.assertEqual(reading.get_done("mylist"), [])


class ReadingPagesTest(unittest.TestCase):
    def setUp(self):
        from server import readingroom
        self.tmp = tempfile.TemporaryDirectory()
        self.pages = Path(self.tmp.name) / "reading"
        for target, attr, val in (
                (reading, "PAGES_DIR", self.pages),
                (readingroom, "ROOMS_DIR", Path(self.tmp.name) / "rooms")):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_missing_dir_is_empty(self):
        self.assertEqual(reading.list_pages(), [])

    def test_pages_listed_with_titles(self):
        self.pages.mkdir(parents=True)
        (self.pages / "b-list.html").write_text(
            "<html><head><title>  Second\n Queue </title></head></html>")
        (self.pages / "a-list.html").write_text("<html>no title here</html>")
        (self.pages / "notes.txt").write_text("ignored")
        out = reading.list_pages()
        self.assertEqual(
            out,
            [{"name": "a-list", "title": "a-list",
              "url": "/reading/a-list.html", "native": False},
             {"name": "b-list", "title": "Second Queue",
              "url": "/reading/b-list.html", "native": False}])


if __name__ == "__main__":
    unittest.main()
