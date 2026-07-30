"""Which vault note belongs to a reading-room item.

The bug these pin: `vault` on an item means the OWNER'S SUMMARY, and
roomvault mints a pointer note precisely when that field is EMPTY — so
reading `vault` as "does a note exist" reports the opposite of the truth
for every item the ingest has ever catalogued (263 of 348 cards on
2026-07-30, each claiming "No vault note yet" over a note in the vault).
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import roomvault


def _note(root, name, item_id):
    p = root / roomvault.ROOMS_SUBDIR / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntitle: {name}\nroom_item_id: {item_id}\n---\n\nbody\n",
                 encoding="utf-8")
    return p


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        (self.root / "wiki").mkdir(parents=True)
        self.links = Path(self.tmp.name) / "room-links.json"
        roomvault._notes_cache.clear()
        self.p_links = mock.patch.object(roomvault, "LINKS_PATH", self.links)
        self.p_links.start()

    def tearDown(self):
        self.p_links.stop()
        roomvault._notes_cache.clear()
        self.tmp.cleanup()

    def resolve(self, items):
        return roomvault.resolve("room", items, root=self.root)

    def test_pointer_note_is_found_by_item_id(self):
        _note(self.root, "core-views", "abc123")
        items = [{"id": "abc123", "vault": ""}]
        self.resolve(items)
        self.assertEqual(items[0]["vault_note"], "wiki/rooms/core-views.md")
        self.assertEqual(items[0]["vault_note_kind"], "room")

    def test_owner_summary_wins_and_is_labelled_owner(self):
        # an item carrying a summary never gets a pointer note minted, so the
        # summary is the only note there is — and it must not read as "room"
        _note(self.root, "stray", "abc123")
        items = [{"id": "abc123", "vault": "wiki/mechanistic.md"}]
        self.resolve(items)
        self.assertEqual(items[0]["vault_note"], "wiki/mechanistic.md")
        self.assertEqual(items[0]["vault_note_kind"], "owner")

    def test_absent_is_reported_as_absent(self):
        items = [{"id": "nope", "vault": ""}]
        self.resolve(items)
        self.assertEqual(items[0]["vault_note"], "")
        self.assertEqual(items[0]["vault_note_kind"], "")

    def test_owner_link_overrides_a_missing_derivation(self):
        roomvault.set_link("room", "abc123", "wiki/hand-written.md")
        items = [{"id": "abc123", "vault": ""}]
        self.resolve(items)
        self.assertEqual(items[0]["vault_note"], "wiki/hand-written.md")

    def test_owner_link_outranks_the_derived_pointer(self):
        _note(self.root, "derived", "abc123")
        roomvault.set_link("room", "abc123", "wiki/chosen.md")
        items = [{"id": "abc123", "vault": ""}]
        self.resolve(items)
        self.assertEqual(items[0]["vault_note"], "wiki/chosen.md")

    def test_clearing_a_link_falls_back_to_the_derivation(self):
        _note(self.root, "derived", "abc123")
        roomvault.set_link("room", "abc123", "wiki/chosen.md")
        roomvault.set_link("room", "abc123", "")
        items = [{"id": "abc123", "vault": ""}]
        self.resolve(items)
        self.assertEqual(items[0]["vault_note"], "wiki/rooms/derived.md")

    def test_links_are_scoped_per_room(self):
        roomvault.set_link("other", "abc123", "wiki/elsewhere.md")
        items = [{"id": "abc123", "vault": ""}]
        self.resolve(items)
        self.assertEqual(items[0]["vault_note"], "")

    def test_a_missing_rooms_dir_is_dormant_not_an_error(self):
        items = [{"id": "abc123", "vault": ""}]
        roomvault.resolve("room", items, root=Path(self.tmp.name) / "gone")
        self.assertEqual(items[0]["vault_note"], "")

    def test_the_index_refreshes_when_a_note_is_added(self):
        # the cache keys on the directory mtime; a note written after a first
        # read must be visible, or the fix would need a restart to take
        items = [{"id": "abc123", "vault": ""}]
        self.resolve(items)
        self.assertEqual(items[0]["vault_note"], "")
        _note(self.root, "late", "abc123")
        roomvault._notes_cache.clear()          # stands in for an mtime tick
        items = [{"id": "abc123", "vault": ""}]
        self.resolve(items)
        self.assertEqual(items[0]["vault_note"], "wiki/rooms/late.md")

    def test_paths_are_vault_relative_posix(self):
        _note(self.root, "core-views", "abc123")
        items = [{"id": "abc123", "vault": ""}]
        self.resolve(items)
        # what openNoteWindow and /api/vault/note both expect
        self.assertFalse(items[0]["vault_note"].startswith("/"))
        self.assertIn("/", items[0]["vault_note"])


if __name__ == "__main__":
    unittest.main()
