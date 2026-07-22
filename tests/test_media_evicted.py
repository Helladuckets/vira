"""Evicted-attachment visibility: originals macOS offloads to iCloud stay
listed in shared media (flagged, not dropped), thumbnail-cache keys survive
eviction, and the contact-photo cache refreshes when AddressBook bytes change.

Fixtures are fully synthetic (temp files, fake row tuples); nothing here
touches chat.db or the real AddressBook stores.

Run: .venv/bin/python -m unittest tests.test_media_evicted
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import media, photos

APPLE_NS = 700_000_000 * 1_000_000_000     # a plausible message date


def _att_row(rowid, path, mime):
    # (rowid, fname, mime, tname, size, from_me, date_ns, msg_rowid,
    #  mtext, mblob) — the _attachment_rows tuple shape
    return (rowid, str(path), mime, None, 123, 0, APPLE_NS,
            rowid * 10, None, None)


class EvictedMediaListing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.on_disk = Path(self.tmp.name) / "beach.jpeg"
        self.on_disk.write_bytes(b"jpegdata")
        self.gone = Path(self.tmp.name) / "evicted.heic"      # never written
        self.gone_doc = Path(self.tmp.name) / "plan.pdf"      # never written

    def _media(self):
        rows = [_att_row(1, self.on_disk, "image/jpeg"),
                _att_row(2, self.gone, "image/heic"),
                _att_row(3, self.gone_doc, "application/pdf")]
        with mock.patch.object(media, "_text_messages", return_value=[]), \
             mock.patch.object(media, "_attachment_rows", return_value=rows), \
             mock.patch.object(media, "_link_rows", return_value=[]), \
             mock.patch.object(media, "_load_meta", return_value={}):
            return media.media_for_chats([7])

    def test_evicted_items_stay_listed(self):
        out = self._media()
        self.assertEqual(len(out["photos"]), 2)
        self.assertEqual(len(out["docs"]), 1)

    def test_evicted_flag_only_on_missing(self):
        out = self._media()
        by_id = {p["id"]: p for p in out["photos"]}
        self.assertNotIn("evicted", by_id[1])
        self.assertTrue(by_id[2]["evicted"])
        self.assertTrue(out["docs"][0]["evicted"])


class DurableThumbKeys(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.thumbs = Path(self.tmp.name) / "thumbs"
        self.thumbs.mkdir()
        p = mock.patch.object(media, "THUMBS", self.thumbs)
        p.start()
        self.addCleanup(p.stop)
        self.src = Path(self.tmp.name) / "photo.heic"
        self.src.write_bytes(b"heicdata")

    def test_key_ignores_mtime(self):
        k1 = media._cache_key(self.src, "thumb480")
        os.utime(self.src, (1, 1))          # eviction/rewrite changes mtime
        self.assertEqual(k1, media._cache_key(self.src, "thumb480"))

    def test_legacy_thumb_migrates_to_durable_key(self):
        legacy = media._legacy_cache_key(self.src, "thumb480")
        legacy.write_bytes(b"thumbjpeg")
        hit = media._cached(self.src, "thumb480")
        self.assertEqual(hit, media._cache_key(self.src, "thumb480"))
        self.assertTrue(hit.exists())
        self.assertFalse(legacy.exists())   # moved, not copied

    def test_cached_thumb_survives_eviction(self):
        media._cache_key(self.src, "thumb480").write_bytes(b"thumbjpeg")
        self.src.unlink()                   # macOS evicts the original
        hit = media._cached(self.src, "thumb480")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.read_bytes(), b"thumbjpeg")

    def test_legacy_key_uncomputable_after_eviction(self):
        self.src.unlink()
        self.assertIsNone(media._legacy_cache_key(self.src, "thumb480"))


class ContactPhotoRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(photos, "CACHE", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)

    def test_rewrites_when_bytes_change(self):
        out = photos._write_cache("p_x", b"old-face")
        self.assertEqual(out.read_bytes(), b"old-face")
        out = photos._write_cache("p_x", b"new-face")
        self.assertEqual(out.read_bytes(), b"new-face")

    def test_unchanged_bytes_are_left_alone(self):
        out = photos._write_cache("p_x", b"face")
        before = out.stat().st_mtime_ns
        os.utime(out, ns=(before - 10**9, before - 10**9))
        photos._write_cache("p_x", b"face")
        self.assertEqual(out.stat().st_mtime_ns, before - 10**9)

    def test_ab_stamp_defaults_to_zero(self):
        with mock.patch.object(photos, "AB_GLOB",
                               Path(self.tmp.name) / "nope"):
            self.assertEqual(photos._ab_stamp(), 0.0)


if __name__ == "__main__":
    unittest.main()
