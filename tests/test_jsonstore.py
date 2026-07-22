"""jsonstore — the shared JSON-store discipline (and its two adopters)."""
import json
import tempfile
import unittest
from pathlib import Path

from server import feedstate, jsonstore, triage


class ReadWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_missing_returns_default(self):
        self.assertEqual(jsonstore.read(self.root / "nope.json", {"a": 1}),
                         {"a": 1})

    def test_read_corrupt_returns_default(self):
        p = self.root / "bad.json"
        p.write_text("{not json")
        self.assertEqual(jsonstore.read(p, []), [])

    def test_write_atomic_bytes_match_hand_rolled(self):
        # the indent=1 ensure_ascii=False shape most stores use
        p = self.root / "sub" / "s.json"
        obj = {"b": "ü", "a": 1}
        jsonstore.write_atomic(p, obj, indent=1, ensure_ascii=False)
        self.assertEqual(p.read_text(encoding="utf-8"),
                         json.dumps(obj, indent=1, ensure_ascii=False))
        self.assertFalse(p.with_name(p.name + ".tmp").exists())

    def test_write_atomic_newline_and_sort_keys(self):
        # the picks.json shape: indent=2, sort_keys, trailing newline
        p = self.root / "picks.json"
        jsonstore.write_atomic(p, {"z": 1, "a": 2}, newline=True,
                               indent=2, sort_keys=True)
        self.assertEqual(p.read_text(encoding="utf-8"),
                         json.dumps({"z": 1, "a": 2}, indent=2,
                                    sort_keys=True) + "\n")

    def test_mutate_returns_written_state(self):
        p = self.root / "m.json"

        def fn(s):
            s["n"] = s.get("n", 0) + 1

        self.assertEqual(jsonstore.mutate(p, fn, {}), {"n": 1})
        self.assertEqual(jsonstore.mutate(p, fn, {}), {"n": 2})
        self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"n": 2})

    def test_mutate_replacement_return(self):
        p = self.root / "r.json"
        out = jsonstore.mutate(p, lambda s: {"fresh": True}, {"old": 1})
        self.assertEqual(out, {"fresh": True})
        self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"fresh": True})

    def test_prune_oldest(self):
        bucket = {"a": 3, "b": 1, "c": 2, "d": 4}
        jsonstore.prune_oldest(bucket, 2)
        self.assertEqual(bucket, {"a": 3, "d": 4})
        jsonstore.prune_oldest(bucket, 2)  # at cap: untouched
        self.assertEqual(bucket, {"a": 3, "d": 4})


class FeedStateAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._store = feedstate.STATE
        feedstate.STATE = Path(self.tmp.name) / "feed-state.json"

    def tearDown(self):
        feedstate.STATE = self._store
        self.tmp.cleanup()

    def test_set_state_and_annotate_fresh_read(self):
        out = feedstate.set_state(101, read=True)
        self.assertEqual(out, {"rowid": "101", "read": True, "hidden": False})
        items = [{"rowid": 101}, {"rowid": 102}]
        feedstate.annotate(items)
        self.assertTrue(items[0]["read"])
        self.assertFalse(items[1]["read"])
        # external write is seen immediately (no module cache)
        feedstate.STATE.write_text(json.dumps({"read": {}, "hidden": {}}))
        feedstate.annotate(items)
        self.assertFalse(items[0]["read"])

    def test_hidden_toggle_and_read_all(self):
        feedstate.set_state("mail-a-1", hidden=True)
        self.assertTrue(feedstate.set_state("mail-a-1")["hidden"])
        self.assertFalse(
            feedstate.set_state("mail-a-1", hidden=False)["hidden"])
        self.assertEqual(feedstate.read_all([1, 2, 3])["read_count"], 3)

    def test_on_disk_shape_unchanged(self):
        feedstate.set_state(7, read=True)
        doc = json.loads(feedstate.STATE.read_text(encoding="utf-8"))
        self.assertEqual(set(doc), {"read", "hidden"})
        self.assertIn("7", doc["read"])


class TriageDismissAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._store = triage.STATE
        triage.STATE = Path(self.tmp.name) / "triage-state.json"

    def tearDown(self):
        triage.STATE = self._store
        self.tmp.cleanup()

    def test_dismiss_byte_compatible(self):
        triage.dismiss("+12125550142")
        triage.dismiss("a@example.com")
        expect = json.dumps(
            {"dismissed": sorted({"+12125550142", "a@example.com"})},
            indent=1)
        self.assertEqual(triage.STATE.read_text(encoding="utf-8"), expect)
        self.assertEqual(triage._dismissed(),
                         {"+12125550142", "a@example.com"})

    def test_dismissed_missing_file(self):
        self.assertEqual(triage._dismissed(), set())


if __name__ == "__main__":
    unittest.main()
