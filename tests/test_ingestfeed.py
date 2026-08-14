"""The Inflow scan.

Every case is rooted at ONE tmp vault. This module reads the vault's wiki
directory AND (through feed) the done-mark store, so both are pinned inside
the fixture — `test_an_empty_fixture_root_reads_nothing` is the guard: a
source added later that reaches the real machine instead of the fixture
fails it on sight.
"""
import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from server import ingestfeed, reading


def note(fm, body=""):
    return "---\n" + fm.strip() + "\n---\n" + body


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wiki = self.root / "wiki"
        self.wiki.mkdir(parents=True)
        (self.root / "raw").mkdir()
        self.store = self.root / "marks"
        self.store.mkdir()
        patches = [
            mock.patch.object(ingestfeed, "vault_root", lambda: self.root),
            mock.patch.object(ingestfeed, "THUMB_DIR", self.root / "thumbs"),
            mock.patch.object(reading, "STORE_DIR", self.store),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        ingestfeed._cache.update(key=None, cards=None, counts=None,
                                root=None, at=0.0)
        self.addCleanup(lambda: ingestfeed._cache.update(
            key=None, cards=None, counts=None, root=None, at=0.0))

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, fm, body=""):
        p = self.wiki / (name + ".md")
        p.write_text(note(fm, body), encoding="utf-8")
        return p

    def asset(self, rel, data=b"\xff\xd8\xffimagebytes"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def cards(self):
        return ingestfeed.scan(force=True)[0]

    def only(self):
        c = self.cards()
        self.assertEqual(len(c), 1, "expected exactly one card")
        return c[0]


class Isolation(Base):
    def test_an_empty_fixture_root_reads_nothing(self):
        cards, counts = ingestfeed.scan(force=True)
        self.assertEqual(cards, [])
        self.assertEqual(counts, {})

    def test_a_missing_vault_is_dormant_not_an_error(self):
        with mock.patch.object(ingestfeed, "vault_root", lambda: None):
            self.assertEqual(ingestfeed.scan(force=True), ([], {}))
            self.assertEqual(ingestfeed.feed()["items"], [])

    def test_a_note_that_is_not_a_source_summary_is_skipped(self):
        self.write("concept", "title: A concept\ntype: concept\ntags: [youtube]")
        self.assertEqual(self.cards(), [])


class Bucketing(Base):
    def test_the_raw_pointer_decides(self):
        self.write("a", 'title: A\ntype: source-summary\n'
                        'source: "[[raw/youtube-subs/a-xyz.md]]"')
        self.assertEqual(self.only()["source"], "youtube")

    def test_a_tag_answers_when_there_is_no_pointer(self):
        self.write("a", "title: A\ntype: source-summary\ntags: [voice-memo, cre]")
        self.assertEqual(self.only()["source"], "voice-memo")

    def test_the_url_host_is_the_last_rung(self):
        self.write("a", "title: A\ntype: source-summary\n"
                        "source_url: https://x.com/someone/status/1")
        self.assertEqual(self.only()["source"], "x")

    def test_the_pointer_outranks_a_tag_naming_another_source(self):
        # Real shape: an Instagram capture whose subject is a YouTube video
        # carries a `youtube` topic tag. The pointer is the fact.
        self.write("a", 'title: A\ntype: source-summary\n'
                        'source: "[[raw/instagram/ig-1.md]]"\n'
                        'tags: [youtube, explainer]\n'
                        'source_url: https://www.youtube.com/watch?v=z')
        self.assertEqual(self.only()["source"], "instagram")

    def test_an_unknown_source_buckets_as_other(self):
        self.write("a", 'title: A\ntype: source-summary\n'
                        'source: "[[raw/chatgpt/c-1.md]]"')
        self.assertEqual(self.only()["source"], "other")

    def test_tags_parse_in_the_block_shape_too(self):
        self.write("a", "title: A\ntype: source-summary\ntags:\n"
                        "  - plaud-transcript\n  - meeting")
        c = self.only()
        self.assertEqual(c["source"], "plaud")
        self.assertEqual(c["topics"], ["meeting"])


class CardFields(Base):
    def setUp(self):
        super().setUp()
        self.asset("wiki/assets/a/chart.jpg")
        self.asset("wiki/assets/a/second.png")
        self.write(
            "radiant",
            'title: "Radiant — microreactors as a product"\n'
            'type: source-summary\n'
            'source_type: video-summary\n'
            'source: "[[raw/youtube-subs/a16z-9V.md]]"\n'
            'source_url: https://www.youtube.com/watch?v=9V\n'
            'created: 2026-08-05\n'
            'tags: [youtube, a16z, nuclear-power, cat/ai]',
            "# Radiant\n\n"
            "> Transcription caveat: names may be garbled.\n\n"
            "One of three a16z mini-documentaries published the same day, "
            "see [[wiki/american-dynamism|american-dynamism]].\n\n"
            "## Visuals\n\n"
            "- ![[wiki/assets/a/chart.jpg]] **1:29 — Archival core sections.**"
            " Mid-century drawing of the lattice."
            " [→ video](https://www.youtube.com/watch?v=9V&t=89s)\n"
            "- ![[wiki/assets/a/second.png]] **Reactor vessel cutaway.**"
            " A dimensioned section.\n")

    def test_the_headline_fields(self):
        c = self.only()
        self.assertEqual(c["title"], "Radiant — microreactors as a product")
        self.assertEqual(c["source"], "youtube")
        self.assertEqual(c["kind"], "video-summary")
        self.assertEqual(c["date"], "2026-08-05")
        self.assertEqual(c["url"], "https://www.youtube.com/watch?v=9V")
        self.assertEqual(c["raws"], ["raw/youtube-subs/a16z-9V.md"])
        self.assertEqual(c["path"], "wiki/radiant.md")

    def test_the_blurb_is_the_first_real_paragraph_with_links_flattened(self):
        c = self.only()
        self.assertTrue(c["blurb"].startswith("One of three a16z"))
        self.assertIn("american-dynamism", c["blurb"])
        self.assertNotIn("[[", c["blurb"])
        self.assertNotIn("Transcription caveat", c["blurb"])

    def test_a_quoted_title_comes_back_without_its_escapes(self):
        # 163 titles in the real vault are quotes inside a quoted scalar.
        # Stripping only the outer pair renders the backslashes on the card.
        self.write("q", 'title: "\\"Please stay\\" — a saved clip"\n'
                        'type: source-summary\ntags: [notes-app]')
        c = [x for x in self.cards() if x["path"].endswith("q.md")][0]
        self.assertEqual(c["title"], '"Please stay" — a saved clip')

    def test_platform_and_category_tags_are_not_topics(self):
        self.assertEqual(self.only()["topics"], ["a16z", "nuclear-power"])

    def test_visuals_carry_the_timestamp_label_prose_and_deep_link(self):
        imgs = self.only()["images"]
        self.assertEqual(len(imgs), 2)
        self.assertEqual(imgs[0]["at"], "1:29")
        self.assertEqual(imgs[0]["label"], "Archival core sections")
        self.assertEqual(imgs[0]["caption"], "Mid-century drawing of the lattice.")
        self.assertIn("t=89s", imgs[0]["link"])
        self.assertEqual(imgs[1]["at"], "")
        self.assertEqual(imgs[1]["label"], "Reactor vessel cutaway")
        self.assertEqual(imgs[1]["link"], "")

    def test_the_id_is_hashed_and_fits_the_done_key_cap(self):
        # reading._clean_id caps a key at 64 characters, and 102 of this
        # vault's note names are longer than that — a stem id would be
        # stored truncated and read back whole, so the mark would never
        # show. The id must survive that function unchanged.
        c = self.only()
        self.assertEqual(reading._clean_id(c["id"]), c["id"])
        self.assertEqual(
            c["id"],
            "if" + hashlib.sha1(b"wiki/radiant.md").hexdigest()[:14])

    def test_a_very_long_note_name_still_keys_correctly(self):
        name = "a-" + ("very-long-" * 12) + "name"
        self.assertGreater(len(name), 64)
        self.write(name, "title: Long\ntype: source-summary\ntags: [notes-app]")
        card = [c for c in self.cards() if c["path"].endswith(name + ".md")][0]
        self.assertEqual(reading._clean_id(card["id"]), card["id"])


class Visuals(Base):
    def test_an_embed_outside_the_visuals_list_still_counts(self):
        self.asset("wiki/assets/b/inline.png")
        self.write("b", "title: B\ntype: source-summary\ntags: [notes-app]",
                   "Some prose.\n\n![[wiki/assets/b/inline.png]]\n")
        self.assertEqual(self.only()["image_count"], 1)

    def test_an_embed_whose_file_is_gone_is_not_offered(self):
        self.write("b", "title: B\ntype: source-summary\ntags: [notes-app]",
                   "![[wiki/assets/b/missing.png]]\n")
        self.assertEqual(self.only()["images"], [])

    def test_a_non_image_embed_is_not_an_image(self):
        self.asset("wiki/assets/b/deck.pdf", b"%PDF-")
        self.write("b", "title: B\ntype: source-summary\ntags: [notes-app]",
                   "![[wiki/assets/b/deck.pdf]]\n")
        self.assertEqual(self.only()["images"], [])

    def test_the_same_image_twice_is_one_image(self):
        self.asset("wiki/assets/b/one.jpg")
        self.write("b", "title: B\ntype: source-summary\ntags: [notes-app]",
                   "## Visuals\n\n- ![[wiki/assets/b/one.jpg]] **X.**\n\n"
                   "Later: ![[wiki/assets/b/one.jpg]]\n")
        self.assertEqual(self.only()["image_count"], 1)

    def test_a_visual_row_is_never_mistaken_for_the_blurb(self):
        self.asset("wiki/assets/b/one.jpg")
        self.write("b", "title: B\ntype: source-summary\ntags: [notes-app]",
                   "## Visuals\n\n- ![[wiki/assets/b/one.jpg]] **A label.** "
                   "Prose long enough to clear the blurb floor easily.\n")
        self.assertEqual(self.only()["blurb"], "")


class Pointers(Base):
    def test_raws_named_only_in_the_prose_are_found(self):
        # The merged voice-memo summary names its five recordings in the
        # body and carries no `source:` pointer at all.
        self.write("coffee", "title: Coffee\ntype: source-summary\n"
                             "tags: [voice-memo, cre]",
                   "Captured across [[raw/voice-memos/vm-1.md]] and "
                   "[[raw/voice-memos/vm-2.md]] on the morning.\n")
        c = self.only()
        self.assertEqual(c["source"], "voice-memo")
        self.assertEqual(c["raws"],
                         ["raw/voice-memos/vm-1.md", "raw/voice-memos/vm-2.md"])


class Feed(Base):
    def setUp(self):
        super().setUp()
        self.write("y", "title: Y\ntype: source-summary\ntags: [youtube]")
        self.write("n", "title: N\ntype: source-summary\ntags: [notes-app]")
        self.write("i1", "title: I1\ntype: source-summary\ntags: [instagram]")
        self.write("i2", "title: I2\ntype: source-summary\ntags: [instagram]")

    def test_the_default_shelf_is_the_five_named_routines(self):
        f = ingestfeed.feed(force=True)
        self.assertEqual({i["source"] for i in f["items"]}, {"youtube", "notes"})
        self.assertEqual(f["shown"], 2)
        self.assertEqual(f["total"], 4)

    def test_a_source_that_is_off_still_reports_its_count(self):
        f = ingestfeed.feed(force=True)
        chip = [c for c in f["sources"] if c["id"] == "instagram"][0]
        self.assertFalse(chip["on"])
        self.assertEqual(chip["count"], 2)

    def test_asking_for_a_source_includes_it(self):
        f = ingestfeed.feed(sources=["instagram"], force=True)
        self.assertEqual(f["shown"], 2)
        self.assertEqual({i["source"] for i in f["items"]}, {"instagram"})

    def test_other_captures_get_a_chip_of_their_own(self):
        self.write("o", 'title: O\ntype: source-summary\n'
                        'source: "[[raw/chatgpt/c.md]]"')
        f = ingestfeed.feed(force=True)
        chip = [c for c in f["sources"] if c["id"] == "other"][0]
        self.assertEqual(chip["count"], 1)
        self.assertFalse(chip["on"])

    def test_newest_first(self):
        self.write("old", "title: Old\ntype: source-summary\n"
                          "tags: [youtube]\ncreated: 2026-01-01")
        self.write("new", "title: New\ntype: source-summary\n"
                          "tags: [youtube]\ncreated: 2026-08-01")
        dates = [i["date"] for i in ingestfeed.feed(force=True)["items"]]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_a_read_mark_rides_the_card(self):
        f = ingestfeed.feed(force=True)
        target = f["items"][0]
        self.assertFalse(target["done"])
        reading.set_done(ingestfeed.DONE_LIST, target["id"], True)
        again = ingestfeed.feed(force=True)
        marked = [i for i in again["items"] if i["id"] == target["id"]][0]
        self.assertTrue(marked["done"])
        self.assertEqual(again["done"], 1)

    def test_read_state_is_never_written_onto_the_shared_cache(self):
        f = ingestfeed.feed(force=True)
        reading.set_done(ingestfeed.DONE_LIST, f["items"][0]["id"], True)
        ingestfeed.feed()                     # marks one, from the cache
        cards, _ = ingestfeed.scan()
        self.assertTrue(all("done" not in c for c in cards))


class Caching(Base):
    def test_a_new_note_invalidates_the_cache(self):
        self.write("a", "title: A\ntype: source-summary\ntags: [youtube]")
        self.assertEqual(len(ingestfeed.scan(force=True)[0]), 1)
        self.write("b", "title: B\ntype: source-summary\ntags: [youtube]")
        ingestfeed._cache["at"] = 0.0         # step past the short TTL
        self.assertEqual(len(ingestfeed.scan()[0]), 2)


class Assets(Base):
    def test_a_real_file_resolves(self):
        p = self.asset("wiki/assets/a/chart.jpg")
        # .resolve() on both sides: macOS tmp is a symlink (/var ->
        # /private/var) and asset_path resolves for containment.
        self.assertEqual(ingestfeed.asset_path("wiki/assets/a/chart.jpg"),
                         p.resolve())

    def test_a_path_that_escapes_the_vault_is_refused(self):
        outside = self.root.parent / "secret.jpg"
        outside.write_bytes(b"x")
        self.addCleanup(outside.unlink)
        self.assertIsNone(ingestfeed.asset_path("../secret.jpg"))
        self.assertIsNone(ingestfeed.asset_path("wiki/../../secret.jpg"))

    def test_a_note_is_not_an_asset(self):
        self.write("a", "title: A\ntype: source-summary")
        self.assertIsNone(ingestfeed.asset_path("wiki/a.md"))

    def test_a_missing_file_is_none(self):
        self.assertIsNone(ingestfeed.asset_path("wiki/assets/nope.jpg"))

    def test_no_vault_means_no_asset(self):
        with mock.patch.object(ingestfeed, "vault_root", lambda: None):
            self.assertIsNone(ingestfeed.asset_path("wiki/assets/a.jpg"))

    def test_a_thumb_falls_back_to_the_original_when_there_is_no_sips(self):
        p = self.asset("wiki/assets/a/chart.jpg")
        with mock.patch.object(ingestfeed.shutil, "which", lambda n: None):
            self.assertEqual(ingestfeed.thumb_path("wiki/assets/a/chart.jpg"),
                             p.resolve())

    def test_a_thumb_of_a_non_image_is_none(self):
        self.asset("wiki/assets/a/deck.pdf", b"%PDF-")
        self.assertIsNone(ingestfeed.thumb_path("wiki/assets/a/deck.pdf"))


class Table(unittest.TestCase):
    def test_every_source_row_is_complete(self):
        for s in ingestfeed.SOURCES:
            for key in ("id", "label", "on", "glyph", "paths", "tags", "hosts"):
                self.assertIn(key, s, s.get("id"))

    def test_the_done_list_name_is_a_legal_store_name(self):
        self.assertRegex(ingestfeed.DONE_LIST, reading.NAME_RE)

    def test_the_five_named_routines_are_the_default(self):
        self.assertEqual(
            set(ingestfeed.DEFAULT_ON),
            {"youtube", "x", "voice-memo", "plaud", "notes"})


if __name__ == "__main__":
    unittest.main()
