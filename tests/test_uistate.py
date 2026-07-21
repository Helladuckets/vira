"""uistate — the server-persisted desktop arrangement store."""
import json
import tempfile
import unittest
from pathlib import Path

from server import uistate


class UiStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._store = uistate.STORE
        uistate.STORE = Path(self.tmp.name) / "ui-state.json"

    def tearDown(self):
        uistate.STORE = self._store
        self.tmp.cleanup()

    def test_empty_store(self):
        self.assertEqual(uistate.load(), {"keys": {}})

    def test_roundtrip_and_merge(self):
        desk = json.dumps({"feed": {"x": 10, "y": 20, "open": True}})
        uistate.save({"vira-desktop": desk})
        dock = json.dumps(["feed", "people", "palette"])
        out = uistate.save({"vira-dock-order": dock})
        # merge: the earlier key survives a later partial save
        self.assertEqual(out["keys"]["vira-desktop"], desk)
        self.assertEqual(out["keys"]["vira-dock-order"], dock)
        self.assertEqual(uistate.load(), out)

    def test_values_stored_verbatim(self):
        # opaque strings: what the browser wrote is byte-for-byte what it
        # reads back (no re-encoding seam)
        desk = '{"feed":{"x":1},"people":{"open":false}}'
        uistate.save({"vira-desktop": desk})
        self.assertEqual(uistate.load()["keys"]["vira-desktop"], desk)

    def test_unknown_keys_ignored(self):
        out = uistate.save({"vira-desktop": "{}", "evil-key": "{}"})
        self.assertNotIn("evil-key", out["keys"])
        self.assertIn("vira-desktop", out["keys"])

    def test_dock_hidden_key_persists(self):
        # the launchpad's curation set syncs like the dock order
        hidden = json.dumps(["triage", "subsviz"])
        out = uistate.save({"vira-dock-hidden": hidden})
        self.assertEqual(out["keys"]["vira-dock-hidden"], hidden)
        self.assertEqual(uistate.load()["keys"]["vira-dock-hidden"], hidden)
        # merges beside the other arrangement keys
        uistate.save({"vira-dock-order": "[]"})
        self.assertEqual(uistate.load()["keys"]["vira-dock-hidden"], hidden)

    def test_non_json_value_rejected(self):
        with self.assertRaises(ValueError):
            uistate.save({"vira-desktop": "not json"})
        self.assertEqual(uistate.load(), {"keys": {}})

    def test_non_string_value_rejected(self):
        with self.assertRaises(ValueError):
            uistate.save({"vira-desktop": {"feed": {}}})

    def test_oversize_value_rejected(self):
        big = json.dumps({"pad": "x" * uistate.MAX_VALUE_BYTES})
        with self.assertRaises(ValueError):
            uistate.save({"vira-desktop": big})

    def test_rejected_batch_writes_nothing(self):
        # one bad value in a batch means NO key from that batch lands
        uistate.save({"vira-desktop": '{"a":1}'})
        with self.assertRaises(ValueError):
            uistate.save({"vira-dock-order": "[]", "vira-desktop": "nope"})
        keys = uistate.load()["keys"]
        self.assertEqual(keys["vira-desktop"], '{"a":1}')
        self.assertNotIn("vira-dock-order", keys)

    def test_instance_id_live_without_snapshot(self):
        self.assertEqual(uistate.instance_id(), "live")

    def test_instance_id_per_clone(self):
        snap = uistate.STORE.parent / ".test-snapshot"
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text("Wed Jul 16 11:58:01 EDT 2026\n")
        first = uistate.instance_id()
        self.assertTrue(first.startswith("test-"))
        # stable across restarts of the same instance...
        self.assertEqual(uistate.instance_id(), first)
        # ...and a re-clone (new stamp) mints a new identity
        snap.write_text("Wed Jul 16 12:30:44 EDT 2026\n")
        self.assertNotEqual(uistate.instance_id(), first)

    def test_instance_id_per_sandbox_provision(self):
        # A sandbox is its own data world without being a branch clone.
        # Without a distinct id it reports "live", and a browser holding the
        # PREVIOUS sandbox's desktop on the same recycled port pushes that
        # layout back into the store `reset` just wiped.
        stamp = uistate.STORE.parent / ".instance-stamp"
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text("1784650000.123456\n")
        first = uistate.instance_id()
        self.assertTrue(first.startswith("inst-"))
        self.assertEqual(uistate.instance_id(), first)
        stamp.write_text("1784660000.654321\n")   # re-provision
        self.assertNotEqual(uistate.instance_id(), first)

    def test_test_snapshot_outranks_instance_stamp(self):
        # A branch worktree that has also been provisioned as a sandbox keeps
        # its clone identity — the data snapshot is the more specific fact.
        (uistate.STORE.parent).mkdir(parents=True, exist_ok=True)
        (uistate.STORE.parent / ".instance-stamp").write_text("1784650000.1\n")
        (uistate.STORE.parent / ".test-snapshot").write_text("Wed Jul 16 11:58:01 EDT 2026\n")
        self.assertTrue(uistate.instance_id().startswith("test-"))

    def test_instance_id_empty_snapshot_is_live(self):
        snap = uistate.STORE.parent / ".test-snapshot"
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text("")
        self.assertEqual(uistate.instance_id(), "live")

    def test_corrupt_store_recovers(self):
        uistate.STORE.parent.mkdir(parents=True, exist_ok=True)
        uistate.STORE.write_text("{{{corrupt")
        self.assertEqual(uistate.load(), {"keys": {}})
        uistate.save({"vira-dock-order": "[]"})
        self.assertEqual(uistate.load()["keys"]["vira-dock-order"], "[]")


if __name__ == "__main__":
    unittest.main()
