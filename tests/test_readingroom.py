"""Reading-room updates and migration — the store-native contracts.

update_prompt turns a built room into a live tracker (carry every item
forward, research since the last refresh, rebuild the SAME slug);
refresh_all_prompt is the weekly room-scout's composition; migrate folds a
legacy page-based room into the store WITHOUT orphaning done-marks (the
old pages carry a different id scheme — measured on the real Anthropic
room: 345/345 ids differ).

Run: .venv/bin/python -m unittest tests.test_readingroom
"""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from server import reading, readingroom


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.pages = base / "pages"
        self.pages.mkdir()
        for target, attr, val in (
                (reading, "PAGES_DIR", self.pages),
                (reading, "STORE_DIR", base / "data" / "reading"),
                (readingroom, "PAGES_DIR", self.pages),
                (readingroom, "ROOMS_DIR", base / "data" / "reading" / "rooms"),
                (readingroom, "ROOT", base)):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def build(self, slug="test-room"):
        return readingroom.build(
            slug, "Test Room", "Everything on one subject.",
            [{"title": "A talk", "url": "https://example.com/a"},
             {"title": "A paper", "url": "https://example.com/b"}])


class UpdatePromptTest(Base):
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

    def test_prompt_points_at_the_store_and_the_refresh_date(self):
        self.build()
        p = readingroom.update_prompt("test-room")
        self.assertIn("rooms", p)                       # the store path
        self.assertIn(date.today().isoformat(), p)

    def test_unknown_room_raises_keyerror(self):
        with self.assertRaises(KeyError):
            readingroom.update_prompt("no-such-room")


class DefinitionTest(Base):
    DEF = {"subject": "Anthropic - the company and its people",
           "why": "Articulate why it changed the arc.",
           "people": "Dario Amodei, Boris Cherny",
           "modes": ["watch", "read"], "depth": "exhaustive",
           "notes": "Verify every URL. Keep YouTube query strings."}

    def test_set_and_survive_a_rebuild(self):
        self.build()
        readingroom.set_definition("test-room", self.DEF)
        self.assertEqual(readingroom.load_room("test-room")["definition"]
                         ["subject"], self.DEF["subject"])
        self.build()                       # a refresh rebuild
        self.assertEqual(readingroom.load_room("test-room")["definition"],
                         readingroom.clean_definition(self.DEF))

    def test_bad_payloads_are_refused(self):
        self.build()
        for bad in ({"modes": ["skim"]}, {"depth": "bottomless"},
                    {"modes": "not-a-mode"}):
            with self.assertRaises(readingroom.BuildError):
                readingroom.set_definition("test-room", {**self.DEF, **bad})
        with self.assertRaises(KeyError):
            readingroom.set_definition("nowhere", self.DEF)

    def test_update_prompt_carries_the_standing_instructions(self):
        self.build()
        readingroom.set_definition("test-room", self.DEF)
        p = readingroom.update_prompt("test-room")
        self.assertIn("STANDING INSTRUCTIONS", p)
        self.assertIn("Keep YouTube query strings.", p)
        self.assertIn("Dario Amodei", p)

    def test_no_definition_means_no_instruction_block(self):
        self.build()
        self.assertNotIn("STANDING INSTRUCTIONS",
                         readingroom.update_prompt("test-room"))


class MetaTest(Base):
    def test_retitle_flows_and_orphans_nothing(self):
        self.build()
        reading.set_done("test-room", "someid", True)
        out = readingroom.set_meta("test-room", title="Anthropic")
        self.assertEqual(out["title"], "Anthropic")
        # Subtitle untouched, marks untouched, list shows the new name.
        self.assertEqual(out["subtitle"], "Everything on one subject.")
        self.assertEqual(reading.get_done("test-room"), ["someid"])
        self.assertEqual(reading.list_pages()[0]["title"], "Anthropic")
        self.assertIn("<title>Anthropic</title>",
                      readingroom.export_html("test-room"))

    def test_none_keeps_and_empty_title_is_refused(self):
        self.build()
        readingroom.set_meta("test-room", subtitle="A new line.")
        room = readingroom.load_room("test-room")
        self.assertEqual(room["title"], "Test Room")
        self.assertEqual(room["subtitle"], "A new line.")
        with self.assertRaises(readingroom.BuildError):
            readingroom.set_meta("test-room", title="   ")
        with self.assertRaises(KeyError):
            readingroom.set_meta("nowhere", title="X")

    def test_rebuild_after_retitle_keeps_the_new_name(self):
        """A refresh session passes the CURRENT title back through the tool;
        set_meta's write must survive an unrelated definition save too."""
        self.build()
        readingroom.set_meta("test-room", title="Anthropic")
        readingroom.set_definition("test-room", {"subject": "s"})
        self.assertEqual(readingroom.load_room("test-room")["title"],
                         "Anthropic")


