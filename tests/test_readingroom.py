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
        # The load-bearing instructions: know the room, real URLs only,
        # add through the MERGE tool with only the new items — never a
        # whole-room re-emit (the ~70k single-string ceiling, 2026-08-03).
        self.assertIn("never re-propose", p)
        self.assertIn("Real URLs only", p)
        self.assertIn("mcp__vira__add_reading_room_items", p)
        self.assertIn("ONLY the new items", p)
        self.assertIn("NEVER re-emit the whole room", p)

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


class StructuredDefinitionTest(Base):
    """People pills, source feeds, the standing watch — and the legacy
    comma-string shape still parsing (older definitions store one)."""

    def test_people_pills_round_trip(self):
        d = readingroom.clean_definition({
            "people": [{"name": "Cat Wu", "ref": "wiki/cat-wu.md",
                        "qualifier": "Head of Product"}]})
        self.assertEqual(d["people"][0]["ref"], "wiki/cat-wu.md")
        self.assertEqual(readingroom.people_line(d), "Cat Wu")

    def test_legacy_comma_string_becomes_unresolved_pills(self):
        d = readingroom.clean_definition({"people": "A B, C D"})
        self.assertEqual(d["people"], [
            {"name": "A B", "ref": "", "qualifier": ""},
            {"name": "C D", "ref": "", "qualifier": ""}])

    def test_bad_refs_and_sources_are_refused(self):
        for bad in ({"people": [{"name": "X", "ref": "/etc/passwd"}]},
                    {"people": [{"name": "X", "ref": "http://x"}]},
                    {"people": [{"name": "X", "ref": "../../secrets"}]},
                    {"people": [{"ref": "wiki/x.md"}]},        # no name
                    {"sources": [{"label": "L", "feed": "ftp://x"}]},
                    {"sources": [{"label": "", "feed": "https://x"}]},
                    {"sources": [{"label": "L", "feed": "https://x",
                                  "kind": "carrier-pigeon"}]},
                    {"sources": "not-a-list"}):
            with self.assertRaises(readingroom.BuildError):
                readingroom.clean_definition(bad)

    def test_sources_and_watch_round_trip(self):
        d = readingroom.clean_definition({
            "sources": [{"label": "@claude",
                         "feed": "https://youtube.example/feed.xml",
                         "kind": "youtube"}],
            "watch": ["new Dario essays", "  ", "Boris interviews"]})
        self.assertEqual(d["sources"][0]["kind"], "youtube")
        self.assertEqual(d["watch"],
                         ["new Dario essays", "Boris interviews"])

    def test_update_prompt_grounds_each_person(self):
        self.build()
        readingroom.set_definition("test-room", {
            "people": [{"name": "Cat Wu", "ref": "wiki/cat-wu.md",
                        "qualifier": "Head of Product, Claude Code"}],
            "watch": ["new Boris interviews"]})
        p = readingroom.update_prompt("test-room")
        self.assertIn("identity page: wiki/cat-wu.md", p)
        self.assertIn("Head of Product, Claude Code", p)
        self.assertIn("not name-matches", p)
        self.assertIn("new Boris interviews", p)


class MergeItemsTest(Base):
    def test_merge_appends_and_keeps_existing_untouched(self):
        self.build()
        before = readingroom.load_room("test-room")["items"]
        res = readingroom.merge_items("test-room", [
            {"title": "A new talk", "url": "https://example.com/new"}])
        self.assertEqual(res["added"], 1)
        self.assertEqual(res["items"], 3)
        after = readingroom.load_room("test-room")["items"]
        self.assertEqual(after[:2], before)         # byte-identical carry

    def test_a_duplicate_url_is_dropped_not_doubled(self):
        self.build()
        res = readingroom.merge_items("test-room", [
            {"title": "A talk again", "url": "https://example.com/a"}])
        self.assertEqual(res["added"], 0)
        self.assertEqual(res["items"], 2)

    def test_ping_fires_on_new_items_only(self):
        self.build()
        with mock.patch.object(readingroom, "_ping_additions") as ping:
            readingroom.merge_items("test-room", [
                {"title": "Fresh", "url": "https://example.com/fresh"}])
            readingroom.merge_items("test-room", [
                {"title": "Fresh", "url": "https://example.com/fresh"}])
        self.assertEqual(ping.call_count, 1)
        self.assertEqual(ping.call_args[0][2], ["Fresh"])

    def test_unknown_room_and_bad_payload_raise(self):
        with self.assertRaises(KeyError):
            readingroom.merge_items("no-such-room", [])
        self.build()
        with self.assertRaises(readingroom.BuildError):
            readingroom.merge_items("test-room", "not-a-list")
        with self.assertRaises(readingroom.BuildError):
            readingroom.merge_items("test-room", [{"url": "https://x"}])


ATOM_FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry><title>Already in the room</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=AAAAAAAAAAA"/>
    <published>2026-07-01T00:00:00+00:00</published></entry>
  <entry><title>Brand new video</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=BBBBBBBBBBB"/>
    <published>2026-08-01T00:00:00+00:00</published></entry>
