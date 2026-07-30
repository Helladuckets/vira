"""Reading-room -> vault projection."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import readingroom, roomvault, vault


def item(**kw):
    base = {
        "title": "A Talk About Things", "url": "https://example.com/talk",
        "date": "2025-01-02", "year": "2025", "mode": "watch",
        "status": "MISSING", "prio": "P1", "people": ["Ada Lovelace"],
        "type": "lecture", "venue": "Somewhere", "note": "What it is.",
        "why": "Why it matters.", "vault": "", "pay": False,
    }
    base.update(kw)
    base["id"] = kw.get("id") or readingroom.item_id(base)
    return base


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        (self.vault / "wiki").mkdir(parents=True)
        self.rooms = self.tmp / "rooms"
        self.rooms.mkdir()
        self.p_rooms = mock.patch.object(readingroom, "ROOMS_DIR", self.rooms)
        self.p_vault = mock.patch.object(vault, "vault_root",
                                         lambda: self.vault)
        self.p_rooms.start()
        self.p_vault.start()
        self.addCleanup(self.p_rooms.stop)
        self.addCleanup(self.p_vault.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def room(self, items, slug="demo", definition=None):
        doc = {"slug": slug, "title": "Demo Room", "subtitle": "A subtitle.",
               "built": "2026-07-01", "updated": "2026-07-01T00:00:00-04:00",
               "legacy_key": "", "definition": definition or {},
               "items": items}
        (self.rooms / f"{slug}.md").unlink(missing_ok=True)
        (self.rooms / f"{slug}.json").write_text(json.dumps(doc),
                                                 encoding="utf-8")
        return doc

    def note(self, rel):
        return (self.vault / rel).read_text(encoding="utf-8")


class IngestTests(Base):
    def test_creates_a_pointer_note_and_a_hub(self):
        self.room([item()])
        res = roomvault.ingest("demo")
        self.assertEqual(res["created"], 1)
        self.assertTrue((self.vault / "wiki" / "demo-reading-room.md").exists())
        n = self.note("wiki/rooms/a-talk-about-things.md")
        self.assertIn("type: reading-room-item", n)
        self.assertIn("room_slug: demo", n)
        self.assertIn("What it is.", n)
        self.assertIn("Why it matters.", n)
        self.assertIn("https://example.com/talk", n)

    def test_pointer_is_not_a_source_summary(self):
        # The type asserts a raw was read and synthesized. These are not.
        self.room([item()])
        roomvault.ingest("demo")
        self.assertNotIn("source-summary",
                         self.note("wiki/rooms/a-talk-about-things.md"))

    def test_rerun_is_idempotent_and_writes_nothing(self):
        self.room([item()])
        roomvault.ingest("demo")
        p = self.vault / "wiki" / "rooms" / "a-talk-about-things.md"
        before = p.read_text(encoding="utf-8")
        mtime = p.stat().st_mtime
        res = roomvault.ingest("demo")
        self.assertEqual((res["created"], res["updated"]), (0, 0))
        self.assertEqual(res["unchanged"], 1)
        self.assertEqual(p.read_text(encoding="utf-8"), before)
        self.assertEqual(p.stat().st_mtime, mtime)

    def test_retitled_item_updates_its_own_note(self):
        # The id is the URL, so a retitled item must keep its note rather
        # than mint a second node in the graph for one thing.
        it = item()
        self.room([it])
        roomvault.ingest("demo")
        self.room([item(title="A Talk About Things (Revised)", id=it["id"])])
        res = roomvault.ingest("demo")
        self.assertEqual((res["created"], res["updated"]), (0, 1))
        notes = sorted(p.name for p in (self.vault / "wiki" / "rooms").glob("*.md"))
        self.assertEqual(notes, ["a-talk-about-things.md"])
        self.assertIn("(Revised)", self.note("wiki/rooms/a-talk-about-things.md"))

    def test_created_date_survives_an_update(self):
        self.room([item()])
        roomvault.ingest("demo")
        p = self.vault / "wiki" / "rooms" / "a-talk-about-things.md"
        p.write_text(p.read_text(encoding="utf-8")
                     .replace(f"created: {roomvault.date.today().isoformat()}",
                              "created: 2020-01-01"), encoding="utf-8")
        self.room([item(note="Changed.")])
        roomvault.ingest("demo")
        self.assertIn("created: 2020-01-01", p.read_text(encoding="utf-8"))

    def test_consumed_item_links_the_real_note_and_mints_none(self):
        (self.vault / "wiki" / "machines-of-loving-grace.md").write_text(
            "# real note\n", encoding="utf-8")
        self.room([item(title="Machines of Loving Grace", status="HAVE",
                        vault="wiki/machines-of-loving-grace.md")])
        res = roomvault.ingest("demo")
        self.assertEqual((res["created"], res["linked_existing"]), (0, 1))
        self.assertFalse((self.vault / "wiki" / "rooms").exists())
        self.assertIn("[[machines-of-loving-grace]]",
                      self.note("wiki/demo-reading-room.md"))

    def test_a_stale_vault_ref_falls_back_to_a_pointer(self):
        # A link that does not resolve reads as consumed and leads nowhere.
        self.room([item(status="HAVE", vault="wiki/deleted-note.md")])
        res = roomvault.ingest("demo")
        self.assertEqual((res["created"], res["linked_existing"]), (1, 0))

    def test_slug_never_shadows_an_existing_note(self):
        # Obsidian resolves [[link]] by filename across directories, so a
        # colliding stem would silently re-point live links.
        (self.vault / "wiki" / "a-talk-about-things.md").write_text(
            "# unrelated\n", encoding="utf-8")
        self.room([item()])
        roomvault.ingest("demo")
        made = sorted(p.stem for p in (self.vault / "wiki" / "rooms").glob("*.md"))
        self.assertEqual(made, ["a-talk-about-things-lecture"])
        self.assertEqual(self.note("wiki/a-talk-about-things.md"), "# unrelated\n")

    def test_two_items_with_one_title_get_distinct_notes(self):
        self.room([item(url="https://example.com/one"),
                   item(url="https://example.com/two")])
        res = roomvault.ingest("demo")
        self.assertEqual(res["created"], 2)
        self.assertEqual(len(list((self.vault / "wiki" / "rooms").glob("*.md"))), 2)

    def test_people_link_only_when_the_page_exists(self):
        (self.vault / "wiki" / "ada-lovelace.md").write_text("x", encoding="utf-8")
        self.room([item(people=["Ada Lovelace", "Nobody Here"])])
        roomvault.ingest("demo")
        n = self.note("wiki/rooms/a-talk-about-things.md")
        self.assertIn("[[ada-lovelace]]", n)
        self.assertIn("Nobody Here", n)
        self.assertNotIn("[[nobody-here]]", n)

    def test_hub_carries_every_item_and_the_definition(self):
        self.room([item(prio="P1"), item(url="https://e.com/2", prio="P3",
                                         title="Second Thing")],
                  definition={"subject": "S", "why": "W", "notes": "M"})
        roomvault.ingest("demo")
        h = self.note("wiki/demo-reading-room.md")
        self.assertIn("type: reference", h)
        self.assertIn("### P1 — 1 items", h)
        self.assertIn("### P3 — 1 items", h)
        self.assertIn("**Subject.** S", h)
        self.assertIn("**Method.** M", h)
        # source_count is len(sources:) for a non-source-summary page; the
        # catalog size rides its own field.
        self.assertIn("source_count: 0", h)
        self.assertIn("items: 2", h)

    def test_hub_names_recurring_people_with_no_page(self):
        # Linking them would be 165 ghost nodes; naming the gap is the
        # to-do, and a page created later gets linked on the next run.
        items = [item(url=f"https://e.com/{i}", title=f"Thing {i}",
                      people=["Chris Olah"]) for i in range(3)]
        self.room(items)
        roomvault.ingest("demo")
        h = self.note("wiki/demo-reading-room.md")
        self.assertIn("Chris Olah — 3 items", h)
        self.assertNotIn("[[chris-olah]]", h)

    def test_a_one_off_name_is_not_listed_as_a_gap(self):
        self.room([item(people=["Passing Mention"])])
        roomvault.ingest("demo")
        self.assertNotIn("Passing Mention —",
                         self.note("wiki/demo-reading-room.md"))

    def test_a_person_with_a_page_is_not_a_gap(self):
        (self.vault / "wiki" / "chris-olah.md").write_text("x", encoding="utf-8")
        items = [item(url=f"https://e.com/{i}", title=f"Thing {i}",
                      people=["Chris Olah"]) for i in range(3)]
        self.room(items)
        roomvault.ingest("demo")
        h = self.note("wiki/demo-reading-room.md")
        self.assertNotIn("Recurring people with no page yet", h)

    def test_dry_run_writes_nothing(self):
        self.room([item()])
        res = roomvault.ingest("demo", dry_run=True)
        self.assertEqual(res["created"], 1)
        self.assertFalse((self.vault / "wiki" / "rooms").exists())
        self.assertFalse((self.vault / "wiki" / "demo-reading-room.md").exists())

    def test_an_item_that_left_the_room_is_reported_not_deleted(self):
        self.room([item(), item(url="https://e.com/gone", title="Gone Thing")])
        roomvault.ingest("demo")
        self.room([item()])
        res = roomvault.ingest("demo")
        self.assertEqual(res["orphans"], ["gone-thing.md"])
        self.assertTrue((self.vault / "wiki" / "rooms" / "gone-thing.md").exists())

    def test_frontmatter_quotes_titles_and_links(self):
        self.room([item(title="Talk: A Thing", venue="Show: Live")])
        roomvault.ingest("demo")
        n = self.note("wiki/rooms/talk-a-thing.md")
        self.assertIn('title: "Talk: A Thing"', n)
        self.assertIn('venue: "Show: Live"', n)
        self.assertIn('room: "[[demo-reading-room]]"', n)

    def test_paywalled_is_carried(self):
        self.room([item(pay=True)])
        roomvault.ingest("demo")
        self.assertIn("paywalled: true",
                      self.note("wiki/rooms/a-talk-about-things.md"))


class RefusalTests(Base):
    def test_passive_refuses(self):
        self.room([item()])
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(roomvault.IngestError):
                roomvault.ingest("demo")
        self.assertFalse((self.vault / "wiki" / "rooms").exists())

    def test_unknown_room_raises(self):
        with self.assertRaises(roomvault.IngestError):
            roomvault.ingest("nope")

    def test_missing_vault_root_raises(self):
        self.room([item()])
        with mock.patch.object(vault, "vault_root",
                               lambda: self.tmp / "nowhere"):
            with self.assertRaises(roomvault.IngestError):
                roomvault.ingest("demo")


class StoreWriteIsolationTests(Base):
    """build() is a pure store write. Hanging the vault projection off it
    made every caller that never heard of a vault write to the owner's
    real Obsidian vault — 11 fixture rooms landed there on 2026-07-29,
    from tests. The projection belongs to the entry points."""

    def test_build_never_touches_the_vault(self):
        readingroom.build("demo", "Demo Room", "Sub", [item()])
        self.assertFalse((self.vault / "wiki" / "demo-reading-room.md").exists())
        self.assertFalse((self.vault / "wiki" / "rooms").exists())

    def test_build_writes_the_store(self):
        res = readingroom.build("demo", "Demo Room", "Sub", [item()])
        self.assertEqual(res["items"], 1)
        self.assertIsNotNone(readingroom.load_room("demo"))

    def test_no_unpatched_vault_root_reaches_a_real_home(self):
        # The shape of the original bug: a default vault_root pointing at a
        # real directory. Nothing in the store path may consult it.
        with mock.patch.object(vault, "vault_root",
                               side_effect=AssertionError(
                                   "build() consulted vault_root")):
            readingroom.build("demo", "Demo Room", "Sub", [item()])


class SyncTests(Base):
    def test_sync_projects(self):
        self.room([item()])
        res = roomvault.sync("demo")
        self.assertEqual(res["created"], 1)
        self.assertTrue((self.vault / "wiki" / "demo-reading-room.md").exists())

    def test_sync_swallows_failure(self):
        self.room([item()])
        with mock.patch.object(roomvault, "ingest",
                               side_effect=RuntimeError("boom")):
            self.assertIsNone(roomvault.sync("demo"))

    def test_sync_is_none_when_the_vault_is_unset(self):
        self.room([item()])
        with mock.patch.object(vault, "vault_root",
                               lambda: self.tmp / "nowhere"):
            self.assertIsNone(roomvault.sync("demo"))


if __name__ == "__main__":
    unittest.main()
