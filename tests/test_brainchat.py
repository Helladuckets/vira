"""Persistent Find chat, concept validation, and session accumulation."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import brainchat


HITS = [
    {"path": "Sessions/alpha.md", "title": "Alpha", "heading": "Decision",
     "text": "The durable chat decision and supporting research."},
    {"path": "wiki/vault.md", "title": "Vault", "heading": "Grounding",
     "text": "Answers must stay grounded in locally indexed notes."},
]


def _answer(text="The decision is recorded in [[Sessions/alpha.md]]."):
    return {
        "answer": text,
        "citations": [{"ref": "Sessions/alpha.md",
                       "path": "Sessions/alpha.md", "title": "Alpha"}],
        "hits": [{"path": "Sessions/alpha.md", "title": "Alpha",
                  "heading": "Decision"}],
    }


def _concepts(term="durable vault chat", weight=0.7):
    return (
        '{"concepts":[{"term":"' + term + '","weight":'
        + str(weight)
        + ',"primary_path":"Sessions/alpha.md",'
          '"related_paths":["wiki/vault.md","invented.md"]}],'
          '"follow_up_questions":["What should the next window show?"],'
          '"topic_clusters":[{"label":"Chat grounding",'
          '"paths":["Sessions/alpha.md","wiki/vault.md","invented.md"]}]}'
    )


class BrainChatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(brainchat, "STORE",
                                  Path(self.tmp.name) / "brain-chat.json")
        patch.start()
        self.addCleanup(patch.stop)

    @mock.patch("server.brainchat.suggest.complete", return_value=_concepts())
    @mock.patch("server.brainchat.vault.ask", return_value=_answer())
    @mock.patch("server.brainchat.vault.search", return_value=HITS)
    def test_first_turn_persists_grounded_chat_and_companions(
            self, search, ask, complete):
        session = brainchat.ask("What did I decide?")

        self.assertEqual(brainchat.current(), session)
        self.assertEqual(session["turns"][0]["question"],
                         "What did I decide?")
        self.assertEqual(session["concepts"][0]["turns"], 1)
        self.assertEqual(session["concepts"][0]["related_paths"],
                         ["wiki/vault.md"])
        self.assertEqual(session["topic_clusters"][0]["paths"],
                         ["Sessions/alpha.md", "wiki/vault.md"])
        self.assertEqual(session["cited"][0]["count"], 1)
        search.assert_called_once_with("What did I decide?", limit=10)
        ask.assert_called_once_with("What did I decide?", hits=HITS)
        self.assertIn("PRIOR CONCEPTS", complete.call_args.args[0])

    @mock.patch("server.brainchat.suggest.complete",
                side_effect=[_concepts(), _concepts(weight=0.8)])
    @mock.patch("server.brainchat.vault.ask",
                side_effect=[_answer(), _answer("It still is [[Sessions/alpha.md]].")])
    @mock.patch("server.brainchat.vault.search", return_value=HITS)
    def test_later_turn_uses_context_and_accumulates_repeated_concepts(
            self, search, ask, complete):
        first = brainchat.ask("What did I decide?")
        second = brainchat.ask("Why?", first["id"])

        self.assertEqual(len(second["turns"]), 2)
        self.assertEqual(second["concepts"][0]["turns"], 2)
        self.assertAlmostEqual(second["concepts"][0]["weight"], 0.85)
        self.assertEqual(second["cited"][0]["count"], 2)
        contextual = ask.call_args_list[1].args[0]
        self.assertIn("EARLIER EXCHANGE", contextual)
        self.assertIn("CURRENT QUESTION:\nWhy?", contextual)

    @mock.patch("server.brainchat.suggest.complete",
                side_effect=RuntimeError("extractor unavailable"))
    @mock.patch("server.brainchat.vault.ask", return_value=_answer())
    @mock.patch("server.brainchat.vault.search", return_value=HITS)
    def test_concept_failure_does_not_discard_answer(self, search, ask, complete):
        session = brainchat.ask("What did I decide?")
        self.assertTrue(session["turns"][0]["answer"])
        self.assertEqual(session["concepts"], [])

    def test_new_chat_becomes_active_without_destroying_prior_chat(self):
        one = brainchat.new()
        two = brainchat.new()
        self.assertNotEqual(one["id"], two["id"])
        self.assertEqual(brainchat.current()["id"], two["id"])


if __name__ == "__main__":
    unittest.main()
