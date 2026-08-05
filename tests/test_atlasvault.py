"""Vault-web overlay tests: person/entity parsing over a tmp wiki,
CRM dedupe (fold / drop / fresh), the three edge signals including the
company bridge into the CRM, degree BFS, geopolitics-entity handling,
and merge() folding onto an existing composed graph.

Everything roots at a tmp fixture: crm._load, settings.get, owner_pid
and atlaslens._ab_index are all pinned so no test reads this machine's
CRM, config, or AddressBook (the documented machine-read trap).

Run: .venv/bin/python -m unittest tests.test_atlasvault
"""
import copy
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import atlas, atlasvault, atlaslens, data as crm, settings


PAGES = {
    "ada-quill.md": """---
title: Ada Quill
type: person
tags: [nimbus-labs, ai]
sources:
  - "[[ep-one-podcast]]"
---

# Ada Quill

Researcher at **[[nimbus-labs|Nimbus Labs]]** working on webs.

- [[bo-reed]] — collaborator.
""",
    "bo-reed.md": """---
title: Bo Reed
type: person
tags:
  - nimbus-labs
sources:
  - "[[ep-one-podcast]]"
---

# Bo Reed

Host of the Ep One podcast.
""",
    "cher.md": """---
title: Cher
type: person
tags: [music]
---

Single-token name — must never fold onto a CRM contact.
""",
    "zed-poet.md": """---
title: Zed Poet
type: person
tags: []
---

Writes about [[atlantis]].
""",
    "yara-blue.md": """---
title: Yara Blue
type: person
tags: [atlantis]
---

Chronicler of [[atlantis]].
""",
    "carol-finch.md": """---
title: Carol Finch
type: person
tags: []
---

Knows [[alice-larkspur]] well.
""",
    "alice-larkspur.md": """---
title: Alice Larkspur
type: person
tags: []
---

Runs the falconry circle.
""",
    "erin-swift.md": """---
title: Erin Swift
type: person
tags: []
---

Known but quiet.
""",
    "owner-example.md": """---
title: Owner Example
type: person
tags: []
---

The owner themself.
""",
    "nimbus-labs.md": """---
title: Nimbus Labs
type: entity
tags: [ai-lab]
---

# Nimbus Labs
""",
    "atlantis.md": """---
title: Atlantis
type: entity
tags: [geopolitics]
---

# Atlantis
""",
    "some-summary.md": """---
title: A source summary
type: source-summary
---

Mentions [[ada-quill]] and [[bo-reed]] but is not an identity page.
""",
    "untyped.md": "just prose, no frontmatter\n",
    "tess-vole.md": """---
title: Tess Vole
type: person
tags: []
---

Middle-name variant of a CRM contact.
""",
    "ann-reef.md": """---
title: Ann Reef
type: person
tags: []
---

First+last matches TWO contacts — must stay a vault node.
""",
}


def _graph():
    return {
        "nodes": [
            {"id": "p_alice", "name": "Alice Larkspur",
             "company": "Nimbus Labs", "cluster": None, "act": 100},
            {"id": "p_carol", "name": "Carol Finch",
             "company": "", "cluster": None, "act": 50},
        ],
        "edges": [
            {"a": "p_alice", "b": "p_carol", "weight": 0.8,
             "signals": [{"type": "colleague", "strength": 1.0,
                          "detail": "both at Falcon"}]},
        ],
        "ego_edges": [{"a": "ego", "b": "p_alice", "weight": 1.0,
                       "signals": []}],
        "clusters": [],
        "node_cluster": {},
    }


