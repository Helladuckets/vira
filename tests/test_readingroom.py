"""Reading-room updates: the prompt that turns a built room into a live
tracker. The dispatch itself is a normal session launch (main.py); what is
tested here is the composed contract — carry every item forward, research
since the built date, rebuild the SAME slug.

Run: .venv/bin/python -m unittest tests.test_readingroom
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import reading, readingroom


class UpdatePromptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        pages = base / "pages"
        pages.mkdir()
        for target, attr, val in ((reading, "PAGES_DIR", pages),
                                  (reading, "STORE_DIR", base / "data"),
                                  (readingroom, "PAGES_DIR", pages),
                                  (readingroom, "ROOT", base)):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def build(self):
        return readingroom.build(
            "test-room", "Test Room", "Everything on one subject.",
            [{"title": "A talk", "url": "https://example.com/a"},
             {"title": "A paper", "url": "https://example.com/b"}])

    def test_prompt_names_the_room_and_the_contract(self):
        self.build()
        p = readingroom.update_prompt("test-room")
        self.assertIn('"test-room"', p)
        self.assertIn("Test Room", p)
        self.assertIn("Everything on one subject.", p)
        self.assertIn("2 items", p)
        # The three load-bearing instructions: carry forward, real URLs only,
        # rebuild the same slug through the validated tool.
        self.assertIn("EVERY existing item must carry forward", p)
        self.assertIn("Real URLs only", p)
        self.assertIn("mcp__vira__create_reading_room", p)

    def test_prompt_carries_the_built_date(self):
        self.build()
        p = readingroom.update_prompt("test-room")
        from datetime import date
        self.assertIn(date.today().isoformat(), p)

    def test_unknown_room_raises_keyerror(self):
        with self.assertRaises(KeyError):
            readingroom.update_prompt("no-such-room")