</feed>"""

RSS_FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>Known post</title><link>https://blog.example/known</link>
    <pubDate>Tue, 04 Aug 2026 10:00:00 GMT</pubDate></item>
  <item><title>New post</title><link>https://blog.example/new</link>
    <pubDate>Wed, 05 Aug 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""


class EnumerateSourcesTest(Base):
    """The deterministic half of a refresh: feeds are enumerated in code
    and the model is handed the residual — because the keyword sweep
    provably missed known-channel items (2026-08-05 diagnosis)."""

    def room_with_sources(self):
        readingroom.build("test-room", "Test Room", "", [
            # Query-string variant on purpose: the diff must key on the
            # VIDEO ID, not the exact URL.
            {"title": "Old video",
             "url": "https://www.youtube.com/watch?v=AAAAAAAAAAA&t=25s"},
            {"title": "Known post", "url": "https://blog.example/known/"}])
        readingroom.set_definition("test-room", {
            "sources": [
                {"label": "@testtube", "kind": "youtube",
                 "feed": "https://yt.example/feed.xml"},
                {"label": "blog", "kind": "rss",
                 "feed": "https://blog.example/rss"}]})
        return readingroom.load_room("test-room")

    def test_diff_keys_on_video_id_and_normalized_url(self):
        room = self.room_with_sources()
        feeds = {"https://yt.example/feed.xml": ATOM_FEED,
                 "https://blog.example/rss": RSS_FEED}
        with mock.patch.object(readingroom, "_http_get",
                               side_effect=lambda u, **k: feeds[u]):
            out = readingroom.enumerate_sources(room)
        titles = {c["title"] for c in out["candidates"]}
        self.assertEqual(titles, {"Brand new video", "New post"})
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["swept"], 2)

    def test_a_dead_feed_is_a_named_error_not_a_failure(self):
        room = self.room_with_sources()

        def fetch(url, **k):
            if "yt.example" in url:
                raise OSError("connection refused")
            return RSS_FEED
        with mock.patch.object(readingroom, "_http_get", side_effect=fetch):
            out = readingroom.enumerate_sources(room)
        self.assertEqual(len(out["errors"]), 1)
        self.assertIn("@testtube", out["errors"][0])
        self.assertEqual({c["title"] for c in out["candidates"]},
                         {"New post"})

    def test_no_sources_means_no_network_and_no_sweep_block(self):
        self.build()
        with mock.patch.object(readingroom, "_http_get",
                               side_effect=AssertionError("no network")):
            out = readingroom.enumerate_sources(
                readingroom.load_room("test-room"))
            p = readingroom.update_prompt("test-room")
        self.assertEqual(out["swept"], 0)
        self.assertNotIn("DETERMINISTIC SWEEP", p)

    def test_update_prompt_embeds_candidates_and_errors(self):
        self.room_with_sources()

        def fetch(url, **k):
            if "yt.example" in url:
                return ATOM_FEED
            raise OSError("timed out")
        with mock.patch.object(readingroom, "_http_get", side_effect=fetch):
            p = readingroom.update_prompt("test-room")
        self.assertIn("DETERMINISTIC SWEEP", p)
        self.assertIn("Brand new video", p)
        self.assertIn("watch?v=BBBBBBBBBBB", p)
        self.assertIn("FEED ERROR", p)
        self.assertIn("blog", p)
        # Judgment, not discovery — and the write path is the merge tool.
        self.assertIn("your job is judgment", p)


class ResolveSourceTest(Base):
    def test_handle_resolves_to_the_channel_feed(self):
        page = 'noise "channelId":"UCabcdefghijklmnopqrst" noise'
        with mock.patch.object(readingroom, "_http_get",
                               return_value=page) as get:
            src = readingroom.resolve_source("@claude")
        self.assertEqual(src["kind"], "youtube")
        self.assertIn("channel_id=UCabcdefghijklmnopqrst", src["feed"])
        self.assertEqual(src["label"], "@claude")
        get.assert_called_once_with("https://www.youtube.com/@claude")

    def test_channel_url_needs_no_fetch(self):
        with mock.patch.object(readingroom, "_http_get",
                               side_effect=AssertionError("no fetch")):
            src = readingroom.resolve_source(
                "https://www.youtube.com/channel/UCabcdefghijklmnopqrst")
        self.assertIn("channel_id=UC", src["feed"])

    def test_direct_feed_and_page_with_alternate(self):
        feed_xml = "<?xml version='1.0'?><rss><channel>" \
                   "<title>My Feed</title></channel></rss>"
        with mock.patch.object(readingroom, "_http_get",
                               return_value=feed_xml):
            src = readingroom.resolve_source("https://blog.example/rss.xml")
        self.assertEqual(src, {"label": "My Feed", "kind": "rss",
                               "feed": "https://blog.example/rss.xml"})
        page = ('<html><link rel="alternate" '
                'type="application/rss+xml" href="/feed.xml"></html>')
        with mock.patch.object(readingroom, "_http_get", return_value=page):
            src = readingroom.resolve_source("https://blog.example/")
        self.assertEqual(src["feed"], "https://blog.example/feed.xml")

    def test_unresolvable_text_is_refused_with_direction(self):
        for text in ("", "watch for new essays"):
            with self.assertRaises(ValueError):
                readingroom.resolve_source(text)
        page = "<html>no feed here</html>"
        with mock.patch.object(readingroom, "_http_get", return_value=page):
            with self.assertRaises(ValueError):
                readingroom.resolve_source("https://blog.example/")


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
