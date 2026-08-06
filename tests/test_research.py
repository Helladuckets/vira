"""Focused tests for the generic, read-only research graph adapter.

All names, paths, claims, and evidence in this module are synthetic.  The
fixtures exercise authority boundaries without copying personal research into
the repository.
"""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import readingroom, research


SCHEMA = """
CREATE TABLE sources (
    source_id TEXT PRIMARY KEY,
    title TEXT,
    publication_date TEXT,
    original_url TEXT,
    canonical_url TEXT
);
CREATE TABLE source_aliases (
    source_id TEXT,
    alias_url TEXT
);
CREATE TABLE captures (
    capture_id TEXT PRIMARY KEY,
    source_id TEXT,
    local_pointer TEXT
);
CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    event_title TEXT,
    event_date TEXT
);
CREATE TABLE event_sources (
    event_id TEXT,
    source_id TEXT,
    source_role TEXT,
    relationship TEXT,
    confidence REAL,
    is_canonical INTEGER
);
CREATE TABLE claims (
    claim_id TEXT PRIMARY KEY,
    claim_label TEXT,
    category TEXT,
    description TEXT,
    personal_relevance_score INTEGER
);
CREATE TABLE claim_rollups (
    claim_id TEXT PRIMARY KEY,
    claim_label TEXT,
    category TEXT,
    personal_relevance_score INTEGER,
    distinct_speakers INTEGER,
    distinct_events INTEGER,
    utterance_count INTEGER,
    evidence_status TEXT
);
CREATE TABLE claim_utterances (
    claim_id TEXT,
    utterance_id TEXT,
    event_id TEXT,
    relationship TEXT,
    speaker_name TEXT,
    speaker_person_id TEXT,
    speaker_verified INTEGER,
    canonical_text TEXT,
    source_url TEXT
);
CREATE TABLE utterance_appearances (
    appearance_id TEXT PRIMARY KEY,
    utterance_id TEXT,
    event_id TEXT,
    source_id TEXT,
    text TEXT,
    source_url TEXT
);
CREATE TABLE source_relations (
    source_id TEXT,
    related_source_id TEXT,
    relationship TEXT
);
"""


class ResearchGraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.self_record = self.root / "self"
        self.subject = self.self_record / "14-example-labs"
        self.data = self.subject / "corpus" / "data"
        self.data.mkdir(parents=True)
        self.database = self.data / "research.sqlite"
        con = sqlite3.connect(self.database)
        con.executescript(SCHEMA)
        con.execute(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?)",
            ("src-1", "Primary conversation", "2026-05-04",
             "https://www.example.test/talk?utm_source=feed",
             "https://example.test/talk"),
        )
        con.execute(
            "INSERT INTO source_aliases VALUES (?, ?)",
            ("src-1", "https://media.example.test/watch?v=7"),
        )
        con.execute(
            "INSERT INTO captures VALUES (?, ?, ?)",
            ("cap-1", "src-1", "corpus/raw/source.txt"),
        )
        con.execute(
            "INSERT INTO events VALUES (?, ?, ?)",
            ("evt-1", "Product discussion", "2026-05-04"),
        )
        con.execute(
            "INSERT INTO event_sources VALUES (?, ?, ?, ?, ?, ?)",
            ("evt-1", "src-1", "canonical", "transcript", 1.0, 1),
        )
        con.execute(
            "INSERT INTO claims VALUES (?, ?, ?, ?, ?)",
            ("claim-1", "Small teams own outcomes", "culture",
             "Teams retain end-to-end responsibility.", 9),
        )
        con.execute(
            "INSERT INTO claim_rollups VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("claim-1", "Small teams own outcomes", "culture", 9,
             1, 1, 1, "supported"),
        )
        con.execute(
            "INSERT INTO claim_utterances VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("claim-1", "utt-1", "evt-1", "supports", "Person A",
             "person-a", 1,
             "We keep ownership close to the work.",
             "https://example.test/talk"),
        )
        # Two appearances represent one canonical utterance.  The adapter must
        # not inflate the claim evidence count to two.
        con.execute(
            "INSERT INTO utterance_appearances VALUES (?, ?, ?, ?, ?, ?)",
            ("app-1", "utt-1", "evt-1", "src-1",
             "We keep ownership close to the work.",
             "https://example.test/talk"),
        )
        con.execute(
            "INSERT INTO utterance_appearances VALUES (?, ?, ?, ?, ?, ?)",
            ("app-2", "utt-1", "evt-1", "src-1",
             "We keep ownership close to the work.",
             "https://media.example.test/watch?v=7"),
        )
        con.commit()
        con.close()

        self.manifest = self.data / "manifest.json"
        self.manifest.write_text(json.dumps({
            "built": "2026-06-01T00:00:00Z",
            "database": "research.sqlite",
            # Intentionally stale: database counts are canonical.
            "tables": {"sources": 99, "claims": 99},
            "limitations": ["Synthetic fixture has one source."],
        }), encoding="utf-8")
        self.taxonomy = self.subject / "corpus" / "application_bridge_taxonomy.json"
        self.taxonomy.write_text(json.dumps({
            "themes": [{"research_claim": "claim-1", "personal_fact": "fact-x"}],
            "items": [
                {"id": "matched", "label": "Matched bridge", "group": "culture",
                 "claim_ids": ["claim-1"], "rank": 1,
                 "application_use": "Use the matching evidence."},
                {"id": "other", "label": "Other bridge", "group": "culture",
                 "claim_ids": ["claim-2"], "rank": 2,
                 "application_use": "Do not attach here."},
            ],
        }), encoding="utf-8")
        self.claim_taxonomy = self.subject / "corpus" / "claim_taxonomy.json"
        self.claim_taxonomy.write_text(json.dumps({"claims": [
            {"claim_id": "claim-1", "label": "Small teams own outcomes"},
            {"claim_id": "claim-without-evidence",
             "label": "Defined but not materialized"},
        ]}), encoding="utf-8")

        self.rooms = self.root / "rooms"
        self.rooms.mkdir()
        self.room_item = {
            "id": "item-1",
            "title": "Primary conversation",
            "url": "https://www.example.test/talk?utm_campaign=ignored",
            "vault": "wiki/source-summary.md",
        }
        (self.rooms / "example-labs.json").write_text(json.dumps({
            "slug": "example-labs",
            "title": "Example Labs room",
            "subtitle": "Synthetic fixture",
            "built": "2026-06-02",
            "items": [self.room_item],
        }), encoding="utf-8")

        self.auto_patch = mock.patch.object(
            research.applications, "self_record", return_value=self.self_record
        )
        self.settings_patch = mock.patch.object(
            research.settings, "raw", return_value={}
        )
        self.rooms_patch = mock.patch.object(readingroom, "ROOMS_DIR", self.rooms)
        self.auto_patch.start()
        self.settings_patch.start()
        self.rooms_patch.start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def test_catalog_auto_discovers_manifest_and_explicit_config(self):
        rows = research.catalog()
        self.assertEqual(["example-labs"], [row["id"] for row in rows])
        self.assertEqual("canonical", rows[0]["authority"]["database"])
        self.assertTrue(rows[0]["taxonomy_present"])

        with mock.patch.object(research.settings, "raw", return_value={
            "research_graphs": {
                "configured-graph": {
                    "manifest": str(self.manifest),
                    "company": "Configured Company",
                }
            }
        }):
            configured = research.catalog()
        self.assertEqual("configured-graph", configured[0]["id"])
        self.assertEqual("Configured Company", configured[0]["company"])

    def test_overview_counts_database_not_manifest(self):
        result = research.overview()
        self.assertEqual(1, result["canonical"]["table_counts"]["sources"])
        self.assertEqual(1, result["canonical"]["table_counts"]["claims"])
        self.assertEqual(99, result["build_metadata"]["declared_table_counts"]["sources"])
        self.assertEqual("src-1", result["canonical"]["latest_source"]["source_id"])
        self.assertEqual(1, result["coverage"]["materialized_claim_count"])
        self.assertEqual(2, result["coverage"]["defined_claim_count"])
        self.assertEqual(["claim-without-evidence"],
                         result["coverage"]["unmaterialized_claim_ids"])

    def test_overview_names_source_material_newer_than_graph(self):
        vault = self.root / "vault"
        raw = vault / "raw" / "reading-room" / "Primary conversation.md"
        raw.parent.mkdir(parents=True)
        raw.write_text("full transcript", encoding="utf-8")
        # Ensure the material is observably newer on coarse filesystems.
        graph_time = self.database.stat().st_mtime
        raw.touch()
        import os
        os.utime(raw, (graph_time + 5, graph_time + 5))
        with mock.patch.object(research.settings, "get", side_effect=lambda key: (
                str(vault) if key == "vault_root" else None)):
            result = research.overview()
        self.assertEqual("stale", result["freshness"]["status"])
        self.assertEqual(1, result["freshness"]["newer_source_count"])
        self.assertEqual("Primary conversation",
                         result["freshness"]["newer_sources"][0]["title"])

    def test_claim_detail_keeps_canonical_utterance_and_appearances_distinct(self):
        result = research.claim_detail("small teams own outcomes")
        self.assertEqual("claim-1", result["claim"]["claim_id"])
        self.assertEqual(1, result["evidence_total"])
        self.assertEqual(1, result["evidence_returned"])
        self.assertEqual(2, len(result["evidence"][0]["appearances"]))
        self.assertEqual("Product discussion",
                         result["evidence"][0]["event"]["event_title"])
        self.assertEqual("Primary conversation",
                         result["evidence"][0]["source"]["title"])
        self.assertEqual("organization",
                         result["evidence"][0]["evidence_scope"])
        self.assertEqual(1, result["organization_rollup"]
                         ["distinct_speaker_count"])
        self.assertEqual(1, result["organization_rollup"]
                         ["distinct_event_count"])
        self.assertEqual("evt-1", result["events"][0]["event_id"])
        self.assertEqual("src-1", result["sources"][0]["source_id"])
        self.assertEqual("wiki/source-summary.md",
                         result["vault_notes"][0]["path"])
        self.assertEqual(["matched"], [row["id"] for row in
                                      result["application_bridges"]])
        self.assertFalse(result["truncated"])

    def test_context_material_is_preserved_but_excluded_from_org_counts(self):
        con = sqlite3.connect(self.database)
        con.execute(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?)",
            ("src-context", "Outside paper", "2026-05-05",
             "https://outside.example/paper", "https://outside.example/paper"),
        )
        con.execute("INSERT INTO events VALUES (?, ?, ?)",
                    ("evt-context", "Outside publication", "2026-05-05"))
        con.execute(
            "INSERT INTO event_sources VALUES (?, ?, ?, ?, ?, ?)",
            ("evt-context", "src-context", "canonical", "paper", 1.0, 1),
        )
        con.execute(
            "INSERT INTO claim_utterances VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("claim-1", "utt-context", "evt-context", "supports", "", "", 0,
             "A comparison from another organization.",
             "https://outside.example/paper"),
        )
        con.execute(
            "INSERT INTO utterance_appearances VALUES (?, ?, ?, ?, ?, ?)",
            ("app-context", "utt-context", "evt-context", "src-context",
             "A comparison from another organization.",
             "https://outside.example/paper"),
        )
        con.commit()
        con.close()

        result = research.claim_detail("claim-1")
        self.assertEqual(["organization", "context"],
                         [row["evidence_scope"] for row in result["evidence"]])
        self.assertEqual(1, result["organization_rollup"]["utterance_count"])
        self.assertEqual(1, result["organization_rollup"]
                         ["contextual_utterance_count"])

        overview = research.overview()
        claim = overview["claims"][0]
        self.assertEqual("organization", claim["evidence_scope"])
        self.assertEqual(1, claim["organization_rollup"]["utterance_count"])
        self.assertEqual(1, claim["organization_rollup"]
                         ["contextual_utterance_count"])

    def test_source_detail_includes_evidence_and_separate_room_projection(self):
        result = research.source_detail("src-1")
        canonical = result["canonical"]
        self.assertEqual("src-1", canonical["source"]["source_id"])
        self.assertEqual("cap-1", canonical["captures"][0]["capture_id"])
        self.assertEqual("evt-1", canonical["events"][0]["event_id"])
        self.assertEqual("claim-1", canonical["claims"][0]["claim_id"])
        projection = result["projections"]["reading_rooms"][0]
        self.assertEqual("linked_projection", projection["authority"])
        self.assertEqual("owner", projection["vault"]["kind"])
        self.assertEqual("wiki/source-summary.md",
                         result["vault_notes"][0]["path"])

    def test_room_annotation_maps_tracking_variant_without_writing(self):
        result = research.room_annotation("example-labs", item_id="item-1")
        self.assertEqual("linked_projection", result["room_projection"]["authority"])
        self.assertEqual("database", result["graph_annotation"]["authority"])
        self.assertEqual("src-1", result["graph_annotation"]["source"]["source_id"])
        self.assertEqual("wiki/source-summary.md", result["vault_projection"]["note"])

    def test_company_lookup_and_application_context_preserve_bridge_scope(self):
        graph = research.company_lookup("example labs")
        self.assertEqual("example-labs", graph["id"])
        role = {"uid": "role-1", "company": "Example Labs", "title": "Builder"}
        result = research.application_context(role=role)
        self.assertEqual("database", result["research"]["canonical"]["authority"])
        self.assertEqual("personal_local", result["personal_bridge"]["scope"])
        self.assertFalse(result["personal_bridge"]["canonical"])
        self.assertEqual("claim-1", result["personal_bridge"]["taxonomy"]
                         ["themes"][0]["research_claim"])

    def test_queries_leave_database_byte_for_byte_unchanged(self):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        research.overview()
        research.claim_detail("claim-1")
        research.source_detail("src-1")
        research.room_annotation("example-labs", source_id="src-1")
        after = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
