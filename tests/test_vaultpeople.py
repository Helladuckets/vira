"""The vault's person pages as a typeahead index — grounding for a
reading room's people pills.

Everything is derived from a tmp fixture vault (the readinglist
isolation rule: patching a write store does nothing about what a
function READS, so the root itself is pointed at the fixture).

Run: .venv/bin/python -m unittest tests.test_vaultpeople
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import vaultpeople

PERSON = """---
title: "Cat Wu"
type: person
tags:
  - cat-wu
created: 2026-06-09
updated: 2026-06-09
---

# Cat Wu

Head of Product for [[claude-code|Claude Code]] at [[anthropic]].
"""

PERSON2 = """---
title: "Dianne Penn"
type: person
---

# Dianne Penn

Product lead at Anthropic.
"""

NOT_PERSON = """---
title: "Claude Code"
type: concept
---

A concept page — must never appear in the people index.
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wiki = self.root / "wiki"
        self.wiki.mkdir()
        (self.wiki / "cat-wu.md").write_text(PERSON, encoding="utf-8")
        (self.wiki / "dianne-penn.md").write_text(PERSON2, encoding="utf-8")
        (self.wiki / "claude-code.md").write_text(NOT_PERSON,
                                                 encoding="utf-8")
        vaultpeople._cache.clear()
        self.addCleanup(vaultpeople._cache.clear)
        self.addCleanup(self.tmp.cleanup)


class IndexTest(Base):
    def test_only_person_pages_index_with_ref_and_qualifier(self):
        idx = vaultpeople.people_index(self.root)
        names = {e["name"] for e in idx}
        self.assertEqual(names, {"Cat Wu", "Dianne Penn"})
        cat = next(e for e in idx if e["name"] == "Cat Wu")
        self.assertEqual(cat["ref"], "wiki/cat-wu.md")
        # Wikilinks are stripped from the qualifier — it is display text.
        self.assertEqual(cat["qualifier"],
                         "Head of Product for Claude Code at anthropic.")

    def test_missing_vault_is_dormant_not_an_error(self):
        self.assertEqual(vaultpeople.people_index(self.root / "nope"), [])

    def test_cache_invalidates_when_the_wiki_changes(self):
        vaultpeople.people_index(self.root)
        p = self.wiki / "new-person.md"
        p.write_text(PERSON2.replace("Dianne Penn", "New Person"),
                     encoding="utf-8")
        os.utime(self.wiki, (0, 9999999999))       # force a new dir mtime
        names = {e["name"] for e in vaultpeople.people_index(self.root)}
        self.assertIn("New Person", names)


class SearchTest(Base):
    def test_word_prefix_outranks_substring(self):
        # "wu" prefixes Cat Wu's surname; a substring-only match would tie.
        hits = vaultpeople.search("wu", root=self.root)
        self.assertEqual(hits[0]["name"], "Cat Wu")

    def test_names_only_never_page_bodies(self):
        # "product" appears in both bodies but neither name.
        self.assertEqual(vaultpeople.search("product", root=self.root), [])

    def test_empty_query_is_empty(self):
        self.assertEqual(vaultpeople.search("", root=self.root), [])


class StubTest(Base):
    def test_creates_a_person_page_and_the_index_sees_it(self):
        made = vaultpeople.create_stub("Jan Leike", "Alignment lead",
                                       root=self.root)
        self.assertEqual(made["ref"], "wiki/jan-leike.md")
        text = (self.wiki / "jan-leike.md").read_text(encoding="utf-8")
        self.assertIn("type: person", text)
        self.assertIn("Alignment lead", text)
        os.utime(self.wiki, (0, 9999999999))
        self.assertTrue(vaultpeople.search("jan", root=self.root))

    def test_refuses_an_existing_page(self):
        with self.assertRaises(FileExistsError):
            vaultpeople.create_stub("Cat Wu", root=self.root)

    def test_refuses_under_passive(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(PermissionError):
                vaultpeople.create_stub("Someone New", root=self.root)

    def test_refuses_with_no_wiki_and_no_name(self):
        with self.assertRaises(FileNotFoundError):
            vaultpeople.create_stub("X Y", root=self.root / "nope")
        with self.assertRaises(ValueError):
            vaultpeople.create_stub("   ", root=self.root)


class RouteTest(Base):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from server import main
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    def test_search_route(self):
        with mock.patch.object(vaultpeople, "search",
                               return_value=[{"name": "Cat Wu"}]):
            r = self.client.get("/api/vault/people?q=cat")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["people"][0]["name"], "Cat Wu")

    def test_create_route_maps_the_refusals(self):
        for exc, code in ((PermissionError("passive"), 403),
                          (FileExistsError("exists"), 409),
                          (ValueError("bad"), 400)):
            with mock.patch.object(vaultpeople, "create_stub",
                                   side_effect=exc):
                r = self.client.post("/api/vault/people",
                                     json={"name": "X"})
            self.assertEqual(r.status_code, code)


if __name__ == "__main__":
    unittest.main()
