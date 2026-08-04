"""The definition ladder and its write-back.

Every case roots the vault at a tmp fixture. `save()` writes real markdown
into `vault_root()`, so a test that forgot to patch it would edit the
owner's actual Obsidian vault — the readinglist isolation lesson, with
teeth: here the side effect is not a stray row in a store, it is a file in
his wiki. `_DefineCase` patches the root, the store, and the lock together
so they cannot drift apart.
"""
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from server import define


CARD = {
    "term": "Byzantine fault tolerance",
    "rung": "model",
    "sourced": False,
    "family": "distributed systems",
    "rows": [
        {"key": "plain_definition", "label": "Plain definition",
         "value": "A system that keeps working when parts of it lie."},
        {"key": "distinctions", "label": "Synonyms and distinctions",
         "value": "Crash fault tolerance assumes honest failure."},
    ],
    "related": ["consensus", "Paxos"],
    "links": [],
}


class _DefineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "vault"
        (self.root / "wiki").mkdir(parents=True)
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        for p in (
            mock.patch.object(define.vault, "vault_root",
                              return_value=self.root),
            mock.patch.object(define, "STORE", self.data / "glossary.json"),
            mock.patch.object(define, "LOCK", self.data / "glossary.build"),
            mock.patch.object(define.atlasterms, "lookup", return_value=None),
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("VIRA_PASSIVE", None)

    def note(self, stem):
        return (self.root / "wiki" / f"{stem}.md").read_text(encoding="utf-8")


class TermTests(unittest.TestCase):
    def test_a_sentence_is_not_a_term(self):
        self.assertEqual(define.clean_term(
            "the surrounding runtime that turns a model into an agent "
            "operating in a loop"), "")

    def test_punctuation_and_quotes_are_trimmed(self):
        self.assertEqual(define.clean_term('  "harness,"  '), "harness")

    def test_empty_and_oversized_selections_are_refused(self):
        self.assertEqual(define.clean_term(""), "")
        self.assertEqual(define.clean_term("x" * 500), "")


class NoteRoundTripTests(_DefineCase):
    def test_a_card_survives_being_written_and_read_back(self):
        text = define.note_text(CARD, stems={})
        back = define.parse_note(text)
        self.assertEqual(back["term"], CARD["term"])
        self.assertEqual([r["key"] for r in back["rows"]],
                         ["plain_definition", "distinctions"])
        self.assertEqual(back["rows"][0]["value"], CARD["rows"][0]["value"])
        self.assertEqual(back["related"], ["consensus", "Paxos"])
        self.assertFalse(back["sourced"])

    def test_it_is_a_tc_il_concept_page(self):
        text = define.note_text(CARD, stems={})
        self.assertIn("type: concept", text)
        self.assertIn("tags: [", text)
        self.assertIn("created: ", text)
        self.assertIn("# Byzantine fault tolerance", text)
        self.assertIn("## Plain definition", text)

    def test_a_related_term_links_only_when_the_note_exists(self):
        text = define.note_text(CARD, stems={"consensus": Path("x")})
        self.assertIn("- [[consensus|consensus]]", text)
        self.assertIn("- Paxos", text)          # honest loose end, not a
        self.assertNotIn("[[Paxos", text)       # link that resolves nowhere

    def test_links_render_only_when_present(self):
        self.assertNotIn("Read further", define.note_text(CARD, stems={}))
        sourced = dict(CARD, links=[{"label": "origin", "url": "https://a.test"}])
        self.assertIn("- [origin](https://a.test)",
                      define.note_text(sourced, stems={}))


class ValidateTests(unittest.TestCase):
    def test_an_invented_url_is_stripped_from_prose(self):
        card = define._validate(
            {"plain_definition": "A thing, see https://made-up.test/x for more.",
             "related": []}, "thing")
        self.assertNotIn("http", card["rows"][0]["value"])
        self.assertEqual(card["links"], [])
        self.assertFalse(card["sourced"])

    def test_a_card_with_no_definition_is_refused(self):
        with self.assertRaises(define.DefineError):
            define._validate({"trajectory": "Rising."}, "thing")

    def test_related_drops_the_term_itself_and_dedupes(self):
        card = define._validate(
            {"plain_definition": "x",
             "related": ["Thing", "consensus", "consensus", "  "]}, "thing")
        self.assertEqual(card["related"], ["consensus"])


class SaveTests(_DefineCase):
    def test_saving_writes_the_note_and_indexes_it(self):
        card = define.save(CARD)
        self.assertTrue(Path(card["note"]).exists())
        self.assertEqual(card["slug"], "byzantine-fault-tolerance")
        ent = define.entry("byzantine fault tolerance")
        self.assertEqual(ent["slug"], "byzantine-fault-tolerance")

    def test_the_second_lookup_is_free(self):
        define.save(CARD)
        with mock.patch.object(define, "_compose") as compose:
            card = define.lookup("Byzantine fault tolerance")
        compose.assert_not_called()
        self.assertTrue(card["cached"])
        self.assertEqual(card["rows"][0]["value"], CARD["rows"][0]["value"])

    def test_a_resave_preserves_created_and_is_a_no_op(self):
        define.save(CARD)
        first = self.note("byzantine-fault-tolerance")
        define.save(CARD)
        strip = lambda t: "\n".join(l for l in t.splitlines()
                                    if not l.startswith("updated:"))
        self.assertEqual(strip(first), strip(self.note("byzantine-fault-tolerance")))

    def test_it_never_shadows_an_unrelated_note_with_the_same_slug(self):
        (self.root / "wiki" / "consensus.md").write_text(
            "---\ntitle: Consensus\ntype: source-summary\n---\n\nA talk.\n",
            encoding="utf-8")
        card = define.save(dict(CARD, term="consensus", related=[]))
        self.assertEqual(card["slug"], "consensus-term")
        self.assertIn("A talk.", self.note("consensus"))

    def test_passive_refuses_to_write_the_live_vault(self):
        os.environ["VIRA_PASSIVE"] = "1"
        with self.assertRaises(define.DefineError) as e:
            define.save(CARD)
        self.assertIn("passive", str(e.exception))


class BacklinkTests(_DefineCase):
    def _seed(self, term, body):
        card = dict(CARD, term=term, related=[],
                    rows=[{"key": "plain_definition",
                           "label": "Plain definition", "value": body}])
        return define.save(card)

    def test_writing_a_term_points_older_notes_at_it(self):
        self._seed("prompt engineering",
                   "Writing instructions; context engineering is the wider job.")
        define.save(dict(CARD, term="context engineering", related=[],
                         rows=[{"key": "plain_definition",
                                "label": "Plain definition",
                                "value": "Choosing what the model sees."}]))
        older = self.note("prompt-engineering")
        self.assertIn("## Related", older)
        self.assertIn("[[context-engineering|context engineering]]", older)

    def test_it_does_not_link_a_note_that_never_mentions_the_term(self):
        self._seed("woodworking", "Cutting and joining timber.")
        define.save(dict(CARD, term="consensus", related=[]))
        self.assertNotIn("[[consensus", self.note("woodworking"))

    def test_backlinking_is_idempotent(self):
        self._seed("prompt engineering", "See context engineering.")
        for _ in range(3):
            define.save(dict(CARD, term="context engineering", related=[],
                             rows=[{"key": "plain_definition",
                                    "label": "Plain definition",
                                    "value": "Choosing what the model sees."}]))
        self.assertEqual(self.note("prompt-engineering")
                         .count("[[context-engineering"), 1)

    def test_the_body_prose_is_never_rewritten(self):
        self._seed("prompt engineering", "See context engineering.")
        define.save(dict(CARD, term="context engineering", related=[]))
        self.assertIn("See context engineering.",
                      self.note("prompt-engineering"))


class LadderTests(_DefineCase):
    def test_the_atlas_answers_before_the_model(self):
        atlas_card = {"term": "harness", "rung": "atlas", "sourced": True,
                      "rows": [{"key": "plain_definition",
                                "label": "Plain definition",
                                "value": "The surrounding runtime."}],
                      "links": [{"label": "origin", "url": "https://a.test"}],
                      "related": []}
        with mock.patch.object(define.atlasterms, "lookup",
                               return_value=atlas_card), \
             mock.patch.object(define, "_compose") as compose:
            card = define.lookup("harness")
        compose.assert_not_called()
        self.assertEqual(card["rung"], "atlas")
        # banked, so the wiki gains the page and the next lookup is rung 0
        self.assertTrue(Path(card["note"]).exists())

    def test_a_vault_page_titled_the_term_answers_without_a_model(self):
        (self.root / "wiki" / "cap-theorem.md").write_text(
            "---\ntitle: CAP theorem\ntype: concept\n---\n\n"
            "## Plain definition\n\nPick two of three.\n", encoding="utf-8")
        with mock.patch.object(define, "_compose") as compose:
            card = define.lookup("CAP theorem")
        compose.assert_not_called()
        self.assertEqual(card["rows"][0]["value"], "Pick two of three.")

    def test_a_mention_is_not_a_definition(self):
        """A note that talks about a term all day still does not define it."""
        (self.root / "wiki" / "some-talk.md").write_text(
            "---\ntitle: A talk\n---\n\nquorum quorum quorum.\n",
            encoding="utf-8")
        composed = dict(CARD, term="quorum")
        with mock.patch.object(define, "_compose",
                               return_value=composed) as compose, \
             mock.patch.object(define, "_context", return_value=[]):
            define.lookup("quorum")
        compose.assert_called_once()

    def test_the_model_rung_banks_its_answer(self):
        with mock.patch.object(define, "_compose", return_value=dict(CARD)), \
             mock.patch.object(define, "_context", return_value=[]):
            card = define.lookup("Byzantine fault tolerance")
        self.assertTrue(Path(card["note"]).exists())
        self.assertEqual(define.entry("Byzantine fault tolerance")["rung"],
                         "model")

    def test_a_nonsense_selection_raises_rather_than_calling_the_model(self):
        with mock.patch.object(define, "_compose") as compose:
            with self.assertRaises(define.DefineError):
                define.lookup("this is far too long to be a term at all okay")
        compose.assert_not_called()

    def test_passive_still_answers_but_does_not_bank(self):
        os.environ["VIRA_PASSIVE"] = "1"
        with mock.patch.object(define, "_compose", return_value=dict(CARD)), \
             mock.patch.object(define, "_context", return_value=[]):
            card = define.lookup("Byzantine fault tolerance")
        self.assertEqual(card["rows"][0]["value"], CARD["rows"][0]["value"])
        self.assertIsNone(define.entry("Byzantine fault tolerance"))


class SourcePromptTests(_DefineCase):
    def test_the_prompt_names_the_note_it_must_correct(self):
        define.save(CARD)
        p = define.source_prompt("Byzantine fault tolerance")
        self.assertIn("byzantine-fault-tolerance.md", p)
        self.assertIn("Etymology and lineage", p)
        self.assertIn("vira_sourced: true", p)

    def test_it_still_composes_for_a_term_never_written(self):
        self.assertIn("not yet written", define.source_prompt("quorum"))


if __name__ == "__main__":
    unittest.main()
