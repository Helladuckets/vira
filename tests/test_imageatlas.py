"""Image Atlas adapter (server/imageatlas.py) + its routes.

The engine itself is chaska's to test; these cover the ADAPTER contract:
dormancy honesty, the settings re-key, path containment on every serving
route, the passive refusals, and the viewer-facing API mirror. A tiny real
atlas is built in a tmp vault with a fake embedder — the real code path,
no torch.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from server import imageatlas

try:
    import chaska  # noqa: F401 — optional dependency; CI runs without it
    HAVE_CHASKA = True
except Exception:
    HAVE_CHASKA = False


DIMS = 16


class FakeEmbedder:
    def _vec(self, data: bytes) -> np.ndarray:
        h = hashlib.sha256(data).digest()
        v = np.frombuffer((h * ((DIMS * 4) // len(h) + 1))[: DIMS * 4], dtype=np.uint32)
        v = v.astype(np.float64) + 1.0
        return (v / np.linalg.norm(v)).astype(np.float32)

    def embed_images(self, paths):
        out = []
        for p in paths:
            try:
                if hasattr(p, "read"):
                    Image.open(p); p.seek(0)
                    out.append(self._vec(p.read()))
                else:
                    Image.open(p).close()
                    out.append(self._vec(Path(p).read_bytes()))
            except Exception:
                out.append(None)
        return out

    def embed_text(self, text):
        return self._vec(text.encode("utf-8"))


def _make_vault(root: Path) -> None:
    (root / "wiki").mkdir(parents=True)
    for i, color in enumerate([(200, 40, 40), (40, 200, 40), (40, 40, 200), (200, 200, 40)]):
        p = root / "wiki" / "assets" / "trip" / f"img{i}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (20, 16), color).save(p, "PNG")
    (root / "wiki" / "trip.md").write_text(
        "---\ntags: [travel]\nimages_anchors:\n  - trip\n---\n\n# Trip\n\n"
        "## Visuals\n- ![[img0.png]] The first shot.\n",
        encoding="utf-8")


@unittest.skipUnless(HAVE_CHASKA, "chaska not installed (optional dependency)")
class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import chaska
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "vault"
        _make_vault(cls.root)
        cls.engine = chaska.Atlas(cls.root, embedder=FakeEmbedder(),
                                  clusters=2, name="testvault")
        cls.engine.refresh(log=lambda *a: None)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.p1 = mock.patch.object(imageatlas, "atlas", return_value=self.engine)
        self.p2 = mock.patch.object(imageatlas, "vault_root", return_value=self.root)
        self.p1.start(); self.p2.start()
        self.addCleanup(self.p1.stop)
        self.addCleanup(self.p2.stop)


class StatusTest(Base):
    def test_status_available_and_counts(self):
        st = imageatlas.status()
        self.assertTrue(st["available"])
        self.assertEqual(st["items"], 4)
        self.assertEqual(st["embedded"], 4)
        self.assertTrue(st["exported"])
        self.assertFalse(st["building"])

    def test_dormant_without_vault(self):
        with mock.patch.object(imageatlas, "vault_root", return_value=None):
            st = imageatlas.status()
        self.assertFalse(st["available"])
        self.assertIn("vault_root", st["reason"])

    def test_dormant_reason_names_missing_chaska(self):
        with mock.patch.object(imageatlas, "_ChaskaAtlas", None):
            self.assertIn("chaska", imageatlas.dormant_reason())

    def test_atlas_rekeys_on_settings_change(self):
        # the real atlas() (unpatched) re-reads settings per access
        self.p1.stop()
        try:
            imageatlas._active["key"] = None
            a = imageatlas.atlas()
            self.assertEqual(a.config.root, self.root.resolve())
            b = imageatlas.atlas()
            self.assertIs(a, b)          # same key -> cached
        finally:
            imageatlas._active["key"] = None
            imageatlas._active["atlas"] = None
            self.p1.start()


class ContainmentTest(Base):
    def test_note_text_contained(self):
        self.assertIn("Trip", imageatlas.note_text("wiki/trip.md"))
        self.assertIsNone(imageatlas.note_text("../outside.md"))
        self.assertIsNone(imageatlas.note_text("wiki/assets/trip/img0.png"))
        self.assertIsNone(imageatlas.note_text("wiki/missing.md"))

    def test_contained_refuses_escape(self):
        base = self.engine.config.export_dir
        self.assertIsNone(imageatlas.contained(base, "../atlas.sqlite"))
        self.assertIsNotNone(imageatlas.contained(base, "meta.json"))


class ConfigTest(Base):
    def test_roundtrip(self):
        imageatlas.viewer_config_put("atlas-3d", {"cluster_labels": {"A": "B"}})
        self.assertEqual(imageatlas.viewer_config_get("atlas-3d"),
                         {"cluster_labels": {"A": "B"}})

    def test_put_refused_on_passive(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(PermissionError):
                imageatlas.viewer_config_put("atlas-3d", {})


class BuildTest(Base):
    def test_build_refused_on_passive(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(PermissionError):
                imageatlas.start_build()

    def test_build_refused_when_dormant(self):
        with mock.patch.object(imageatlas, "atlas", return_value=None):
            with self.assertRaises(RuntimeError):
                imageatlas.start_build()


class EmbedderSeamTest(unittest.TestCase):
    """_ViraEmbedder's late `from . import localmodels` resolves through the
    server package attribute once the real module has ever been imported, so
    the patch must land BOTH in sys.modules and on the package. localmodels'
    module import is cheap (torch loads lazily), so importing it here is safe.
    """

    def _patched(self, fake):
        import server
        import server.localmodels  # ensure the package attribute exists
        return (mock.patch.dict(sys.modules, {"server.localmodels": fake}),
                mock.patch.object(server, "localmodels", fake))

    def test_vira_embedder_speaks_the_protocol(self):
        fake = types.ModuleType("server.localmodels")
        fake.siglip_embed_images = lambda paths: [np.ones(4, dtype=np.float32)] * len(paths)
        fake.siglip_embed_text = lambda q: np.ones(4, dtype=np.float32)
        p1, p2 = self._patched(fake)
        with p1, p2:
            e = imageatlas._ViraEmbedder()
            out = e.embed_images([Path("/tmp/x.png")])
            self.assertEqual(len(out), 1)
            self.assertEqual(e.embed_text("q").shape, (4,))

    def test_backend_failure_is_none_never_raise(self):
        fake = types.ModuleType("server.localmodels")

        def boom(*a):
            raise RuntimeError("torch is gone")
        fake.siglip_embed_images = boom
        fake.siglip_embed_text = boom
        p1, p2 = self._patched(fake)
        with p1, p2:
            e = imageatlas._ViraEmbedder()
            self.assertIsNone(e.embed_images([Path("/tmp/x.png")]))
            self.assertIsNone(e.embed_text("q"))


class RouteTest(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from fastapi.testclient import TestClient
        from server import main
        cls.client = TestClient(main.app)

    def test_status_route(self):
        r = self.client.get("/api/imageatlas/status")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["available"])

    def test_viewer_and_payload_served(self):
        r = self.client.get("/imageatlas/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"importmap", r.content)
        self.assertNotIn(b"cdn.jsdelivr.net", r.content)
        r = self.client.get("/imageatlas/data/meta.json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 4)
        r = self.client.get("/imageatlas/data/coords3.bin")
        self.assertEqual(len(r.content), 4 * 3 * 4)

    def test_atlases_json_generated(self):
        r = self.client.get("/imageatlas/atlases.json")
        self.assertEqual(r.json()["default"], "local")

    def test_traversal_refused(self):
        r = self.client.get("/imageatlas/data/%2e%2e/atlas.sqlite")
        self.assertIn(r.status_code, (400, 404))
        r = self.client.get("/imageatlas/api/note", params={"path": "../x.md"})
        self.assertEqual(r.status_code, 404)

    def test_note_route(self):
        r = self.client.get("/imageatlas/api/note", params={"path": "wiki/trip.md"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Trip", r.json()["content"])

    def test_embed_route(self):
        r = self.client.post("/imageatlas/api/embed", json={"text": "a red image"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["vector"]), DIMS)
        r = self.client.post("/imageatlas/api/embed", json={})
        self.assertEqual(r.status_code, 400)

    def test_config_routes(self):
        r = self.client.put("/imageatlas/api/config/atlas-3d",
                            json={"content": {"x": 1}})
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/imageatlas/api/config/atlas-3d")
        self.assertEqual(r.json()["content"], {"x": 1})
        r = self.client.get("/imageatlas/api/config/bad row!")
        self.assertEqual(r.status_code, 400)

    def test_me_is_admin(self):
        self.assertTrue(self.client.get("/imageatlas/api/me").json()["admin"])

    def test_build_route_passive_403(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            r = self.client.post("/api/imageatlas/build", json={})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
