"""Reading-room -> vault projection (hub-only since the 2026-08-05 full
ingest — pointer notes are no longer minted; see test_fullingest.py for
the staging/reconcile half)."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import fullingest, readingroom, roomvault, vault


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
        fullingest._summaries_cache.clear()
        roomvault._notes_cache.clear()

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

    def summary(self, stem, iid):
        (self.vault / "wiki" / f"{stem}.md").write_text(
            "---\ntitle: \"S\"\ntype: source-summary\n"
            f"room_item_id: {iid}\n---\n\n# S\n", encoding="utf-8")

    def pointer(self, stem, iid):
        d = self.vault / roomvault.ROOMS_SUBDIR
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{stem}.md").write_text(
            "---\ntitle: \"P\"\ntype: reading-room-item\n"
            f"room_item_id: {iid}\n---\n\n# P\n", encoding="utf-8")


class IngestTests(Base):
    def test_writes_the_hub_and_mints_no_pointer_notes(self):
        self.room([item()])
        res = roomvault.ingest("demo")
        self.assertTrue((self.vault / "wiki" / "demo-reading-room.md").exists())
        self.assertFalse((self.vault / "wiki" / "rooms").exists())
        self.assertEqual((res["linked"], res["pending"]), (0, 1))

    def test_consumed_item_links_the_real_note(self):
        (self.vault / "wiki" / "machines-of-loving-grace.md").write_text(
            "# real note\n", encoding="utf-8")
        self.room([item(title="Machines of Loving Grace", status="HAVE",
                        vault="wiki/machines-of-loving-grace.md")])
        res = roomvault.ingest("demo")
        self.assertEqual(res["linked"], 1)
        self.assertIn("[[machines-of-loving-grace]]",
                      self.note("wiki/demo-reading-room.md"))

    def test_a_synthesized_summary_links_before_reconcile(self):
        it = item()
        self.summary("a-talk-summary", it["id"])
        self.room([it])
        res = roomvault.ingest("demo")
        self.assertEqual(res["linked"], 1)
        self.assertIn("[[a-talk-summary]]",
                      self.note("wiki/demo-reading-room.md"))

    def test_a_legacy_pointer_still_links_until_retired(self):
        it = item()
        self.pointer("a-talk-pointer", it["id"])
        self.room([it])
        res = roomvault.ingest("demo")
        self.assertEqual(res["linked"], 1)
        self.assertIn("[[a-talk-pointer]]",
                      self.note("wiki/demo-reading-room.md"))

    def test_the_summary_outranks_the_legacy_pointer(self):
        it = item()
        self.pointer("a-talk-pointer", it["id"])
        self.summary("a-talk-summary", it["id"])
        self.room([it])
        roomvault.ingest("demo")
        h = self.note("wiki/demo-reading-room.md")
        self.assertIn("[[a-talk-summary]]", h)
        self.assertNotIn("[[a-talk-pointer]]", h)

    def test_a_stale_vault_ref_reads_as_pending(self):
        # A link that does not resolve reads as consumed and leads nowhere.
        self.room([item(status="HAVE", vault="wiki/deleted-note.md")])
        res = roomvault.ingest("demo")
        self.assertEqual((res["linked"], res["pending"]), (0, 1))

    def test_rerun_is_idempotent_and_writes_nothing(self):
        self.room([item()])
        roomvault.ingest("demo")
        p = self.vault / "wiki" / "demo-reading-room.md"
        before = p.read_text(encoding="utf-8")
        mtime = p.stat().st_mtime
        res = roomvault.ingest("demo")
        self.assertFalse(res["hub_changed"])
        self.assertEqual(p.read_text(encoding="utf-8"), before)
        self.assertEqual(p.stat().st_mtime, mtime)

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
        items = [item(url=f"https://e.com/{i}", title=f"Thing {i}",
                      people=["Chris Olah"]) for i in range(3)]
        self.room(items)
        roomvault.ingest("demo")
        h = self.note("wiki/demo-reading-room.md")
        self.assertIn("Chris Olah — 3 items", h)
        self.assertNotIn("[[chris-olah]]", h)

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
        self.assertEqual(res["pending"], 1)
        self.assertFalse((self.vault / "wiki" / "demo-reading-room.md").exists())

    def test_a_legacy_pointer_whose_item_left_is_reported_not_deleted(self):
        gone = item(url="https://e.com/gone", title="Gone Thing")
        self.pointer("gone-thing", gone["id"])
        self.room([item()])
        res = roomvault.ingest("demo")
        self.assertEqual(res["orphans"], ["gone-thing.md"])
        self.assertTrue((self.vault / "wiki" / "rooms" / "gone-thing.md").exists())


class RefusalTests(Base):
    def test_passive_refuses(self):
        self.room([item()])
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(roomvault.IngestError):
                roomvault.ingest("demo")
        self.assertFalse((self.vault / "wiki" / "demo-reading-room.md").exists())

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
        with mock.patch.object(vault, "vault_root",
                               side_effect=AssertionError(
                                   "build() consulted vault_root")):
            readingroom.build("demo", "Demo Room", "Sub", [item()])


class SyncTests(Base):
    def test_sync_projects_and_kicks_the_full_ingest(self):
        self.room([item()])
        with mock.patch.object(fullingest, "sync") as kick:
            res = roomvault.sync("demo")
        self.assertEqual(res["pending"], 1)
        self.assertTrue((self.vault / "wiki" / "demo-reading-room.md").exists())
        kick.assert_called_once_with("demo")

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
