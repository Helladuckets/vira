"""Document thumbnails: dormant without a browser, derived with mtime
invalidation, joined at the route layer, films excluded.

The fake browser follows the shebang-is-not-a-binary rule: a plain .py
payload behind a /bin/sh shim (POSIX) or a .cmd shim (Windows), so the
capture contract is exercised on both CI platforms.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import docthumbs, readinglist, settings

PAYLOAD = r"""
import sys
shot = next(a for a in sys.argv if a.startswith("--screenshot="))
path = shot.split("=", 1)[1]
# a minimal real PNG header so a downscaler that inspects it does not choke
open(path, "wb").write(bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000000"))
"""


def fake_browser(root: Path) -> str:
    payload = root / "fakebrowser.py"
    payload.write_text(PAYLOAD, encoding="utf-8")
    if os.name == "nt":
        shim = root / "fakebrowser.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{payload}" %*\r\n',
                        encoding="utf-8")
    else:
        shim = root / "fakebrowser.sh"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{payload}" "$@"\n',
                        encoding="utf-8")
        shim.chmod(0o755)
    return str(shim)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.docs = root / "static" / "docs"
        self.docs.mkdir(parents=True)
        self.thumbs = root / "doc-thumbs"
        self.cfg = {"thumb_browser": fake_browser(root)}
        patches = (
            mock.patch.object(readinglist, "STORE", root / "reading-list.json"),
            mock.patch.object(readinglist, "ROOT", root),
            mock.patch.object(readinglist, "WALKTHROUGH_DIR", root / "wt"),
            mock.patch.object(docthumbs, "THUMB_DIR", self.thumbs),
            mock.patch.object(settings, "raw", side_effect=lambda: dict(self.cfg)),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def reg(self, title, rel="a.html", kind="plan", body="<title>x</title>"):
        (self.docs / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.docs / rel).write_text(body, encoding="utf-8")
        return readinglist.register(title, kind, "/docs/" + rel)


class DormancyTests(Base):
    def test_no_browser_is_dormant_never_an_error(self):
        self.cfg = {}
        with mock.patch.object(docthumbs.shutil, "which", return_value=None), \
             mock.patch.object(docthumbs, "BROWSER_PATHS", ()):
            self.assertIsNone(docthumbs.browser())
            self.assertTrue(docthumbs.sweep()["dormant"])
            self.assertFalse(docthumbs.status()["browser"])

    def test_a_configured_path_that_does_not_exist_is_dormant(self):
        self.cfg = {"thumb_browser": str(Path(self.tmp.name) / "gone")}
        self.assertIsNone(docthumbs.browser())


class CaptureTests(Base):
    def test_sweep_captures_an_html_document(self):
        it = self.reg("Plan A")
        r = docthumbs.sweep()
        self.assertEqual(r["made"], 1)
        self.assertTrue(docthumbs.thumb_file(it))

    def test_films_are_excluded_they_carry_their_own_frames(self):
        wt = Path(self.tmp.name) / "wt" / "film-2026-08-01"
        wt.mkdir(parents=True)
        (wt / "index.html").write_text("<title>f</title>", encoding="utf-8")
        it = readinglist.register("Film", "walkthrough",
                                  "/walkthroughs/film-2026-08-01/")
        self.assertIsNone(docthumbs.eligible(it))
        self.assertEqual(docthumbs.sweep()["made"], 0)

    def test_a_rerendered_source_invalidates_the_thumb(self):
        it = self.reg("Plan A")
        docthumbs.sweep()
        first = docthumbs.thumb_file(it)
        src = self.docs / "a.html"
        os.utime(src, (time.time() + 5, time.time() + 5))
        self.assertIsNone(docthumbs.thumb_file(it))   # stale = gone
        docthumbs.sweep()
        second = docthumbs.thumb_file(it)
        self.assertTrue(second)
        self.assertNotEqual(first.name, second.name)

    def test_a_failing_browser_never_stalls_the_sweep(self):
        self.reg("Plan A")
        self.reg("Plan B", rel="b.html")
        self.cfg = {"thumb_browser": fake_browser(Path(self.tmp.name))}
        with mock.patch.object(docthumbs, "generate", return_value=False):
            r = docthumbs.sweep()
        self.assertEqual(r["made"], 0)
        self.assertEqual(r["pending"], 2)


class JoinTests(Base):
    def test_annotate_joins_by_id_and_skips_films(self):
        it = self.reg("Plan A")
        docthumbs.sweep()
        rows = [dict(it), {"id": "rl_zzzz", "kind": "plan"},
                {"id": it["id"], "kind": "walkthrough"}]
        docthumbs.annotate(rows)
        self.assertEqual(rows[0]["thumb"], "/api/reading/thumb/" + it["id"])
        self.assertNotIn("thumb", rows[1])
        self.assertNotIn("thumb", rows[2])

    def test_by_id_validates_the_id_before_touching_disk(self):
        self.assertIsNone(docthumbs.by_id("../../etc/passwd"))
        self.assertIsNone(docthumbs.by_id(""))
        self.assertIsNone(docthumbs.by_id(None))

    def test_by_id_serves_the_newest_capture(self):
        it = self.reg("Plan A")
        docthumbs.sweep()
        p = docthumbs.by_id(it["id"])
        self.assertTrue(p and p.is_file())


if __name__ == "__main__":
    unittest.main()
