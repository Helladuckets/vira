"""Images attached to a queued idea — storage, the read Vira takes off
them, and the block that puts them in front of a dispatched agent.

Everything roots at ONE throwaway fixture: the image store directory, the
thumbnail cache AND the idea store. The standing lesson these follow is
that patching a store isolates what a function WRITES and does nothing
about what it READS — ideaimages.file_path and .purge both walk DIR, so a
test that patched only ideas.STORE would be reading the owner's real
attachments. `test_the_fixture_isolates_every_store` is the guard: a
source added later that reaches outside the fixture fails it on sight.

The derivation cases mock localmodels rather than the OCR result, because
the interesting behaviour is the LADDER — OCR first, caption only when OCR
comes back thin, and an honest empty when a machine has neither.

Run: .venv/bin/python -m unittest discover tests
"""
import base64
import json
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from server import circuits, ideaimages, ideas, ideatags

# The REAL store paths, captured at import before any patching. The guard
# case below compares their bytes across a full run of everything this
# module can do — which is what catches a root nobody remembered to patch,
# rather than merely re-asserting the ones we did.
REAL_STORES = [ideas.STORE, ideatags.STORE, circuits.DEFS]


def png_bytes(w=8, h=8):
    """A real, decodable 1-colour PNG. Written by hand rather than pulled
    from Pillow so the sniffer and the thumbnail path are exercised against
    genuine bytes on a machine with no image fixtures checked in."""
    def chunk(tag, data):
        body = tag + data
        return (len(data).to_bytes(4, "big") + body
                + zlib.crc32(body).to_bytes(4, "big"))
    raw = b"".join(b"\x00" + b"\x7f\x7f\x7f" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", w.to_bytes(4, "big") + h.to_bytes(4, "big")
                    + bytes([8, 2, 0, 0, 0]))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def b64(blob):
    return base64.b64encode(blob).decode()


class ImageCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.dir = root / "idea-images"
        self.ideas_store = root / "ideas.json"
        self.ideas_store.write_text(json.dumps({"items": [], "projects": []}),
                                    encoding="utf-8")
        self.tags_store = root / "idea-index.json"
        # circuits.DEFS is the FOURTH root, and the one that got away:
        # flows.flow_for_idea saves a real Flow, so the Build-in-Flows cases
        # were writing "plain idea" into the owner's own Forge list on every
        # suite run. The merge protocol re-runs this suite in the LIVE tree,
        # so an unpatched store here is a permanent junk entry there.
        self.flows_store = root / "circuits.json"
        for p in (mock.patch.object(ideaimages, "DIR", self.dir),
                  mock.patch.object(ideaimages, "THUMBS", self.dir / ".thumbs"),
                  mock.patch.object(ideas, "STORE", self.ideas_store),
                  mock.patch.object(ideatags, "STORE", self.tags_store),
                  mock.patch.object(circuits, "DEFS", self.flows_store)):
            p.start()
            self.addCleanup(p.stop)

    def new_idea(self, text="a bug"):
        return ideas.add(text)

    def attach(self, it, name="shot.png", blob=None, derive=False):
        return ideaimages.attach(it["id"], name, b64(blob or png_bytes()),
                                 derive=derive)

    def reload(self, idea_id):
        return next(i for i in ideas.list_items() if i["id"] == idea_id)


class Isolation(ImageCase):
    def test_no_real_store_is_touched_by_a_full_run(self):
        """The guard, and it is deliberately written to catch a root NOBODY
        REMEMBERED — the earlier version only asserted the globals setUp
        already patched, which is structurally incapable of noticing a new
        one. It missed circuits.DEFS for exactly that reason: the
        Build-in-Flows cases wrote real Flows into the owner's Forge list on
        every run, and because the merge protocol re-runs this suite in the
        LIVE tree, that junk would have been permanent.

        So this drives everything the module can do and compares the real
        stores' BYTES across it."""
        before = {p: (p.read_bytes() if p.exists() else None)
                  for p in REAL_STORES}
        it = self.new_idea("a full exercise")
        meta = self.attach(it)
        # A bare Mock would return a Mock from vision_ocr and blow up on
        # len(); the backend has to answer in the shape the ladder reads.
        lm = mock.Mock()
        lm.ocr_available.return_value = True
        lm.vision_ocr.return_value = "some words off the screenshot"
        with mock.patch("server.localmodels", lm):
            ideaimages.reread(it["id"], meta["id"])
        ideaimages.thumbnail(it["id"], meta["id"])
        from server import flows
        flows.flow_for_idea(it["id"])
        ideaimages.detach(it["id"], meta["id"])
        ideas.remove(it["id"])
        for p in REAL_STORES:
            self.assertEqual(p.read_bytes() if p.exists() else None,
                             before[p], f"a real store was written: {p}")

    def test_the_bytes_land_inside_the_fixture(self):
        it = self.new_idea()
        self.attach(it)
        found = [p for p in Path(self.tmp.name).rglob("img_*") if p.is_file()]
        self.assertEqual(len(found), 1)


