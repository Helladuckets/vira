"""The module story: right-click a window, "What is this?".

What is worth pinning, because each is a decision:
  1. The corpus is the LIBRARY — read and unread alike. A module's build
     story is mostly documents already marked read, which queue() excludes.
  2. The window id itself always counts as a tag, so a vocabulary that
     converges on the id needs no table edit.
  3. Companions alias to their host; ids with no row answer None (the route
     404s) rather than inventing an empty story for chrome like the palette.
  4. Untagged documents are invisible to a story but COUNTED (`pending`), so
     a thin story reads as still-being-tagged, never as "this is all".
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import doctags, modulemap, modulestory, readinglist, settings


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.films = root / "walkthroughs"
        self.films.mkdir()
        (root / "static").mkdir()
        patches = (
            mock.patch.object(readinglist, "STORE", root / "reading-list.json"),
            mock.patch.object(readinglist, "ROOT", root),
            mock.patch.object(readinglist, "WALKTHROUGH_DIR", self.films),
            mock.patch.object(doctags, "STORE", root / "doc-index.json"),
            mock.patch.object(modulemap, "STORE", root / "modules.json"),
            mock.patch.object(settings, "get", side_effect=lambda k: ""),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def reg(self, title, kind="plan", locator=None, tags=None, read=False):
        locator = locator or "/docs/" + title.replace(" ", "-") + ".html"
        rid = readinglist.register(title, kind, locator)["id"]
        if read:
            readinglist.complete(rid, True)
        if tags is not None:
            s = {"entries": {}, "last_pass": ""}
            if doctags.STORE.exists():
                s = json.loads(doctags.STORE.read_text(encoding="utf-8"))
            s["entries"][rid] = {"tags": {"module": tags, "subproject": [],
                                          "theme": [], "concept": []},
                                 "tagged": True}
            doctags.STORE.write_text(json.dumps(s), encoding="utf-8")
        return rid


class TableTests(Base):
    def test_every_row_is_an_alias_or_carries_tags_and_a_map_id(self):
        for wid, row in modulestory.WINDOWS.items():
            if row.get("alias"):
                self.assertIn(row["alias"], modulestory.WINDOWS, wid)
                self.assertIsNone(modulestory.WINDOWS[row["alias"]].get("alias"),
                                  "an alias must resolve in one hop")
            else:
                self.assertTrue(row.get("tags"), wid)
                self.assertTrue(row.get("map"), wid)

    def test_companions_resolve_to_find(self):
        wid, row = modulestory.resolve("find-cloud")
        self.assertEqual(wid, "find")
        self.assertTrue(row)

    def test_chrome_has_no_story(self):
        self.assertIsNone(modulestory.story("launchpad"))
        self.assertIsNone(modulestory.story("no-such-window"))


class StoryTests(Base):
    def test_filters_on_the_module_axis(self):
        self.reg("Brief plan", tags=["brief"])
        self.reg("Atlas plan", tags=["atlas"])
        s = modulestory.story("brief")
        self.assertEqual([d["title"] for d in s["docs"]], ["Brief plan"])
        self.assertEqual(s["counts"], {"plan": 1})

    def test_read_documents_are_the_story_too(self):
        self.reg("Old brief plan", tags=["brief"], read=True)
        s = modulestory.story("brief")
        self.assertEqual(len(s["docs"]), 1)
        self.assertTrue(s["docs"][0]["read"])

    def test_the_window_id_itself_counts_as_a_tag(self):
        # "feed" is not in its own tags list; the implicit {win_id} is what
        # keeps a vocabulary that converges on the id working with no edit.
        self.assertNotIn("feed", modulestory.WINDOWS["feed"]["tags"])
        self.reg("Feed retro", kind="retro", tags=["feed"])
        s = modulestory.story("feed")
        self.assertEqual(len(s["docs"]), 1)

    def test_untagged_documents_are_pending_never_invisible_silently(self):
        self.reg("Tagged", tags=["brief"])
        self.reg("Untagged", tags=None)
        s = modulestory.story("brief")
        self.assertEqual(len(s["docs"]), 1)
        self.assertEqual(s["pending"], 1)

    def test_registry_blurb_joins_when_present(self):
        modulemap.STORE.write_text(json.dumps({"modules": [
            {"id": "brief-win", "name": "Daily Brief",
             "what": "The morning answer."}]}), encoding="utf-8")
        s = modulestory.story("brief")
        self.assertEqual(s["title"], "Daily Brief")
        self.assertEqual(s["what"], "The morning answer.")

    def test_a_fresh_install_answers_from_the_seeded_registry(self):
        # No modules.json on disk: modulemap seeds DEFAULT_MODULES, so the
        # story still opens with a real description rather than a blank.
        self.reg("Brief plan", tags=["brief"])
        s = modulestory.story("brief")
        self.assertTrue(s["what"])
        self.assertEqual(len(s["docs"]), 1)

    def test_film_metadata_joins_on_the_locator(self):
        slug = "vira-2026-08-01-brief"
        d = self.films / slug
        d.mkdir()
        (d / "index.html").write_text("<title>Brief film</title>",
                                      encoding="utf-8")
        (d / "thumb.jpg").write_bytes(b"x")
        self.reg("Brief film", kind="walkthrough",
                 locator=f"/walkthroughs/{slug}/", tags=["brief"])
        s = modulestory.story("brief")
        self.assertEqual(s["docs"][0]["film"]["thumb"],
                         f"/walkthroughs/{slug}/thumb.jpg")
