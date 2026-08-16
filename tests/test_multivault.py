"""Multiple connected knowledge vaults: federation without write ambiguity."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import onboard, settings, vault


class MultiVaultIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.primary = root / "primary"
        self.extra = root / "research"
        (self.primary / "wiki").mkdir(parents=True)
        self.extra.mkdir()
        (self.primary / "wiki" / "shared.md").write_text(
            "# Primary shared\nThe primary write target.\n", encoding="utf-8")
        (self.primary / "wiki" / "primary-only.md").write_text(
            "# Primary only\nA cedar porch.\n", encoding="utf-8")
        (self.extra / "shared.md").write_text(
            "# Research shared\nBig science can create abundance and clean water.\n",
            encoding="utf-8")
        (self.extra / "extra-only.md").write_text(
            "# Extra only\nTrustworthy frontier research at scale.\n",
            encoding="utf-8")
        (self.extra / "chart.png").write_bytes(b"not-a-real-png")

        original_get = settings.get

        def configured(key):
            if key == "vault_sources":
                return [{"id": "research", "name": "Research",
                         "root": str(self.extra)}]
            return original_get(key)

        for patcher in (
                mock.patch.object(vault, "DB_PATH", root / "primary.sqlite"),
                mock.patch.object(vault, "vault_root",
                                  return_value=self.primary),
                mock.patch.object(vault, "vault_dirs", return_value=["wiki"]),
                mock.patch.object(settings, "get", side_effect=configured)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self._reset()
        self.addCleanup(self._reset)

    @staticmethod
    def _reset():
        vault._active.update(key=None, vault=None, rows=[])
        vault._vec_state.update(gen=-1, ids=None, mat=None)
        vault._extra_vec_states.clear()
        vault._stem_cache["key"] = None
        vault._extra_stem_caches.clear()

    def test_scan_search_and_status_federate(self):
        scanned = vault.scan_once()
        self.assertEqual(scanned["changed"], 4)
        hits = vault.search("abundance clean water", limit=5)
        self.assertEqual(hits[0]["path"], "@research/shared.md")
        self.assertEqual(hits[0]["vault_name"], "Research")
        literal = vault.grep_notes("frontier research")
        self.assertEqual(literal[0]["path"], "@research/extra-only.md")
        state = vault.status()
        self.assertEqual(state["notes"], 4)
        self.assertEqual([row["id"] for row in state["vaults"]],
                         ["primary", "research"])

    def test_source_paths_read_notes_assets_and_reject_traversal(self):
        vault.scan_once()
        self.assertIn("abundance", vault.note_text("@research/shared.md"))
        self.assertEqual(vault.asset_path("@research/chart.png"),
                         (self.extra / "chart.png").resolve())
        with self.assertRaises(ValueError):
            vault.note_text("@research/../../outside.md")
        self.assertIsNone(vault.asset_path("@research/../../outside.png"))
        with self.assertRaises(ValueError):
            vault.note_text("@unknown/shared.md")

    def test_wikilinks_prefer_the_note_source(self):
        vault.scan_once()
        self.assertEqual(vault.resolve_ref("shared")["path"],
                         "wiki/shared.md")
        self.assertEqual(
            vault.resolve_ref("shared", from_path="@research/extra-only.md")
            ["path"], "@research/shared.md")

    def test_ask_validates_a_secondary_vault_citation(self):
        vault.scan_once()
        answer = "The goal appears in [[@research/shared.md]]."
        hits = vault.search("abundance clean water", limit=5)
        with mock.patch("server.suggest.complete", return_value=answer):
            out = vault.ask("What is the abundance goal?", hits=hits)
        self.assertEqual([c["path"] for c in out["citations"]],
                         ["@research/shared.md"])


class VaultSourceConfigTests(unittest.TestCase):
    def test_add_update_and_disconnect_never_touch_source_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "primary"
            extra = root / "extra"
            primary.mkdir()
            extra.mkdir()
            note = extra / "keep.md"
            note.write_text("# Keep\n", encoding="utf-8")
            cfg = root / "config.json"
            cfg.write_text(json.dumps({"vault_root": str(primary)}),
                           encoding="utf-8")
            with mock.patch.object(settings, "CONFIG_PATH", cfg):
                added = onboard.vault_source_set(str(extra), "Work research")
                updated = onboard.vault_source_set(
                    str(extra), "Research archive", added["id"])
                removed = onboard.vault_source_remove(added["id"])
                source_survived = note.exists()
            saved = json.loads(cfg.read_text(encoding="utf-8"))
        self.assertEqual(added["id"], "work-research")
        self.assertEqual(updated["name"], "Research archive")
        self.assertTrue(removed["removed"])
        self.assertEqual(saved["vault_sources"], [])
        self.assertTrue(source_survived)

    def test_primary_cannot_be_added_as_a_secondary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "config.json"
            cfg.write_text(json.dumps({"vault_root": str(root)}),
                           encoding="utf-8")
            with mock.patch.object(settings, "CONFIG_PATH", cfg):
                with self.assertRaises(ValueError):
                    onboard.vault_source_set(str(root), "Duplicate")

    def test_nested_vault_cannot_duplicate_the_primary_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "notes"
            nested.mkdir()
            cfg = root / "config.json"
            cfg.write_text(json.dumps({"vault_root": str(root)}),
                           encoding="utf-8")
            with mock.patch.object(settings, "CONFIG_PATH", cfg):
                with self.assertRaises(ValueError):
                    onboard.vault_source_set(str(nested), "Nested")


class VaultSourceRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from server import main
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    @mock.patch("server.main.onboard.vault_source_set",
                return_value={"id": "work", "name": "Work", "notes": 2})
    def test_add_source_route(self, add):
        response = self.client.post("/api/vault/sources", json={
            "path": "/notes/work", "name": "Work",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "work")
        add.assert_called_once_with("/notes/work", "Work", None)

    @mock.patch("server.main.onboard.vault_source_remove",
                return_value={"id": "work", "removed": True})
    def test_disconnect_source_route(self, remove):
        response = self.client.delete("/api/vault/sources/work")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["removed"])
        remove.assert_called_once_with("work")

    @mock.patch("server.main.vault.resolve_ref",
                return_value={"path": "@work/index.md", "exact": True})
    def test_resolve_route_passes_source_context(self, resolve):
        response = self.client.get("/api/vault/resolve", params={
            "ref": "index", "from_path": "@work/topic.md",
        })
        self.assertEqual(response.status_code, 200)
        resolve.assert_called_once_with("index", from_path="@work/topic.md")


if __name__ == "__main__":
    unittest.main()