def _cache():
    people = [
        {"id": "p_alice", "name": "Alice Larkspur"},
        {"id": "p_carol", "name": "Carol Finch"},
        {"id": "p_erin", "name": "Erin Swift"},      # not in the graph
        {"id": "p_owner", "name": "Owner Example"},
        {"id": "p_cher", "name": "Cher"},            # single token
        {"id": "p_tess", "name": "Tess Marie Vole"},  # middle-name variant
        {"id": "p_ann1", "name": "Ann X Reef"},      # ambiguous first+last
        {"id": "p_ann2", "name": "Ann Y Reef"},
        {"id": "p_x", "name": "(unidentified 555)"},
    ]
    return {"people": people, "master": {}, "profiles": {},
            "by_id": {p["id"]: p for p in people}, "by_handle": {},
            "chats_by_person": {}, "loaded_at": 0}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        wiki = self.root / "wiki"
        wiki.mkdir()
        for name, text in PAGES.items():
            (wiki / name).write_text(text, encoding="utf-8")
        self.cfg = {"atlas_min_edge_weight": 0.15,
                    "owner_name": "Owner Example"}
        self.cache = _cache()
        patches = [
            mock.patch.object(crm, "_load", lambda: self.cache),
            mock.patch.object(settings, "get",
                              lambda k: self.cfg.get(k, "")),
            mock.patch.object(atlas, "owner_pid", lambda c=None: "p_owner"),
            mock.patch.object(atlaslens, "_ab_index", lambda: {}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        atlasvault._cache["fp"] = None       # never reuse another test's scan

    def overlay(self, graph=None, **kw):
        return atlasvault.overlay(graph or _graph(), root=self.root, **kw)


class ScanTests(Base):
    def test_parses_people_and_entities_only(self):
        s = atlasvault.scan(self.root)
        self.assertEqual(len(s["people"]), 11)
        self.assertEqual(set(s["entities"]), {"nimbus-labs", "atlantis"})
        self.assertNotIn("some-summary", s["people"])

    def test_tags_both_forms_and_qualifier(self):
        s = atlasvault.scan(self.root)
        ada = s["people"]["ada-quill"]
        self.assertIn("nimbus-labs", ada["tags"])          # inline form
        self.assertIn("nimbus-labs", s["people"]["bo-reed"]["tags"])  # block
        self.assertEqual(ada["qualifier"],
                         "Researcher at Nimbus Labs working on webs.")

    def test_links_and_sources_split(self):
        s = atlasvault.scan(self.root)
        ada = s["people"]["ada-quill"]
        self.assertIn("bo-reed", ada["links"])
        self.assertIn("ep-one-podcast", ada["sources"])
        self.assertNotIn("ep-one-podcast", ada["links"])

    def test_cache_invalidates_on_edit(self):
        atlasvault.scan(self.root)
        p = self.root / "wiki" / "new-face.md"
        p.write_text("---\ntitle: New Face\ntype: person\n---\n\nFresh.\n",
                     encoding="utf-8")
        s = atlasvault.scan(self.root)
        self.assertIn("new-face", s["people"])


class PartitionTests(Base):
    def test_fold_drop_fresh(self):
        ov = self.overlay()
        ids = {n["id"] for n in ov["nodes"]}
        # matched-in-graph people fold, never node
        self.assertNotIn("v:carol-finch", ids)
        self.assertNotIn("v:alice-larkspur", ids)
        self.assertEqual(ov["wiki_refs"],
                         {"p_carol": "wiki/carol-finch.md",
                          "p_alice": "wiki/alice-larkspur.md"})
        # below-cutoff match and the owner both drop
        self.assertNotIn("v:erin-swift", ids)
        self.assertNotIn("v:owner-example", ids)
        # a first+last variant of a known contact drops too
        self.assertNotIn("v:tess-vole", ids)
        # everyone else is a fresh vault node — including the page whose
        # first+last pair matches TWO contacts (ambiguous, never folded)
        self.assertEqual(ids, {"v:ada-quill", "v:bo-reed", "v:cher",
                               "v:zed-poet", "v:yara-blue", "v:ann-reef"})

    def test_single_token_never_matches(self):
        ov = self.overlay()
        self.assertIn("v:cher", {n["id"] for n in ov["nodes"]})


class EdgeTests(Base):
    def _pairs(self, ov, kind=None):
        out = {}
        for e in ov["edges"]:
            for s in e["signals"]:
                if kind is None or s["type"] == kind:
                    out.setdefault(frozenset((e["a"], e["b"])), []).append(s)
        return out

    def test_wiki_link_edge(self):
        links = self._pairs(self.overlay(), "wiki_link")
        self.assertIn(frozenset(("v:ada-quill", "v:bo-reed")), links)
        # both endpoints matched CRM pids still make a wiki_link edge
        self.assertIn(frozenset(("p_alice", "p_carol")), links)

    def test_cosource_edge(self):
        co = self._pairs(self.overlay(), "wiki_cosource")
        sig = co[frozenset(("v:ada-quill", "v:bo-reed"))][0]
        self.assertIn("1 shared source", sig["detail"])

    def test_org_edge_and_crm_bridge(self):
        org = self._pairs(self.overlay(), "wiki_org")
        self.assertIn(frozenset(("v:ada-quill", "v:bo-reed")), org)
        # the bridge: Alice's CRM company normalizes onto the entity title
        self.assertIn(frozenset(("v:ada-quill", "p_alice")), org)
        self.assertIn(frozenset(("v:bo-reed", "p_alice")), org)

    def test_geopolitics_entity_edges_but_no_company(self):
        ov = self.overlay()
        org = self._pairs(ov, "wiki_org")
        self.assertIn(frozenset(("v:zed-poet", "v:yara-blue")), org)
        zed = next(n for n in ov["nodes"] if n["id"] == "v:zed-poet")
        self.assertEqual(zed["company"], "")

    def test_company_field_from_entity(self):
        ov = self.overlay()
        ada = next(n for n in ov["nodes"] if n["id"] == "v:ada-quill")
        self.assertEqual(ada["company"], "Nimbus Labs")

    def test_min_weight_floor(self):
        ov = self.overlay(min_weight=5.0)
        self.assertEqual(ov["edges"], [])


class DegreeTests(Base):
    def test_bridged_vault_nodes_read_second_degree(self):
        ov = self.overlay()
        deg = {n["id"]: n["degree"] for n in ov["nodes"]}
        self.assertEqual(deg["v:ada-quill"], 2)     # via Alice (degree 1)
        self.assertEqual(deg["v:bo-reed"], 2)
        self.assertIsNone(deg["v:cher"])            # no path from the ego


class MergeTests(Base):
    def test_merge_shape_and_signal_fold(self):
        g = _graph()
        atlasvault.merge(g, root=self.root)
        ids = {n["id"] for n in g["nodes"]}
        self.assertEqual(len(ids), 8)               # 2 CRM + 6 vault
        # the (alice, carol) pair existed — signals fold, no duplicate edge
        pair_edges = [e for e in g["edges"]
                      if {e["a"], e["b"]} == {"p_alice", "p_carol"}]
        self.assertEqual(len(pair_edges), 1)
        kinds = {s["type"] for s in pair_edges[0]["signals"]}
        self.assertEqual(kinds, {"colleague", "wiki_link"})
        # wiki refs stamped on the folded CRM nodes
        carol = next(n for n in g["nodes"] if n["id"] == "p_carol")
        self.assertEqual(carol["wiki"], "wiki/carol-finch.md")
        self.assertEqual(g["vault"]["people"], 6)
        self.assertEqual(g["vault"]["linked"], 2)

    def test_missing_wiki_is_dormant(self):
        g = _graph()
        before = copy.deepcopy(g)
        atlasvault.merge(g, root=self.root / "nowhere")
        self.assertEqual(g["nodes"], before["nodes"])
        self.assertEqual(g["edges"], before["edges"])
        self.assertEqual(g["vault"]["people"], 0)


if __name__ == "__main__":
    unittest.main()