class RefreshAllTest(Base):
    def test_no_rooms_is_an_empty_prompt_not_an_error(self):
        self.assertEqual(readingroom.refresh_all_prompt(), "")

    def test_every_room_rides_the_sweep(self):
        self.build("alpha")
        self.build("beta")
        p = readingroom.refresh_all_prompt()
        self.assertIn('"alpha"', p)
        self.assertIn('"beta"', p)
        self.assertIn("2 rooms", p)


LEGACY_PAGE = """<!DOCTYPE html><html><head><title>Old Room</title></head>
<body><header><h1>Old Room</h1><p>The universe, ranked. Built 2026-07-17.</p></header>
<script>
const DATA = [{"id": "oldid00001", "title": "A talk", "url": "https://example.com/a"},
 {"id": "oldid00002", "title": "A paper", "url": "https://example.com/b"}];
const API = "/api/reading/old-room/done";
const LS_KEY = "old-room-done";
</script></body></html>"""


class MigrateTest(Base):
    def page(self, slug="old-room", text=LEGACY_PAGE):
        (self.pages / f"{slug}.html").write_text(text, encoding="utf-8")

    def test_migration_moves_the_room_and_remaps_marks(self):
        self.page()
        reading.set_done("old-room", "oldid00001", True)   # earned on the page
        res = readingroom.migrate("old-room")
        room = readingroom.load_room("old-room")
        self.assertEqual(room["title"], "Old Room")
        self.assertEqual(len(room["items"]), 2)
        self.assertEqual(room["legacy_key"], "old-room-done")
        self.assertEqual(res["remapped_marks"], 1)
        # The mark now lives under the generator id of the SAME item.
        new_id = readingroom.item_id({"title": "A talk",
                                      "url": "https://example.com/a"})
        self.assertIn(new_id, reading.get_done("old-room"))
        # The page is kept as a backup, invisible to the .html glob.
        self.assertFalse((self.pages / "old-room.html").exists())
        self.assertTrue((self.pages / "old-room.html.migrated").exists())

    def test_migrating_a_native_room_is_refused(self):
        self.build("test-room")
        with self.assertRaises(readingroom.BuildError):
            readingroom.migrate("test-room")

    def test_migrating_a_missing_page_raises_keyerror(self):
        with self.assertRaises(KeyError):
            readingroom.migrate("nowhere")

    def test_a_page_with_no_data_is_refused_not_half_migrated(self):
        self.page("odd", "<html><head><title>Odd</title></head></html>")
        with self.assertRaises(readingroom.BuildError):
            readingroom.migrate("odd")
        self.assertIsNone(readingroom.load_room("odd"))
        self.assertTrue((self.pages / "odd.html").exists())


class ListPagesUnionTest(Base):
    def test_native_rooms_and_legacy_pages_union_native_wins(self):
        self.build("native-room")
        (self.pages / "legacy.html").write_text(
            "<html><head><title>Legacy</title></head></html>",
            encoding="utf-8")
        # A page whose slug matches a native room is shadowed, not listed twice.
        (self.pages / "native-room.html").write_text(
            "<html><head><title>Stale copy</title></head></html>",
            encoding="utf-8")
        rows = reading.list_pages()
        by = {r["name"]: r for r in rows}
        self.assertEqual(len(rows), 2)
        self.assertTrue(by["native-room"]["native"])
        self.assertEqual(by["native-room"]["title"], "Test Room")
        self.assertFalse(by["legacy"]["native"])

    def test_native_details_come_from_the_store_not_a_parser(self):
        self.build("native-room")
        reading.set_done("native-room", readingroom.item_id(
            {"title": "A talk", "url": "https://example.com/a"}), True)
        row = next(r for r in reading.page_details()
                   if r["name"] == "native-room")
        self.assertEqual(row["items"], 2)
        self.assertEqual(row["done"], 1)
        self.assertEqual(row["subtitle"], "Everything on one subject.")
        self.assertEqual(row["built"], date.today().isoformat())
