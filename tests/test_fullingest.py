"""Full ingest — staging, reconcile, and the retirement of pointer notes."""
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import fullingest, readingroom, roomvault, vault
from scripts import stage_tcil_selection


def item(**kw):
    base = {
        "title": "A Talk About Things", "url": "https://example.com/talk",
        "date": "2025-01-02", "year": "2025", "mode": "watch",
        "status": "MISSING", "prio": "P1", "people": ["Ada Lovelace"],
        "type": "lecture", "venue": "Somewhere", "note": "",
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
        # readingroom's build lock resolves under ROOT/data/reading.
        (self.tmp / "data" / "reading").mkdir(parents=True)
        self.p_rooms = mock.patch.object(readingroom, "ROOMS_DIR", self.rooms)
        self.p_root = mock.patch.object(readingroom, "ROOT", self.tmp)
        self.p_vault = mock.patch.object(vault, "vault_root",
                                         lambda: self.vault)
        for p in (self.p_rooms, self.p_root, self.p_vault):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        fullingest._summaries_cache.clear()

    def room(self, items, slug="demo"):
        doc = {"slug": slug, "title": "Demo Room", "subtitle": "",
               "built": "2026-07-01", "updated": "2026-07-01T00:00:00-04:00",
               "legacy_key": "", "definition": {}, "items": items}
        (self.rooms / f"{slug}.json").write_text(json.dumps(doc),
                                                 encoding="utf-8")
        return doc

    def summary(self, stem, iid):
        p = self.vault / "wiki" / f"{stem}.md"
        p.write_text("---\ntitle: \"S\"\ntype: source-summary\n"
                     f"room_item_id: {iid}\n---\n\n# S\n", encoding="utf-8")
        return p

    def pointer(self, stem, iid):
        d = self.vault / roomvault.ROOMS_SUBDIR
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{stem}.md"
        p.write_text("---\ntitle: \"P\"\ntype: reading-room-item\n"
                     f"room_item_id: {iid}\n---\n\n# P\n", encoding="utf-8")
        return p


class ClassifyTests(unittest.TestCase):
    def test_youtube_forms(self):
        for u in ("https://www.youtube.com/watch?v=abc123DEF45",
                  "https://youtu.be/abc123DEF45",
                  "https://www.youtube.com/shorts/abc123DEF45",
                  "https://www.youtube.com/live/abc123DEF45"):
            self.assertEqual(fullingest.classify(u), "youtube", u)
            self.assertEqual(fullingest.video_id(u), "abc123DEF45", u)

    def test_web_and_empty(self):
        self.assertEqual(fullingest.classify("https://example.com/post"), "web")
        self.assertEqual(fullingest.classify(""), "")


class VttTests(unittest.TestCase):
    VTT = (
        "WEBVTT\nKind: captions\nLanguage: en\n\n"
        "00:00:00.000 --> 00:00:02.000\nhello there\n\n"
        "00:00:02.000 --> 00:00:04.000\nhello there\ngeneral <c>kenobi</c>\n\n"
        "00:00:04.000 --> 00:00:06.000\ngeneral kenobi\nyou are bold\n")

    def test_rolling_repeats_dedupe_and_tags_strip(self):
        text = fullingest.vtt_to_text(self.VTT)
        self.assertEqual(text.count("hello there"), 1)
        self.assertEqual(text.count("general kenobi"), 1)
        self.assertIn("you are bold", text)
        self.assertNotIn("<c>", text)

    def test_blocks_carry_stamps(self):
        text = fullingest.vtt_to_text(self.VTT)
        self.assertTrue(text.startswith("**0:00** ·"), text[:24])

    def test_hour_long_stamps(self):
        vtt = ("WEBVTT\n\n01:02:03.000 --> 01:02:05.000\ndeep into it\n")
        self.assertIn("**1:02:03**", fullingest.vtt_to_text(vtt))


class ArticleTests(unittest.TestCase):
    HTML = ("<html><head><title>The Piece — Site</title>"
            "<style>.x{color:red}</style></head><body>"
            "<nav>menu menu</nav><article><h1>The Piece</h1>"
            "<p>First paragraph of substance.</p>"
            "<script>var x=1;</script>"
            "<p>Second &amp; final paragraph.</p></article>"
            "<footer>footer junk</footer></body></html>")

    def fake_urlopen(self, html):
        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return lambda req, timeout=0: R(html.encode("utf-8"))

    def test_extracts_article_body_and_title(self):
        with mock.patch("urllib.request.urlopen", self.fake_urlopen(self.HTML)):
            title, text = fullingest.fetch_article("https://e.com/p")
        self.assertEqual(title, "The Piece — Site")
        self.assertIn("First paragraph of substance.", text)
        self.assertIn("Second & final paragraph.", text)
        self.assertNotIn("menu menu", text)
        self.assertNotIn("footer junk", text)
        self.assertNotIn("var x=1", text)


class StageItemTests(Base):
    def test_no_url(self):
        self.assertEqual(
            fullingest.stage_item(item(url=""), "demo", self.vault), "no_url")

    def test_already_staged_is_untouched(self):
        it = item()
        p = fullingest.raw_path(self.vault, it)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("existing", encoding="utf-8")
        self.assertEqual(fullingest.stage_item(it, "demo", self.vault),
                         "already")
        self.assertEqual(p.read_text(encoding="utf-8"), "existing")

    def test_youtube_without_ytdlp_is_named(self):
        it = item(url="https://youtu.be/abc123DEF45")
        state = fullingest.stage_item(it, "demo", self.vault, binary="")
        self.assertEqual(state, "failed: yt-dlp not installed")

    def test_youtube_without_captions_needs_transcription(self):
        it = item(url="https://youtu.be/abc123DEF45")
        with mock.patch.object(fullingest, "fetch_meta",
                               return_value={"title": "T", "channel": "C"}), \
             mock.patch.object(fullingest, "fetch_captions",
                               return_value=("", "no captions")):
            state = fullingest.stage_item(it, "demo", self.vault, binary="yt")
        self.assertEqual(state, "needs_transcription")
        self.assertFalse(fullingest.raw_path(self.vault, it).exists())

    def test_youtube_stages_a_raw_with_room_id(self):
        it = item(url="https://youtu.be/abc123DEF45")
        with mock.patch.object(fullingest, "fetch_meta",
                               return_value={"title": "T", "channel": "Chan",
                                             "upload_date": "20250102",
                                             "description": "About things."}), \
             mock.patch.object(fullingest, "fetch_captions",
                               return_value=("**0:00** · words", "auto-captions")):
            state = fullingest.stage_item(it, "demo", self.vault, binary="yt")
        self.assertEqual(state, "staged")
        raw = fullingest.raw_path(self.vault, it).read_text(encoding="utf-8")
        self.assertIn(f"room_item_id: {it['id']}", raw)
        self.assertIn("room_slug: demo", raw)
        self.assertIn("## Transcript", raw)
        self.assertIn("**0:00** · words", raw)
        self.assertIn('author:\n  - "[[Chan]]"', raw)
        self.assertIn("published: 2025-01-02", raw)

    def test_research_ids_are_preserved_in_raw_frontmatter(self):
        it = item(url="https://example.com/research",
                  research_source_id="src_123",
                  research_event_id="evt_456")
        with mock.patch.object(fullingest, "fetch_article",
                               return_value=("T", "x" * 500)):
            self.assertEqual(
                fullingest.stage_item(it, "anthropic", self.vault), "staged")
        raw = fullingest.raw_path(self.vault, it).read_text(encoding="utf-8")
        self.assertIn('research_source_id: "src_123"', raw)
        self.assertIn('research_event_id: "evt_456"', raw)

    def test_web_too_thin_is_a_named_failure(self):
        it = item(url="https://example.com/thin")
        with mock.patch.object(fullingest, "fetch_article",
                               return_value=("T", "tiny")):
            state = fullingest.stage_item(it, "demo", self.vault)
        self.assertTrue(state.startswith("failed:"), state)
        self.assertIn("paywalled or script-rendered", state)

    def test_web_stages_content(self):
        it = item(url="https://example.com/post")
        with mock.patch.object(fullingest, "fetch_article",
                               return_value=("T", "x" * 500)):
            state = fullingest.stage_item(it, "demo", self.vault)
        self.assertEqual(state, "staged")
        raw = fullingest.raw_path(self.vault, it).read_text(encoding="utf-8")
        self.assertIn("## Content", raw)
        self.assertIn(f"room_item_id: {it['id']}", raw)


class StageSweepTests(Base):
    def test_passive_refuses(self):
        self.room([item()])
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(fullingest.StageError):
                fullingest.stage("demo")

    def test_unknown_room_raises(self):
        with self.assertRaises(fullingest.StageError):
            fullingest.stage("nope")

    def test_consumed_items_are_skipped(self):
        self.room([item(vault="wiki/existing.md")])
        res = fullingest.stage("demo")
        self.assertEqual(res["counts"], {"consumed": 1})

    def test_counts_and_failures_are_named(self):
        self.room([item(url="https://e.com/a", title="A"),
                   item(url="https://e.com/b", title="B")])
        with mock.patch.object(fullingest, "fetch_article",
                               side_effect=[("T", "x" * 500),
                                            RuntimeError("dead")]):
            res = fullingest.stage("demo")
        self.assertEqual(res["counts"].get("staged"), 1)
        self.assertEqual(res["counts"].get("failed"), 1)
        self.assertEqual(res["failures"][0]["title"], "B")


class StageSelectionTests(Base):
    def test_stages_only_explicit_clean_selection(self):
        selected = [item(title="A", research_source_id="src-a",
                         research_event_id="evt-a", private_owner_note="never"),
                    item(title="B", url="https://e.com/b",
                         research_source_id="src-b", research_event_id="evt-b")]
        seen = []
        with mock.patch.object(fullingest, "stage_item",
                               side_effect=lambda it, slug, root, binary:
                               seen.append(it) or "staged"):
            res = fullingest.stage_items(
                selected, "anthropic-universe", self.vault, binary="")
        self.assertEqual(res["selected"], 2)
        self.assertEqual(res["counts"], {"staged": 2})
        self.assertEqual([row["state"] for row in res["outcomes"]],
                         ["staged", "staged"])
        self.assertEqual([it["research_source_id"] for it in seen],
                         ["src-a", "src-b"])
        self.assertNotIn("private_owner_note", seen[0])

    def test_passive_refuses_selection(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(fullingest.StageError):
                fullingest.stage_items([item()], "anthropic-universe", self.vault)


class SelectionManifestTests(unittest.TestCase):
    @staticmethod
    def row(source_id="src-a", event_id="evt-a", **extra):
        row = {"source_id": source_id, "event_id": event_id,
               "title": "Public source", "url": "https://example.com/a",
               "publication_date": "2026-08-06", "priority": "high_signal",
               "claim_count": 2}
        row.update(extra)
        return row

    def manifest(self, selected=None, pointers=None, bibliography=None):
        selected = selected if selected is not None else [self.row()]
        return {"counts": {"by_action": {"ingest_full": len(selected)}},
                "ingest_full": selected,
                "pointer_only_distribution": pointers or [],
                "bibliography_only": bibliography or []}

    def test_projects_public_whitelist_and_graph_ids(self):
        manifest = self.manifest([self.row(personal_relevance="secret",
                                                application_use="secret")])
        items = stage_tcil_selection.selection(manifest, 1)
        self.assertEqual(items[0]["research_source_id"], "src-a")
        self.assertEqual(items[0]["research_event_id"], "evt-a")
        self.assertNotIn("personal_relevance", items[0])
        self.assertNotIn("application_use", items[0])

    def test_excluded_action_overlap_is_rejected(self):
        manifest = self.manifest(
            [self.row()], pointers=[{"source_id": "src-a"}])
        with self.assertRaises(stage_tcil_selection.SelectionError):
            stage_tcil_selection.selection(manifest, 1)

    def test_expected_count_is_a_hard_gate(self):
        with self.assertRaises(stage_tcil_selection.SelectionError):
            stage_tcil_selection.selection(self.manifest(), 31)


class SummariesIndexTests(Base):
    def test_finds_summaries_and_skips_other_types(self):
        self.summary("real-summary", "id111")
        (self.vault / "wiki" / "not-a-summary.md").write_text(
            "---\ntitle: \"x\"\ntype: concept\nroom_item_id: id222\n---\n",
            encoding="utf-8")
        idx = fullingest.summaries_by_item(self.vault)
        self.assertIn("id111", idx)
        self.assertNotIn("id222", idx)

    def test_cache_invalidates_on_new_note(self):
        self.summary("first", "id111")
        idx = fullingest.summaries_by_item(self.vault)
        self.assertEqual(set(idx), {"id111"})
        import time
        time.sleep(0.02)
        self.summary("second", "id333")
        os.utime(self.vault / "wiki")
        idx = fullingest.summaries_by_item(self.vault)
        self.assertIn("id333", idx)


class ReconcileTests(Base):
    def test_links_store_and_retires_pointer(self):
        it = item()
        self.room([it])
        self.summary("the-summary", it["id"])
        ptr = self.pointer("the-pointer", it["id"])
        res = fullingest.reconcile("demo")
        self.assertEqual((res["linked"], res["retired"]), (1, 1))
        room = readingroom.load_room("demo")
        self.assertEqual(room["items"][0]["vault"], "wiki/the-summary.md")
        self.assertEqual(room["items"][0]["status"], "HAVE")
        self.assertFalse(ptr.exists())
        self.assertTrue((self.vault / fullingest.RETIRE_SUBDIR /
                         "the-pointer.md").exists())

    def test_idempotent(self):
        it = item()
        self.room([it])
        self.summary("the-summary", it["id"])
        fullingest.reconcile("demo")
        res = fullingest.reconcile("demo")
        self.assertEqual((res["linked"], res["retired"]), (0, 0))

    def test_pointer_without_summary_is_kept(self):
        it = item()
        self.room([it])
        ptr = self.pointer("the-pointer", it["id"])
        res = fullingest.reconcile("demo")
        self.assertEqual(res["retired"], 0)
        self.assertTrue(ptr.exists())

    def test_retire_name_collision_gets_a_suffix(self):
        it = item()
        self.room([it])
        self.summary("the-summary", it["id"])
        retire = self.vault / fullingest.RETIRE_SUBDIR
        retire.mkdir(parents=True)
        (retire / "the-pointer.md").write_text("older", encoding="utf-8")
        self.pointer("the-pointer", it["id"])
        res = fullingest.reconcile("demo")
        self.assertEqual(res["retired"], 1)
        self.assertTrue((retire / "the-pointer-2.md").exists())
        self.assertEqual((retire / "the-pointer.md").read_text(encoding="utf-8"),
                         "older")

    def test_passive_refuses(self):
        self.room([item()])
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(fullingest.StageError):
                fullingest.reconcile("demo")


class StatusTests(Base):
    def test_counts_every_state(self):
        a = item(url="https://e.com/a", title="A", vault="wiki/a.md")
        b = item(url="https://e.com/b", title="B")
        c = item(url="https://e.com/c", title="C")
        d = item(url="", title="D")
        self.room([a, b, c, d])
        self.summary("b-summary", b["id"])
        p = fullingest.raw_path(self.vault, c)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("raw", encoding="utf-8")
        s = fullingest.status("demo")
        self.assertEqual(s["consumed"], 1)
        self.assertEqual(s["synthesized_unlinked"], 1)
        self.assertEqual(s["staged"], 1)
        self.assertEqual(s["no_url"], 1)
        self.assertEqual(s["pending"], 0)


class SyncTests(Base):
    def test_passive_returns_none(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            self.assertIsNone(fullingest.sync("demo"))

    def test_runs_stage_then_reconcile_off_thread(self):
        calls = []
        with mock.patch.object(fullingest, "stage",
                               side_effect=lambda s: calls.append(("stage", s))), \
             mock.patch.object(fullingest, "reconcile",
                               side_effect=lambda s: calls.append(("rec", s))):
            t = fullingest.sync("demo")
            t.join(5)
        self.assertEqual(calls, [("stage", "demo"), ("rec", "demo")])

    def test_a_stage_failure_never_blocks_reconcile(self):
        calls = []
        with mock.patch.object(fullingest, "stage",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(fullingest, "reconcile",
                               side_effect=lambda s: calls.append(("rec", s))):
            t = fullingest.sync("demo")
            t.join(5)
        self.assertEqual(calls, [("rec", "demo")])


if __name__ == "__main__":
    unittest.main()
