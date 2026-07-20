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
        self.tmp = tempfile.TemporaryDirectory()
        self.pages = Path(self.tmp.name) / "reading"
        patcher = mock.patch.object(reading, "PAGES_DIR", self.pages)
        patcher.start()
        self.addCleanup(patcher.stop)
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
            [{"name": "a-list", "title": "a-list", "url": "/reading/a-list.html"},
             {"name": "b-list", "title": "Second Queue", "url": "/reading/b-list.html"}])


if __name__ == "__main__":
    unittest.main()
