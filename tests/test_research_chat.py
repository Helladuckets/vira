"""End-to-end Find chat grounding over a canonical research graph."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from server import brainchat, main, research, settings


SCHEMA = """
CREATE TABLE sources (
    source_id TEXT PRIMARY KEY, title TEXT, publication_date TEXT,
    original_url TEXT, canonical_url TEXT, voice_scope TEXT,
    local_pointer TEXT
);
CREATE TABLE events (
    event_id TEXT PRIMARY KEY, event_title TEXT, event_date TEXT,
    venue TEXT, publisher TEXT, canonical_source_id TEXT
);
CREATE TABLE event_sources (
    event_id TEXT, source_id TEXT, is_canonical INTEGER
);
CREATE TABLE claims (
    claim_id TEXT PRIMARY KEY, claim_label TEXT, category TEXT,
    description TEXT, patterns TEXT, personal_relevance_score INTEGER
);
CREATE TABLE claim_rollups (
    claim_id TEXT PRIMARY KEY, claim_label TEXT, category TEXT,
    personal_relevance_score INTEGER, distinct_speaker_count INTEGER,
    distinct_event_count INTEGER, utterance_count INTEGER,
    appearance_count INTEGER, evidence_status TEXT
);
CREATE TABLE claim_utterances (
    claim_id TEXT, utterance_id TEXT, event_id TEXT, relationship TEXT,
    mapping_method TEXT, matched_basis TEXT, speaker_name TEXT,
    speaker_person_id TEXT, speaker_verified INTEGER,
    timestamp_seconds REAL, locator TEXT, canonical_text TEXT,
    source_url TEXT
);
CREATE TABLE utterance_appearances (
    appearance_id TEXT PRIMARY KEY, utterance_id TEXT, event_id TEXT,
    source_id TEXT, text TEXT, source_url TEXT
);
"""


class ResearchChatEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.corpus = self.root / "self" / "14-anthropic" / "corpus"
        self.data = self.corpus / "data"
        self.data.mkdir(parents=True)
        self.database = self.data / "research.sqlite"
        con = sqlite3.connect(self.database)
        con.executescript(SCHEMA)

        sources = [
            ("cat-source", "Cat Wu interview", "2026-04-23",
             "https://www.youtube.com/watch?v=cat123",
             "https://www.youtube.com/watch?v=cat123",
             "direct_interview_or_authorship",
             self._transcript("raw/cat-wu.md", "Complete Cat Wu transcript.")),
            ("boris-source", "Boris Cherny interview", "2026-02-17",
             "https://www.youtube.com/watch?v=boris123",
             "https://www.youtube.com/watch?v=boris123",
             "direct_interview_or_authorship",
             self._transcript("raw/boris-cherny.md", "Complete Boris Cherny transcript.")),
            ("context-source", "Outside laboratory article", "2025-01-02",
             "https://outside.example/first-principles",
             "https://outside.example/first-principles",
             "third_party_or_unknown",
             self._transcript("raw/outside.md", "Complete outside transcript.")),
            ("incidental-source", "Short Anthropic interview", "2025-06-01",
             "https://www.youtube.com/watch?v=incidental123",
             "https://www.youtube.com/watch?v=incidental123",
             "direct_interview_or_authorship",
             self._transcript("raw/incidental.md", "Complete incidental transcript.")),
        ]
        con.executemany("INSERT INTO sources VALUES (?,?,?,?,?,?,?)", sources)
        events = [
            ("cat-event", "How Anthropic's product team moves", "2026-04-23",
             "Lenny's Podcast", "Lenny's Podcast", "cat-source"),
            ("boris-event", "Inside Claude Code", "2026-02-17",
             "Y Combinator Lightcone Podcast", "Y Combinator", "boris-source"),
            ("context-event", "Outside laboratory discussion", "2025-01-02",
             "Outside Podcast", "Outside Media", "context-source"),
            ("incidental-event", "Short Anthropic interview", "2025-06-01",
             "Example Podcast", "Example Media", "incidental-source"),
        ]
        con.executemany("INSERT INTO events VALUES (?,?,?,?,?,?)", events)
        con.executemany("INSERT INTO event_sources VALUES (?,?,1)", [
            ("cat-event", "cat-source"),
            ("boris-event", "boris-source"),
            ("context-event", "context-source"),
            ("incidental-event", "incidental-source"),
        ])
        claim = (
            "first-principles", "First-principles reasoning adapts to change",
            "hiring", "Reason from goals when old playbooks stop transferring.",
            "first principles | first principles thinking", 90,
        )
        con.execute("INSERT INTO claims VALUES (?,?,?,?,?,?)", claim)
        con.execute("INSERT INTO claim_rollups VALUES (?,?,?,?,?,?,?,?,?)", (
            "first-principles", claim[1], "hiring", 90, 3, 4, 4, 4,
            "verified_speaker_evidence",
        ))
        evidence = [
            ("first-principles", "cat-utterance", "cat-event", "supports",
             "controlled_semantic_pattern", "first principles thinking",
             "Cat Wu", "cat-wu", 1, 4778, "1:19:38",
             "First-principles thinking lets you deduce the right action from what the team needs.",
             "https://www.youtube.com/watch?v=cat123"),
            ("first-principles", "boris-utterance", "boris-event", "supports",
             "manual_verified_seed", "first principles", "Boris Cherny",
             "boris-cherny", 1, 993, "00:16:33",
             "The biggest skill is thinking scientifically and reasoning from first principles.",
             "https://www.youtube.com/watch?v=boris123&t=993s"),
            ("first-principles", "context-utterance", "context-event", "supports",
             "controlled_semantic_pattern", "first principles", "", "", 0,
             None, "article", "This outside article mentions first principles without an Anthropic speaker.",
             "https://outside.example/first-principles"),
            ("first-principles", "incidental-utterance", "incidental-event",
             "supports", "controlled_semantic_pattern", "first principles",
             "Alex Albert", "alex-albert", 1, 300, "5:00",
             "I heard the phrase first principles yesterday.",
             "https://www.youtube.com/watch?v=incidental123"),
        ]
        con.executemany("INSERT INTO claim_utterances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        evidence)
        con.executemany("INSERT INTO utterance_appearances VALUES (?,?,?,?,?,?)", [
            ("cat-appearance", "cat-utterance", "cat-event", "cat-source",
             evidence[0][11], evidence[0][12]),
            ("boris-appearance", "boris-utterance", "boris-event", "boris-source",
             evidence[1][11], evidence[1][12]),
            ("context-appearance", "context-utterance", "context-event", "context-source",
             evidence[2][11], evidence[2][12]),
            ("incidental-appearance", "incidental-utterance", "incidental-event",
             "incidental-source", evidence[3][11], evidence[3][12]),
        ])
        con.commit()
        con.close()

        self.manifest_path = self.data / "manifest.json"
        self.manifest = {
            "built": "2026-08-06T12:00:00Z", "database": "research.sqlite",
            "tables": {"claims": 1, "sources": 4, "events": 4,
                       "utterances": 4},
        }
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        (self.corpus / "claim_taxonomy.json").write_text(json.dumps({
            "claims": [{"claim_id": "first-principles"}]
        }), encoding="utf-8")
        self.graph = {
            "id": "anthropic", "name": "Anthropic", "company": "Anthropic",
            "manifest_path": self.manifest_path,
            "database_path": self.database,
            "taxonomy_path": self.corpus / "application_bridge_taxonomy.json",
            "room": "anthropic", "manifest": self.manifest, "error": "",
        }

        patches = [
            mock.patch.object(brainchat, "STORE", self.root / "brain-chat.json"),
            mock.patch.object(research, "_graphs", return_value=[self.graph]),
            mock.patch.object(research.readingroom, "load_room", return_value=None),
            mock.patch.object(research.readingroom, "list_rooms", return_value=[]),
            mock.patch.object(settings, "get", side_effect=self._setting),
            mock.patch("server.brainchat.suggest.complete", return_value=(
                '{"concepts":[],"follow_up_questions":[],"topic_clusters":[]}'
            )),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.client = TestClient(main.app, raise_server_exceptions=False)

    def _transcript(self, relative, text):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return str(path)

    def _setting(self, key):
        if key == "vault_root":
            return str(self.vault)
        return settings.DEFAULTS.get(key, "")

    def test_find_chat_groups_corroboration_with_complete_provenance(self):
        response = self.client.post("/api/find/chat", json={
            "question": (
                "Show me where else first-principles thinking is defined "
                "by Anthropic employees."
            )
        })
        self.assertEqual(200, response.status_code, response.text)
        turn = response.json()["session"]["turns"][0]
        grounded = turn["research"]
        self.assertEqual("canonical_research_graph", grounded["authority"])
        self.assertEqual(
            ["Boris Cherny", "Cat Wu"],
            [group["speaker"] for group in grounded["substantive"]],
        )
        self.assertEqual(1, len(grounded["context"]))
        self.assertEqual(["Alex Albert"], [
            group["speaker"] for group in grounded["incidental"]
        ])
        for group in grounded["substantive"]:
            for event_group in group["events"]:
                event = event_group["event"]
                self.assertTrue(event["event_title"])
                self.assertTrue(event["event_date"])
                self.assertTrue(event["venue"])
                for item in event_group["evidence"]:
                    self.assertTrue(item["canonical_text"])
                    self.assertTrue(item["speaker_name"])
                    self.assertIn("t=", item["timestamped_original_url"])
                    self.assertTrue(item["full_transcript"]["path"].startswith("raw/"))
                    self.assertTrue(item["vault_links"])
                    self.assertTrue(item["source_links"][0]["url"])
        self.assertIn("Boris Cherny", turn["answer"])
        self.assertIn("Cat Wu", turn["answer"])
        self.assertIn("Non-Anthropic context", turn["answer"])


if __name__ == "__main__":
    unittest.main()