class Sniffing(ImageCase):
    def test_recognises_each_supported_format(self):
        cases = {
            "image/png": png_bytes(),
            "image/jpeg": b"\xff\xd8\xff\xe0" + b"\x00" * 20,
            "image/gif": b"GIF89a" + b"\x00" * 20,
            "image/webp": b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 12,
            "image/heic": b"\x00\x00\x00\x18ftypheic" + b"\x00" * 12,
        }
        for want, blob in cases.items():
            self.assertEqual(ideaimages.sniff(blob), want, want)

    def test_a_non_image_is_not_an_image(self):
        for blob in (b"%PDF-1.7\n%\xe2\xe3", b"<html>hi</html>",
                     b"PK\x03\x04zipzip", b""):
            self.assertIsNone(ideaimages.sniff(blob))

    def test_the_declared_type_never_decides(self):
        """The client's filename and mime are a request, not a fact — these
        bytes are served back to a browser, so only the bytes count."""
        it = self.new_idea()
        with self.assertRaises(ValueError) as cm:
            ideaimages.attach(it["id"], "screenshot.png", b64(b"%PDF-1.7 nope"))
        self.assertIn("not an image", str(cm.exception))


class Attaching(ImageCase):
    def test_manifest_rides_the_idea_and_bytes_land_on_disk(self):
        it = self.new_idea()
        meta = self.attach(it, "Bug Report.png")
        row = self.reload(it["id"])["images"][0]
        self.assertEqual(row["id"], meta["id"])
        self.assertEqual(row["mime"], "image/png")
        self.assertEqual(row["name"], "Bug Report.png")
        self.assertTrue(Path(meta["path"]).is_file())
        self.assertEqual(Path(meta["path"]).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_a_data_url_is_accepted_whole(self):
        """A browser FileReader yields one; making every caller strip the
        prefix is a rule someone eventually forgets."""
        it = self.new_idea()
        ideaimages.attach(it["id"], "x.png",
                          "data:image/png;base64," + b64(png_bytes()),
                          derive=False)
        self.assertEqual(len(self.reload(it["id"])["images"]), 1)

    def test_rejections_read_as_sentences(self):
        it = self.new_idea()
        for payload, want in ((b64(b""), "empty"),
                              ("!!!not base64!!!", "base64")):
            with self.assertRaises(ValueError) as cm:
                ideaimages.attach(it["id"], "x.png", payload)
            self.assertIn(want, str(cm.exception))

    def test_oversize_is_refused_with_the_cap_named(self):
        it = self.new_idea()
        big = png_bytes() + b"\x00" * (ideaimages.MAX_UPLOAD + 1)
        with self.assertRaises(ValueError) as cm:
            ideaimages.attach(it["id"], "big.png", b64(big))
        self.assertIn("the cap is", str(cm.exception))

    def test_a_full_idea_refuses_the_next_one(self):
        it = self.new_idea()
        for _ in range(ideaimages.MAX_PER_IDEA):
            self.attach(it)
        with self.assertRaises(ValueError) as cm:
            self.attach(it)
        self.assertIn("remove one first", str(cm.exception))

    def test_an_unknown_idea_raises_keyerror(self):
        with self.assertRaises(KeyError):
            ideaimages.attach("idea_nope", "x.png", b64(png_bytes()))

    def test_a_failed_manifest_write_leaves_no_orphan_bytes(self):
        """Bytes no manifest names are invisible forever. The file write
        comes first, so its cleanup has to be real."""
        it = self.new_idea()
        with mock.patch.object(ideas, "attach_image",
                               side_effect=OSError("store is gone")):
            with self.assertRaises(OSError):
                self.attach(it)
        self.assertEqual([p for p in (self.dir / it["id"]).glob("img_*")], [])

    def test_a_hostile_idea_id_cannot_escape_the_store(self):
        for bad in ("../../etc", "idea_a/../../b", "", "notanidea"):
            with self.assertRaises(ValueError):
                ideaimages.idea_dir(bad)


class Removing(ImageCase):
    def test_detach_drops_the_row_and_the_file(self):
        it = self.new_idea()
        meta = self.attach(it)
        ideaimages.detach(it["id"], meta["id"])
        self.assertEqual(self.reload(it["id"])["images"], [])
        self.assertFalse(Path(meta["path"]).exists())

    def test_detaching_an_unknown_image_raises(self):
        it = self.new_idea()
        with self.assertRaises(KeyError):
            ideaimages.detach(it["id"], "img_nothere")

    def test_purge_takes_the_whole_directory(self):
        it = self.new_idea()
        self.attach(it)
        self.attach(it)
        ideaimages.purge(it["id"])
        self.assertFalse((self.dir / it["id"]).exists())

    def test_purge_of_a_bad_id_is_quiet(self):
        ideaimages.purge("../../etc")          # must not raise, must not walk


class Serving(ImageCase):
    def test_file_path_resolves_and_mime_is_resniffed(self):
        it = self.new_idea()
        meta = self.attach(it)
        p = ideaimages.file_path(it["id"], meta["id"])
        self.assertEqual(p, Path(meta["path"]))
        self.assertEqual(ideaimages.mime_of(p), "image/png")

    def test_a_bad_image_id_serves_nothing(self):
        it = self.new_idea()
        self.attach(it)
        for bad in ("../../../etc/passwd", "img_../x", "nope", ""):
            self.assertIsNone(ideaimages.file_path(it["id"], bad))

    def test_thumbnail_is_a_jpeg_and_is_cached(self):
        """The cache assertion is on the WORK, not the path: the path is
        derived from the ids and is identical whether or not the mtime
        short-circuit fired, so comparing paths would stay green with the
        cache check deleted."""
        it = self.new_idea()
        meta = self.attach(it)
        t = ideaimages.thumbnail(it["id"], meta["id"])
        self.assertIsNotNone(t)
        self.assertEqual(t.read_bytes()[:3], b"\xff\xd8\xff")
        # Not side_effect: thumbnail() catches broadly and would fall
        # through to sips, regenerate, and pass anyway. Assert it was never
        # reached instead.
        with mock.patch("PIL.Image.open") as opened:
            self.assertEqual(ideaimages.thumbnail(it["id"], meta["id"]), t)
        opened.assert_not_called()

    def test_an_unrenderable_image_yields_no_thumbnail_rather_than_raising(self):
        """The caller serves the original in that case, so None is the
        contract — never an exception into a FileResponse."""
        it = self.new_idea()
        meta = self.attach(it)
        Path(meta["path"]).write_bytes(b"\x89PNG\r\n\x1a\ncorrupted")
        with mock.patch.object(ideaimages.settings, "IS_MAC", False):
            self.assertIsNone(ideaimages.thumbnail(it["id"], meta["id"]))


class Reading(ImageCase):
    """The ladder: OCR first because a bug report is a screenshot and a
    screenshot is mostly words; caption only when OCR comes back thin."""

    def fake_models(self, ocr="", caption=None, has_ocr=True):
        """Patch the ATTRIBUTE on the server package, not sys.modules.
        derive_text does `from . import localmodels` inside the function,
        and that reads getattr(server, "localmodels") when the submodule is
        already imported — which it always is here — so a sys.modules entry
        would be bypassed and the test would silently exercise the real
        Apple Vision backend."""
        m = mock.Mock()
        m.ocr_available.return_value = has_ocr
        m.vision_ocr.return_value = ocr
        m.ollama_caption.return_value = caption
        return mock.patch("server.localmodels", m), m

    def test_ocr_wins_when_it_reads_something_substantial(self):
        patch, m = self.fake_models(ocr="Traceback: unable to open database",
                                    caption="a screenshot")
        with patch:
            text, src = ideaimages.derive_text("/x.png")
        self.assertEqual(src, "ocr")
        self.assertIn("Traceback", text)
        m.ollama_caption.assert_not_called()

    def test_thin_ocr_falls_through_to_the_caption(self):
        patch, _ = self.fake_models(ocr="OK", caption="A photo of a whiteboard "
                                                      "covered in boxes.")
        with patch:
            text, src = ideaimages.derive_text("/x.png")
        self.assertEqual(src, "caption")
        self.assertIn("whiteboard", text)

    def test_a_machine_with_neither_backend_says_none(self):
        patch, _ = self.fake_models(has_ocr=False, caption=None)
        with patch:
            self.assertEqual(ideaimages.derive_text("/x.png"), ("", "none"))

    def test_a_raising_backend_never_propagates(self):
        m = mock.Mock()
        m.ocr_available.side_effect = RuntimeError("Vision exploded")
        m.ollama_caption.side_effect = RuntimeError("ollama exploded")
        with mock.patch("server.localmodels", m):
            self.assertEqual(ideaimages.derive_text("/x.png"), ("", "none"))

    def test_reread_stamps_the_manifest_without_bumping_updated(self):
        """Vira's own background read is not the owner touching the idea;
        letting it move `updated` would reorder the Last-updated sort
        minutes after an upload."""
        it = self.new_idea()
        meta = self.attach(it)
        before = self.reload(it["id"])["updated"]
        patch, _ = self.fake_models(ocr="the login screen is blank and grey")
        with patch:
            ideaimages.reread(it["id"], meta["id"])
        row = self.reload(it["id"])
        self.assertIn("login screen", row["images"][0]["text"])
        self.assertEqual(row["images"][0]["text_source"], "ocr")
        self.assertEqual(row["updated"], before)

    def test_derived_text_is_capped(self):
        it = self.new_idea()
        meta = self.attach(it)
        patch, _ = self.fake_models(ocr="x" * 9000)
        with patch:
            ideaimages.reread(it["id"], meta["id"])
        self.assertEqual(len(self.reload(it["id"])["images"][0]["text"]), 4000)


class PromptBlock(ImageCase):
    def test_no_images_is_no_block(self):
        self.assertEqual(ideaimages.prompt_block({"id": "idea_x"}), "")

    def test_the_agent_is_given_absolute_paths(self):
        """The whole point: the model opens the real pixels rather than
        reading a description of them."""
        it = self.new_idea()
        meta = self.attach(it)
        block = ideaimages.prompt_block(self.reload(it["id"]))
        self.assertIn(meta["path"], block)
        self.assertTrue(Path(meta["path"]).is_absolute())
        self.assertIn("file-reading tool", block)
        self.assertIn("attached 1 image to this idea", block)

    def test_the_read_rides_along_as_a_hint(self):
        it = self.new_idea()
        meta = self.attach(it)
        ideas.update_image(it["id"], meta["id"], text="Queue is empty",
                           text_source="ocr")
        block = ideaimages.prompt_block(self.reload(it["id"]))
        self.assertIn("text Vira read off it: Queue is empty", block)

    def test_a_missing_file_is_left_out_rather_than_named(self):
        """A path the agent cannot open is worse than no path: it sends the
        session looking for a file that is not there."""
        it = self.new_idea()
        meta = self.attach(it)
        Path(meta["path"]).unlink()
        self.assertEqual(ideaimages.prompt_block(self.reload(it["id"])), "")

    def test_images_of_reports_a_missing_file_honestly(self):
        it = self.new_idea()
        meta = self.attach(it)
        Path(meta["path"]).unlink()
        row = ideaimages.images_of(self.reload(it["id"]))[0]
        self.assertTrue(row["missing"])
        self.assertEqual(row["path"], "")


class FoldsIntoTheIdea(ImageCase):
    """A screenshot-only idea has to tag, embed and dedupe like any other,
    which means what Vira read off the image counts as the idea's text."""

    def test_item_text_folds_the_image_read_in(self):
        it = {"id": "idea_a", "text": "this is broken",
              "images": [{"id": "img_1", "text": "Reader window: 404 not found"}]}
        folded = ideatags.item_text(it)
        self.assertIn("this is broken", folded)
        self.assertIn("404 not found", folded)

    def test_an_idea_with_no_images_is_unchanged(self):
        it = {"id": "idea_a", "text": "plain idea"}
        self.assertEqual(ideatags.item_text(it), "plain idea")

    def test_an_unread_image_adds_nothing(self):
        it = {"id": "idea_a", "text": "plain idea",
              "images": [{"id": "img_1", "text": ""}]}
        self.assertEqual(ideatags.item_text(it), "plain idea")

    def test_attaching_an_image_makes_the_idea_stale_for_retagging(self):
        """The content hash reads through item_text, so a new screenshot
        re-tags its idea exactly as editing the words would."""
        plain = {"id": "idea_a", "text": "this is broken"}
        withimg = dict(plain, images=[{"id": "img_1", "text": "stack trace"}])
        self.assertNotEqual(ideatags._hash(ideatags.item_text(plain)),
                            ideatags._hash(ideatags.item_text(withimg)))

    def test_tokens_reach_words_that_exist_only_in_the_screenshot(self):
        it = {"id": "idea_a", "text": "broken",
              "images": [{"id": "img_1", "text": "unable to open database"}]}
        self.assertIn("database", ideatags._tokens(it))


class EveryDispatchCarriesThem(ImageCase):
    """The half-applied failure this guards against: attach a screenshot,
    hit "Build in Flows" or "Approve & build", and the agent receives the
    words with the evidence stripped off — silently, because a prompt with
    no image block looks exactly like an idea that never had one.

    The browser composes the Plan/Implement/Export prompts (app.js
    ideaImageBlock) and is covered by its own path; these two turn an idea
    into a run input SERVER-side and are the ones a test has to hold."""

    def test_build_in_flows_carries_the_image_paths(self):
        from server import flows
        it = self.new_idea("the Reader shows a blank page")
        meta = self.attach(it)
        out = flows.flow_for_idea(it["id"])
        self.assertIn(meta["path"], out["input"])
        self.assertIn("the Reader shows a blank page", out["input"])
        # The NAME stays the owner's words — a path in the Flow title would
        # be unreadable in the list.
        self.assertNotIn(meta["path"], out["name"])

    def test_a_flow_from_an_imageless_idea_is_unchanged(self):
        from server import flows
        it = self.new_idea("plain idea")
        self.assertEqual(flows.flow_for_idea(it["id"])["input"], "plain idea")

    def test_approve_and_build_carries_the_image_paths(self):
        from fastapi.testclient import TestClient
        from server import circuits, ideatags, main
        it = ideas.add("this screen is wrong", status="proposed")
        meta = self.attach(it)
        with mock.patch.object(ideatags, "STORE",
                               Path(self.tmp.name) / "idea-index.json"), \
             mock.patch.object(circuits, "start_run",
                               return_value={"id": "run_abc123"}) as run:
            r = TestClient(main.app).post(f"/api/ideas/{it['id']}/approve",
                                          json={"build": True})
        self.assertEqual(r.status_code, 200)
        self.assertIn(meta["path"], run.call_args.args[1])
        self.assertIn("this screen is wrong", run.call_args.args[1])


class StoreMutators(ImageCase):
    """ideas.py owns the store, so the manifest writes live there — one
    lock, one writer. These pin the contract ideaimages depends on."""

    def test_attach_detach_round_trip(self):
        it = self.new_idea()
        ideas.attach_image(it["id"], {"id": "img_1", "name": "a.png"})
        ideas.attach_image(it["id"], {"id": "img_2", "name": "b.png"})
        self.assertEqual(
            [i["id"] for i in self.reload(it["id"])["images"]],
            ["img_1", "img_2"])
        ideas.detach_image(it["id"], "img_1")
        self.assertEqual(
            [i["id"] for i in self.reload(it["id"])["images"]], ["img_2"])

    def test_update_image_only_writes_known_fields(self):
        it = self.new_idea()
        ideas.attach_image(it["id"], {"id": "img_1", "name": "a.png"})
        ideas.update_image(it["id"], "img_1", text="hi", text_source="ocr",
                           mime="image/evil", bytes=-1)
        row = self.reload(it["id"])["images"][0]
        self.assertEqual((row["text"], row["text_source"]), ("hi", "ocr"))
        self.assertNotIn("mime", row)

    def test_unknown_ids_raise_keyerror(self):
        it = self.new_idea()
        with self.assertRaises(KeyError):
            ideas.attach_image("idea_nope", {"id": "img_1"})
        with self.assertRaises(KeyError):
            ideas.update_image(it["id"], "img_nope", text="x")


if __name__ == "__main__":
    unittest.main()
