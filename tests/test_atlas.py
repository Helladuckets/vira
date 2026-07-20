"""Contact Atlas engine tests: signal fusion over a synthetic CRM +
faces fixture — edge symmetry, weight thresholding, ego/degree BFS,
shortest_path, cluster assignment (anchor org, family components), and
graceful degradation when the media/vault indexes are absent.

Run: .venv/bin/python -m unittest tests.test_atlas
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import atlas, data as crm, mediaindex, photos, settings, vault


def _person(pid, name, tier="B", imsg=0, email=0):
    return {"id": pid, "name": name, "profile_tier": tier,
            "handles": {"emails": [], "imessage": [], "phones10": []},
            "activity": {"imsg_n": imsg, "email_n": email}}


def _fixture_cache():
    """A small synthetic world exercising every signal type."""
    people = [
        _person("p_owner", "Owner Example", tier="A", imsg=1),
        _person("p_alice", "Alice Larkspur", tier="A", imsg=300),
        _person("p_bob", "Bob Larkspur", tier="B", imsg=60),
        _person("p_carol", "Carol Finch", tier="A", imsg=150, email=25),
        _person("p_dave", "Dave Heron", tier="B", imsg=10),
        _person("p_erin", "Erin Swift", tier="C"),          # no 1:1 history
        _person("p_frank", "Frank Plover", tier="C", imsg=4),
        _person("p_grace", "Grace Plover", tier="C", imsg=4),
        _person("p_x", "(unidentified 555)", tier="B", imsg=99),
    ]
    master = {
        "p_alice": {"id": "p_alice", "company": "Falcon Capital"},
        "p_carol": {"id": "p_carol", "company": "Falcon Capital, LLC"},
        "p_frank": {"id": "p_frank",
                    "relationship": "brother of Grace"},
    }
    profiles = {
        "p_bob": {"relationship_class": "family"},
        "p_alice": {"hooks": ["loves falconry and telescopes"]},
        "p_carol": {"hooks": ["falconry meetups", "telescopes collector"]},
    }
    group = {"file": "g1.md", "type": "group", "title": "group: trip",
             "messages": 500,
             "participants": [{"person_id": "p_carol"},
                              {"person_id": "p_dave"}]}
    group2 = {"file": "g2.md", "type": "group", "title": "group: quiet",
              "messages": 40,
              "participants": [{"person_id": "p_dave"},
                               {"person_id": "p_erin"}]}
    blast = {"file": "g3.md", "type": "group", "title": "group: blast",
             "messages": 9,
             "participants": [{"person_id": f"p_b{i}"} for i in range(11)]
             + [{"person_id": "p_alice"}, {"person_id": "p_carol"}]}
    chats_by_person = {
        "p_carol": [group, blast], "p_dave": [group, group2],
        "p_erin": [group2], "p_alice": [blast],
    }
    by_id = {p["id"]: p for p in people}
    return {"people": people, "master": master, "profiles": profiles,
            "by_id": by_id, "by_handle": {"owner@example.com": "p_owner"},
            "chats_by_person": chats_by_person, "loaded_at": 0}


def _faces_db(path):
    con = sqlite3.connect(path)
    con.executescript(mediaindex.SCHEMA)
    seq = 0
    def add_photo(*pids):
        nonlocal seq
        seq += 1
        con.execute(
            "INSERT INTO items(seq, kind, id, path, purged) "
            "VALUES(?, 'photo', ?, ?, 0)", (seq, seq, f"/tmp/none{seq}.jpg"))
        for pid in pids:
            con.execute(
                "INSERT INTO faces(seq, bbox, det_score, person_id, "
                "match_score) VALUES(?, ?, 0.9, ?, 0.8)",
                (seq, json.dumps([10, 10, 60, 60]), pid))
    for _ in range(3):
        add_photo("p_alice", "p_bob")            # alice-bob co-occurrence
    add_photo("p_alice", "p_dave")
    add_photo("p_owner", "p_alice")              # ego photo signal
    con.commit()
    con.close()


class AtlasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        faces = root / "media-index.sqlite"
        _faces_db(faces)
        self.cache = _fixture_cache()
        self.cfg = {"atlas_anchor_org": "", "atlas_max_nodes": 200,
                    "atlas_min_edge_weight": 0.15,
                    "owner_name": "Owner",
                    "notify_handle": "owner@example.com"}
        patches = [
            mock.patch.object(crm, "_load", lambda: self.cache),
            mock.patch.object(atlas, "GRAPH", root / "atlas-graph.json"),
            mock.patch.object(atlas, "GROUPS", root / "atlas-groups.json"),
            mock.patch.object(atlas, "FACES_DIR", root / "atlas-faces"),
            mock.patch.object(mediaindex, "DB", faces),
            mock.patch.object(vault, "DB_PATH", root / "no-vault.sqlite"),
            mock.patch.object(photos, "photo_path", lambda pid: None),
            mock.patch.object(settings, "get", self._get),
            mock.patch.object(settings, "raw", lambda: self.cfg),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _get(self, key):
        v = self.cfg.get(key)
        return v if v not in (None, "") else settings.DEFAULTS.get(key, "")

    # ---------- node selection / ego ----------

    def test_owner_and_node_selection(self):
        self.assertEqual(atlas.owner_pid(self.cache), "p_owner")
        pids = {p["id"] for p in atlas.select_nodes(self.cache)}
        self.assertNotIn("p_owner", pids)         # the owner IS the ego
        self.assertNotIn("p_x", pids)             # placeholders excluded
        self.assertIn("p_erin", pids)

    def test_node_cap(self):
        pids = [p["id"] for p in atlas.select_nodes(self.cache, cap=3)]
        self.assertEqual(len(pids), 3)
        self.assertEqual(pids[0], "p_alice")      # most active first

    # ---------- edges ----------

    def _edges(self):
        pids = {p["id"] for p in atlas.select_nodes(self.cache)}
        return atlas.build_edges(self.cache, pids)

    def test_edge_symmetry_and_signals(self):
        edges = self._edges()
        seen = set()
        for e in edges:
            self.assertLess(e["a"], e["b"])       # canonical order, no dups
            self.assertNotIn((e["a"], e["b"]), seen)
            seen.add((e["a"], e["b"]))
        by_pair = {(e["a"], e["b"]): e for e in edges}
        ab = by_pair[("p_alice", "p_bob")]
        types = {s["type"] for s in ab["signals"]}
        self.assertIn("photo_cooccur", types)     # 3 shared photos
        self.assertIn("family", types)            # shared surname + tag
        ac = by_pair[("p_alice", "p_carol")]
        types = {s["type"] for s in ac["signals"]}
        self.assertIn("colleague", types)         # Falcon Capital, LLC norm
        self.assertIn("shared_topic", types)      # falconry + telescopes
        self.assertIn(("p_carol", "p_dave"), by_pair)   # group co-chat
        self.assertIn(("p_dave", "p_erin"), by_pair)
        fg = by_pair[("p_frank", "p_grace")]      # relationship-text family
        self.assertEqual(fg["signals"][0]["type"], "family")

    def test_blast_groups_carry_no_signal(self):
        edges = self._edges()
        by_pair = {(e["a"], e["b"]): e for e in edges}
        ac = by_pair[("p_alice", "p_carol")]
        self.assertNotIn("group_cochat",
                         {s["type"] for s in ac["signals"]})

    def test_weight_threshold_drops_weak_edges(self):
        edges = self._edges()
        pairs = {(e["a"], e["b"]) for e in edges}
        self.assertIn(("p_alice", "p_carol"), pairs)
        self.cfg["atlas_min_edge_weight"] = 5.0   # nothing clears this
        self.assertEqual(atlas.build_edges(
            self.cache, {p["id"] for p in atlas.select_nodes(self.cache)}),
            [])

    # ---------- degrees / path ----------

    def test_degrees_and_shortest_path(self):
        g = atlas.build_graph()
        deg = {n["id"]: n["degree"] for n in g["nodes"]}
        self.assertEqual(deg["p_alice"], 1)       # direct history
        self.assertEqual(deg["p_dave"], 1)
        self.assertEqual(deg["p_erin"], 2)        # only via Dave's group
        path = atlas.shortest_path(g, "p_erin", "p_alice")
        self.assertEqual(path, ["p_erin", "p_dave", "p_alice"])
        res = atlas.path_between("p_erin", "p_carol")
        self.assertTrue(res["found"])
        self.assertEqual([p["pid"] for p in res["path"]],
                         ["p_erin", "p_dave", "p_carol"])

    def test_ego_edges(self):
        g = atlas.build_graph()
        ego = {e["b"]: e for e in g["ego_edges"]}
        self.assertIn("p_alice", ego)
        self.assertNotIn("p_erin", ego)           # no direct history
        types = {s["type"] for s in ego["p_alice"]["signals"]}
        self.assertEqual(types, {"direct", "photo_cooccur"})

    # ---------- clusters ----------

    def test_anchor_and_family_clusters(self):
        self.cfg["atlas_anchor_org"] = "Falcon Capital"
        g = atlas.build_graph()
        anchor = next(c for c in g["clusters"] if c["anchor"])
        self.assertEqual(anchor["label"], "Falcon Capital")
        cluster_of = {n["id"]: n["cluster"] for n in g["nodes"]}
        self.assertEqual(cluster_of["p_alice"], anchor["id"])
        self.assertEqual(cluster_of["p_carol"], anchor["id"])
        fam = next((c for c in g["clusters"] if "family" in c["label"]
                    and "Plover" in c["label"]), None)
        self.assertIsNotNone(fam)
        self.assertEqual(cluster_of["p_frank"], fam["id"])
        self.assertEqual(cluster_of["p_grace"], fam["id"])

    # ---------- store / API payloads ----------

    def test_compose_and_node_detail(self):
        self.assertEqual(atlas.compose()["status"], "empty")
        atlas.build_graph()
        g = atlas.compose()
        self.assertEqual(g["status"], "ok")
        self.assertTrue(g["generated"])
        d = atlas.node_detail("p_alice")
        self.assertEqual(d["node"]["name"], "Alice Larkspur")
        self.assertTrue(d["edges"])
        self.assertEqual(d["edges"][0]["pid"], "p_bob")   # strongest first
        self.assertIsNone(atlas.node_detail("p_nobody"))

    def test_narration_survives_rebuild(self):
        g = atlas.build_graph()
        g["edges"][0]["narrative"] = "old friends from the lake house"
        with atlas._lock:
            atlas._write(g)
        g2 = atlas.build_graph()
        pair = (g["edges"][0]["a"], g["edges"][0]["b"])
        kept = next(e for e in g2["edges"]
                    if (e["a"], e["b"]) == pair)
        self.assertEqual(kept["narrative"], "old friends from the lake house")

    # ---------- graceful degradation ----------

    def test_builds_without_media_or_vault_index(self):
        with mock.patch.object(mediaindex, "DB",
                               Path(self.tmp.name) / "missing.sqlite"):
            g = atlas.build_graph()
        types = {s["type"] for e in g["edges"] for s in e["signals"]}
        self.assertNotIn("photo_cooccur", types)  # no index -> no photo edges
        self.assertIn("family", types)            # everything else still works
        self.assertIn(("p_carol"), {e["b"] for e in g["ego_edges"]})

    def test_face_crop_absent_index(self):
        with mock.patch.object(mediaindex, "DB",
                               Path(self.tmp.name) / "missing.sqlite"):
            self.assertIsNone(atlas.face_crop("p_alice"))

    # ---------- user-curated groups (the override layer) ----------

    def _family(self):
        g = atlas.compose()
        return next(c for c in g["clusters"] if "family" in c["label"])

    def test_group_create_and_assign(self):
        atlas.build_graph()
        r = atlas.group_create("Poker night")
        gid = r["gid"]
        custom = next(c for c in r["clusters"] if c["id"] == gid)
        self.assertTrue(custom["custom"])
        self.assertEqual(custom["size"], 0)     # visible even when empty
        r = atlas.group_assign("p_alice", gid)
        self.assertEqual(r["node_cluster"]["p_alice"], gid)
        self.assertEqual(
            next(c for c in r["clusters"] if c["id"] == gid)["size"], 1)

    def test_assign_to_derived_promotes(self):
        atlas.build_graph()
        fam = self._family()
        members = [p for p, c in atlas.compose()["node_cluster"].items()
                   if c == fam["id"]]
        r = atlas.group_assign("p_alice", fam["id"])
        gid = r["gid"]
        self.assertTrue(gid.startswith("g"))
        # the whole family snapshots into the custom group + the new member
        for pid in members + ["p_alice"]:
            self.assertEqual(r["node_cluster"][pid], gid)
        # the derived chip is superseded — same label, custom now
        labels = [(c["label"], c.get("custom", False))
                  for c in r["clusters"]]
        self.assertIn((fam["label"], True), labels)
        self.assertNotIn((fam["label"], False), labels)

    def test_rename_promotes_and_survives_rebuild(self):
        atlas.build_graph()
        fam = self._family()
        r = atlas.group_rename(fam["id"], "The Plovers")
        gid = r["gid"]
        self.assertIn("The Plovers",
                      [c["label"] for c in r["clusters"]])
        atlas.build_graph()                      # rebuild reshuffles ids
        g = atlas.compose()
        self.assertIn("The Plovers", [c["label"] for c in g["clusters"]])
        self.assertNotIn(fam["label"], [c["label"] for c in g["clusters"]])
        self.assertEqual(g["node_cluster"]["p_frank"], gid)

    def test_dissolve_derived_stays_gone(self):
        atlas.build_graph()
        fam = self._family()
        r = atlas.group_dissolve(fam["id"])
        self.assertNotIn(fam["label"], [c["label"] for c in r["clusters"]])
        self.assertIsNone(r["node_cluster"].get("p_frank"))
        atlas.build_graph()                      # cannot resurrect
        g = atlas.compose()
        self.assertNotIn(fam["label"], [c["label"] for c in g["clusters"]])

    def test_remove_member_and_delete_custom_falls_back(self):
        atlas.build_graph()
        fam = self._family()
        r = atlas.group_assign("p_frank", "")    # promote + remove frank
        gid = next(c["id"] for c in r["clusters"]
                   if c["label"] == fam["label"])
        self.assertIsNone(r["node_cluster"].get("p_frank"))
        self.assertEqual(
            next(c for c in r["clusters"] if c["id"] == gid)["size"],
            fam["size"] - 1)
        # deleting the custom group clears its assignments; members fall
        # back to the derived grouping (label was dissolved by promotion,
        # so a re-dissolve entry keeps the derived chip away)
        r = atlas.group_dissolve(gid)
        self.assertNotIn(gid, [c["id"] for c in r["clusters"]])

    def test_person_groups_payload(self):
        atlas.build_graph()
        fam = self._family()
        pg = atlas.person_groups("p_frank")
        self.assertEqual(pg["current"]["id"], fam["id"])
        self.assertTrue(pg["in_atlas"])
        # designating someone outside the rendered graph sticks in the store
        r = atlas.group_create("Advisors")
        atlas.group_assign("p_ghost", r["gid"])
        pg2 = atlas.person_groups("p_ghost")
        self.assertEqual(pg2["current"]["id"], r["gid"])
        self.assertFalse(pg2["in_atlas"])


if __name__ == "__main__":
    unittest.main()
