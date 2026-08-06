"""atlasops — the Image Atlas move engine (plan / approve / apply / undo).

Everything runs against tmp fixture vaults; the registry is faked at the
imageatlas.vaults seam so nothing reads the machine's config, and the plan
store is rooted in the fixture (the JournalBase isolation lesson: patch the
side effects of the function you CALL).
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import atlasops, imageatlas


def _write(p: Path, text: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.src = base / "src-vault"
        self.dst = base / "dst-vault"
        (self.dst / "wiki").mkdir(parents=True)

        # raw pair
        _write(self.src / "raw" / "aaa.png", "png-bytes")
        _write(self.src / "wiki" / "aaa.md", "---\nsubject: a thing\n---\nbody")
        # asset page fully covered when both images are selected
        _write(self.src / "wiki" / "assets" / "trip" / "one.png")
        _write(self.src / "wiki" / "assets" / "trip" / "two.png")
        _write(self.src / "wiki" / "trip.md",
               "---\nimages_anchors:\n  - trip\n---\n# Trip\n")
        # asset page that can only ever partially move
        _write(self.src / "wiki" / "assets" / "party" / "p1.png")
        _write(self.src / "wiki" / "assets" / "party" / "p2.png")
        _write(self.src / "wiki" / "party.md",
               "---\nimages_anchors:\n  - party\n---\n# Party\n")
        # a loose file and a staying note that references moving things
        _write(self.src / "misc" / "loose.png")
        _write(self.src / "wiki" / "other.md",
               "See [[aaa]] and the embed ![[one.png]] and [[party]].")

        self.vaults = [
            {"id": "primary", "name": "src", "root": str(self.src),
             "exists": True, "primary": True},
            {"id": "dest", "name": "Dest", "root": str(self.dst),
             "exists": True, "primary": False},
        ]
        p1 = mock.patch.object(imageatlas, "vaults", side_effect=lambda: list(self.vaults))
        p2 = mock.patch.object(atlasops, "STORE", Path(self.tmp.name) / "ops.json")
        p1.start(); p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def plan(self, paths, dest="dest", new_vault=None):
        return atlasops.plan_move("primary", paths, dest_vid=dest,
                                  new_vault=new_vault)


class PlanShapes(Base):
    def test_raw_pair_note_moves(self):
        plan = self.plan(["raw/aaa.png"])
        f = plan["files"][0]
        self.assertEqual(f["kind"], "raw-pair")
        self.assertEqual(f["note"], "wiki/aaa.md")
        self.assertEqual(f["note_action"], "moves")
        self.assertEqual([n["path"] for n in plan["notes"]], ["wiki/aaa.md"])
        self.assertEqual(plan["totals"]["images"], 1)
        self.assertEqual(plan["totals"]["notes"], 1)
        self.assertGreater(plan["totals"]["bytes"], 0)

    def test_fully_covered_asset_page_moves(self):
        plan = self.plan(["wiki/assets/trip/one.png", "wiki/assets/trip/two.png"])
        self.assertIn("wiki/trip.md", [n["path"] for n in plan["notes"]])
        self.assertEqual(plan["conflicts"], [])
        for f in plan["files"]:
            self.assertEqual(f["note_action"], "moves")

    def test_partial_asset_page_stays_with_reason(self):
        plan = self.plan(["wiki/assets/party/p1.png"])
        self.assertEqual(plan["notes"], [])
        self.assertEqual(len(plan["conflicts"]), 1)
        c = plan["conflicts"][0]
        self.assertEqual(c["note"], "wiki/party.md")
        self.assertIn("wiki/assets/party/p2.png", c["uncovered"])
        self.assertEqual(plan["files"][0]["note_action"], "stays")

    def test_loose_file_travels_alone(self):
        plan = self.plan(["misc/loose.png"])
        self.assertEqual(plan["files"][0]["kind"], "loose")
        self.assertEqual(plan["notes"], [])

    def test_inbound_links_counted_with_samples(self):
        plan = self.plan(["raw/aaa.png", "wiki/assets/trip/one.png",
                          "wiki/assets/trip/two.png"])
        # other.md references [[aaa]] (moving note stem) and ![[one.png]]
        self.assertGreaterEqual(plan["inbound"]["count"], 2)
        notes = [s["note"] for s in plan["inbound"]["samples"]]
        self.assertIn("wiki/other.md", notes)

    def test_a_moving_note_is_not_an_inbound_source(self):
        # trip.md moves with its images, so its own embeds never count
        plan = self.plan(["wiki/assets/trip/one.png", "wiki/assets/trip/two.png"])
        notes = [s["note"] for s in plan["inbound"]["samples"]]
        self.assertNotIn("wiki/trip.md", notes)

    def test_collisions_reported(self):
        _write(self.dst / "raw" / "aaa.png", "already here")
        plan = self.plan(["raw/aaa.png"])
        self.assertIn("raw/aaa.png", plan["collisions"])

    def test_refusals(self):
        with self.assertRaises(ValueError):
            self.plan([])
        with self.assertRaises(ValueError):
            self.plan(["raw/aaa.png"], dest="primary")
        with self.assertRaises(ValueError):
            self.plan(["../outside.png"])
        with self.assertRaises(ValueError):
            self.plan(["raw/not-there.png"])
        with self.assertRaises(ValueError):
            atlasops.plan_move("primary", ["raw/aaa.png"])   # no destination

    def test_plan_is_stored(self):
        plan = self.plan(["misc/loose.png"])
        self.assertEqual(atlasops.get_plan(plan["id"])["status"], "proposed")
        self.assertEqual(atlasops.recent()[0]["id"], plan["id"])


class ApplyAndUndo(Base):
    def test_apply_moves_files_and_notes(self):
        plan = self.plan(["raw/aaa.png", "wiki/assets/trip/one.png",
                          "wiki/assets/trip/two.png"])
        res = atlasops.apply_plan(plan["id"])
        self.assertEqual(res["moved"], 5)   # 3 images + 2 notes
        self.assertFalse((self.src / "raw" / "aaa.png").exists())
        self.assertTrue((self.dst / "raw" / "aaa.png").is_file())
        self.assertTrue((self.dst / "wiki" / "aaa.md").is_file())
        self.assertTrue((self.dst / "wiki" / "trip.md").is_file())
        self.assertTrue((self.dst / "wiki" / "assets" / "trip" / "one.png").is_file())
        # the emptied anchor dir is tidied away
        self.assertFalse((self.src / "wiki" / "assets" / "trip").exists())
        self.assertEqual(atlasops.get_plan(plan["id"])["status"], "applied")

    def test_apply_refuses_on_drift(self):
        plan = self.plan(["raw/aaa.png"])
        time.sleep(0.02)
        (self.src / "raw" / "aaa.png").write_text("edited since", encoding="utf-8")
        os.utime(self.src / "raw" / "aaa.png", (time.time(), time.time() + 5))
        with self.assertRaises(ValueError) as cm:
            atlasops.apply_plan(plan["id"])
        self.assertIn("re-plan", str(cm.exception))
        self.assertTrue((self.src / "raw" / "aaa.png").exists())

    def test_apply_refuses_on_late_collision(self):
        plan = self.plan(["misc/loose.png"])
        _write(self.dst / "misc" / "loose.png", "arrived meanwhile")
        with self.assertRaises(ValueError):
            atlasops.apply_plan(plan["id"])
        self.assertTrue((self.src / "misc" / "loose.png").exists())

    def test_apply_is_single_status_transition(self):
        plan = self.plan(["misc/loose.png"])
        atlasops.apply_plan(plan["id"])
        with self.assertRaises(ValueError):
            atlasops.apply_plan(plan["id"])

    def test_undo_returns_everything(self):
        plan = self.plan(["raw/aaa.png"])
        atlasops.apply_plan(plan["id"])
        res = atlasops.undo_plan(plan["id"])
        self.assertEqual(res["returned"], 2)
        self.assertTrue((self.src / "raw" / "aaa.png").is_file())
        self.assertTrue((self.src / "wiki" / "aaa.md").is_file())
        self.assertFalse((self.dst / "raw" / "aaa.png").exists())
        self.assertEqual(atlasops.get_plan(plan["id"])["status"], "undone")

    def test_undo_never_overwrites_a_reoccupied_path(self):
        plan = self.plan(["misc/loose.png"])
        atlasops.apply_plan(plan["id"])
        _write(self.src / "misc" / "loose.png", "new occupant")
        res = atlasops.undo_plan(plan["id"])
        self.assertEqual(res["returned"], 0)
        self.assertEqual(res["failed"][0]["error"], "source path re-occupied")
        self.assertEqual((self.src / "misc" / "loose.png").read_text(encoding="utf-8"),
                         "new occupant")

    def test_passive_refuses_apply_and_undo(self):
        plan = self.plan(["misc/loose.png"])
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(PermissionError):
                atlasops.apply_plan(plan["id"])
            with self.assertRaises(PermissionError):
                atlasops.undo_plan(plan["id"])
        self.assertTrue((self.src / "misc" / "loose.png").exists())

    def test_new_vault_created_at_apply_not_plan(self):
        nv_root = Path(self.tmp.name) / "brand-new"
        plan = self.plan(["misc/loose.png"], dest="",
                         new_vault={"name": "Brand New", "root": str(nv_root)})
        self.assertFalse(nv_root.exists())     # planning has no side effects
        entry = {"id": "brand-new", "name": "Brand New", "root": str(nv_root),
                 "exists": True, "primary": False}

        def fake_register(name, root, create=False):
            (Path(root) / "wiki").mkdir(parents=True, exist_ok=True)
            (Path(root) / "raw").mkdir(parents=True, exist_ok=True)
            self.vaults.append(entry)
            return entry
        with mock.patch.object(imageatlas, "register_vault",
                               side_effect=fake_register):
            res = atlasops.apply_plan(plan["id"])
        self.assertEqual(res["dest"], "brand-new")
        self.assertTrue((nv_root / "misc" / "loose.png").is_file())


@unittest.skipUnless(atlasops.CHASKA_OK, "chaska not installed (optional dependency)")
class SidecarMigration(Base):
    def test_vec_rows_follow_the_move(self):
        from chaska.config import Config
        from chaska.index import Index
        idx = Index(Config(root=self.src))
        idx.con.execute("INSERT INTO items(path, mtime, size) VALUES (?,?,?)",
                        ("misc/loose.png", 1.0, 1))
        idx.con.execute("INSERT INTO vecs(path, model, dims, v) VALUES (?,?,?,?)",
                        ("misc/loose.png", "m", 4, b"\x00" * 8))
        idx.con.commit(); idx.close()
        plan = self.plan(["misc/loose.png"])
        res = atlasops.apply_plan(plan["id"])
        self.assertEqual(res["migrated_vectors"], 1)
        dst_idx = Index(Config(root=self.dst))
        row = dst_idx.con.execute("SELECT * FROM vecs WHERE path=?",
                                  ("misc/loose.png",)).fetchone()
        dst_idx.close()
        self.assertIsNotNone(row)
        src_idx = Index(Config(root=self.src))
        row = src_idx.con.execute("SELECT * FROM vecs WHERE path=?",
                                  ("misc/loose.png",)).fetchone()
        src_idx.close()
        self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
