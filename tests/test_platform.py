"""Mac-only seams must degrade, never crash, off-Mac (P0 portable core).

Each test forces the "wrong platform" or "store missing" condition and
asserts the module answers with its honest fallback — the contract that
lets one codebase boot on a Mac with everything, a Mac before Full Disk
Access, and a Windows/Linux machine with none of the Apple stores.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import atlas, brief, localmodels, mediaindex, send, settings


class BriefCalendarFailSoftTests(unittest.TestCase):
    """The pre-FDA 502: an unreachable Calendar store used to raise out of
    _occurrences and kill the whole brief. It must yield an empty section
    with the reason named."""

    def setUp(self):
        brief._cal_error = None
        self.addCleanup(setattr, brief, "_cal_error", None)

    def test_missing_store_is_empty_not_an_exception(self):
        with mock.patch.object(brief, "CAL_DB",
                               Path("/nonexistent/Calendar.sqlitedb")):
            rows = brief._occurrences(*brief._day_bounds(0))
        self.assertEqual(rows, [])
        self.assertIn("no local calendar store", brief._cal_error)

    def test_unreadable_store_names_full_disk_access(self):
        # An existing file that is not a database: both ro connects open
        # (sqlite defers reads), the query then fails — the FDA shape.
        with tempfile.NamedTemporaryFile(suffix=".sqlitedb") as fh:
            fh.write(b"not a database")
            fh.flush()
            with mock.patch.object(brief, "CAL_DB", Path(fh.name)):
                rows = brief._occurrences(*brief._day_bounds(0))
        self.assertEqual(rows, [])
        self.assertIn("calendar store unreadable", brief._cal_error)

    def test_calendar_payload_carries_the_error(self):
        with mock.patch.object(brief, "CAL_DB",
                               Path("/nonexistent/Calendar.sqlitedb")), \
             mock.patch.object(brief, "_m365_events",
                               return_value={"today": [], "tomorrow": [],
                                             "status": ""}):
            cal = brief._calendar()
        self.assertFalse(cal["available"])
        self.assertTrue(cal["error"])
        self.assertEqual(cal["today"], [])


class SendPlatformGateTests(unittest.TestCase):
    def test_non_mac_send_refuses_with_a_named_reason(self):
        with mock.patch.object(send.settings, "IS_MAC", False):
            with self.assertRaises(RuntimeError) as ctx:
                send.send_imessage("hi", handle="owner@example.com")
        self.assertIn("macOS", str(ctx.exception))


class OcrFallbackTests(unittest.TestCase):
    def test_ocr_unavailable_off_mac(self):
        with mock.patch.object(localmodels.settings, "IS_MAC", False):
            self.assertFalse(localmodels.ocr_available())

    def test_ocr_stage_skips_resumably_when_unavailable(self):
        # The stage must return before touching the index db — items keep
        # ocr='' so a future backend still finds them (never stamped
        # "ran, empty" on a platform where OCR never ran).
        logs = []
        with mock.patch.object(localmodels, "ocr_available",
                               return_value=False), \
             mock.patch.object(mediaindex, "_db",
                               side_effect=AssertionError("db touched")):
            n = mediaindex.backfill_ocr(log=logs.append)
        self.assertEqual(n, 0)
        self.assertTrue(any("skipped" in s for s in logs))


class AtlasFaceFallbackTests(unittest.TestCase):
    def test_sips_crop_declines_off_mac(self):
        with mock.patch.object(atlas.settings, "IS_MAC", False):
            out = atlas._sips_crop(Path("/tmp/x.jpg"), [0, 0, 100, 100],
                                   Path("/tmp/out.jpg"))
        self.assertIsNone(out)

    def test_sips_dims_absent_tool_is_none(self):
        with mock.patch.object(atlas.subprocess, "run",
                               side_effect=FileNotFoundError("sips")):
            self.assertIsNone(atlas._sips_dims(Path("/tmp/x.jpg")))


class PlatformConstantsTests(unittest.TestCase):
    def test_constants_exist_and_are_bool(self):
        self.assertIsInstance(settings.IS_MAC, bool)
        self.assertIsInstance(settings.IS_WIN, bool)
        self.assertFalse(settings.IS_MAC and settings.IS_WIN)


if __name__ == "__main__":
    unittest.main()
