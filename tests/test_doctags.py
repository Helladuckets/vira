"""Document tagging — the shared vocabulary behind the grouped library.

Two things these cases exist to hold, both found by measuring rather than by
reading the code:

  - rung 1 must match the document's SUBJECT, never the shelf it sits on.
    Matching the locator's parent directories tagged all 76 plans `plans` and
    every brief `brief` — 48 confident false hits that read exactly like a
    working grouping.
  - a thin title (`2026-08-03 2133 vira`) carries no subject at all, and 423
    of 519 live documents are that shape. They are tagged from an excerpt, and
    that read happens in the PASS, never on a request.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import doctags


VOCAB = {"module": [("media-index", 17), ("media", 9), ("reader", 8),
                    ("brief", 32), ("plans", 5), ("atlas", 5), ("ui", 3)],
         "subproject": [], "theme": [], "concept": []}


def doc(did, title, kind="plan", locator="/docs/plans/x.html"):
    return {"id": did, "title": title, "kind": kind, "locator": locator}


class MatchText(unittest.TestCase):
    def test_the_locators_parent_directories_are_not_the_subject(self):
        it = doc("d1", "Layout templates",
                 locator="/docs/plans/2026-07-24-layout-templates.html")
        self.assertNotIn("docs", doctags.match_text(it))
        self.assertNotIn("plans", doctags.match_text(it))
        self.assertIn("layout templates", doctags.match_text(it))

    def test_the_kind_is_not_matched_against(self):
        """A brief matching the module `brief` says only that a brief is a
        brief. 10 of 10 live briefs hit on exactly this."""
        it = doc("d1", "Morning dossier", kind="brief",
                 locator="/vault/Briefs/2026-08-03 morning-dossier.md")
        self.assertNotIn("brief", doctags.match_text(it).lower())

    def test_the_slug_leaf_carries_the_subject(self):
        it = doc("d1", "", kind="walkthrough",
                 locator="/walkthroughs/vira-reader-2026-07-27/")
        self.assertIn("reader", doctags.match_text(it))

    def test_digits_are_dropped_from_the_slug(self):
        it = doc("d1", "", locator="/docs/plans/2026-07-24-1533-layout.html")
        self.assertNotIn("2026", doctags.match_text(it))

    def test_doc_text_keeps_the_kind_for_the_model(self):
        """The model gets the kind as real context — it is only the substring
        MATCH that must not see it."""
        self.assertIn("plan", doctags.doc_text(doc("d1", "A thing")))


class GuessModule(unittest.TestCase):
    def test_longest_match_wins(self):
        it = doc("d1", "Media index rebuild")
        self.assertEqual(doctags.guess_module(doctags.match_text(it), VOCAB),
                         "media-index")

    def test_a_hyphenated_tag_matches_a_spaced_title(self):
        self.assertEqual(doctags.guess_module("the media index pass", VOCAB),
                         "media-index")

    def test_a_hyphenated_tag_matches_a_slug(self):
        self.assertEqual(doctags.guess_module("vira media-index 2026", VOCAB),
                         "media-index")

    def test_short_tags_never_match(self):
        """A two-letter tag matches inside ordinary words, and a wrong group
        reads as fact."""
        self.assertEqual(doctags.guess_module("building the ui shell", VOCAB), "")

    def test_stop_tags_never_match(self):
        self.assertEqual(
            doctags.guess_module("vira session walkthrough",
                                 {"module": [("vira", 9), ("session", 4)]}), "")

    def test_a_partial_word_is_not_a_match(self):
        self.assertEqual(doctags.guess_module("readership numbers", VOCAB), "")

    def test_no_match_is_empty_not_a_guess(self):
        self.assertEqual(doctags.guess_module("something unrelated", VOCAB), "")


class ThinTitles(unittest.TestCase):
    def test_a_date_stamped_retro_title_is_thin(self):
        for t in ("2026-08-03 2133 vira", "2026-08-03 cross-project",
                  "2026-08-03 day crm", "2026-05-17 1009 thedurham-nyc"):
            self.assertTrue(doctags._is_thin(t), t)

    def test_a_real_title_is_not_thin(self):
        for t in ("The Reader, rebuilt around its rooms",
                  "Layout templates", "A boring dashboard, and any model"):
            self.assertFalse(doctags._is_thin(t), t)


class Excerpt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _at(self, name, body):
        p = self.root / name
        p.write_text(body, encoding="utf-8")
        return p

    def test_frontmatter_is_stripped(self):
        p = self._at("a.md", "---\ntitle: x\n---\nThe goal was the Reader.\n")
        with mock.patch.object(doctags.readinglist, "source_path",
                               return_value=p):
            self.assertTrue(doctags.excerpt({}).startswith("The goal"))

    def test_html_tags_and_scripts_are_stripped(self):
        p = self._at("a.html",
                     "<script>var x=1</script><h1>Title</h1><p>Body here</p>")
        with mock.patch.object(doctags.readinglist, "source_path",
                               return_value=p):
            got = doctags.excerpt({})
        self.assertNotIn("var x", got)
        self.assertIn("Body here", got)

    def test_a_missing_file_degrades_to_empty(self):
        with mock.patch.object(doctags.readinglist, "source_path",
                               return_value=self.root / "nope.md"):
            self.assertEqual(doctags.excerpt({}), "")

    def test_an_unresolvable_locator_degrades_to_empty(self):
        with mock.patch.object(doctags.readinglist, "source_path",
                               return_value=None):
            self.assertEqual(doctags.excerpt({}), "")

    def test_the_excerpt_is_bounded(self):
        p = self._at("a.md", "word " * 5000)
        with mock.patch.object(doctags.readinglist, "source_path",
                               return_value=p):
            self.assertLessEqual(len(doctags.excerpt({})),
                                 doctags.EXCERPT_CHARS)


class ParseModelOutput(unittest.TestCase):
    def setUp(self):
        self.batch = [doc("d1", "One"), doc("d2", "Two")]

    def test_plain_json(self):
        got = doctags._parse('{"1": {"module": ["reader"]}}', self.batch)
        self.assertEqual(got["d1"]["module"], ["reader"])

    def test_a_fenced_block_is_tolerated(self):
        got = doctags._parse('```json\n{"2": {"module": ["brief"]}}\n```',
                             self.batch)
        self.assertEqual(got["d2"]["module"], ["brief"])

    def test_prose_around_the_object_is_tolerated(self):
        got = doctags._parse('Sure!\n{"1": {"module": ["atlas"]}}\nDone.',
                             self.batch)
        self.assertEqual(got["d1"]["module"], ["atlas"])

    def test_an_index_outside_the_batch_is_dropped(self):
        self.assertEqual(doctags._parse('{"9": {"module": ["x"]}}', self.batch),
                         {})

    def test_junk_is_dropped_rather_than_raising(self):
        self.assertEqual(doctags._parse("not json at all", self.batch), {})

    def test_tags_are_validated_by_ideatags(self):
        """Bad spellings and over-long lists are dropped, not stored — the
        model proposes, _clean_tags decides."""
        got = doctags._parse(
            '{"1": {"module": ["Reading Room", "a b c", "x", "y", "z"]}}',
            self.batch)
        mods = got["d1"]["module"]
        self.assertIn("reading-room", mods)
        self.assertLessEqual(len(mods), 3)


class Overlay(unittest.TestCase):
    def test_owner_additions_win_and_drops_lose(self):
        derived = {"module": ["reader"], "subproject": [], "theme": [],
                   "concept": []}
        item = {"tags_add": {"module": ["brief"]}, "tags_drop": ["reader"]}
        got = doctags._overlay(derived, item)
        self.assertEqual(got["module"], ["brief"])

    def test_no_corrections_is_the_derived_set(self):
        derived = {"module": ["reader"], "subproject": [], "theme": [],
                   "concept": []}
        self.assertEqual(doctags._overlay(derived, {})["module"], ["reader"])


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "doc-index.json"
        p = mock.patch.object(doctags, "STORE", self.store)
        p.start()
        self.addCleanup(p.stop)

    def test_annotate_attaches_tags_and_the_flag(self):
        it = doc("d1", "One")
        doctags._write_tags({"d1": {"module": ["reader"], "subproject": [],
                                    "theme": [], "concept": []}}, [it])
        got = doctags.annotate([it])[0]
        self.assertEqual(got["tags"]["module"], ["reader"])
        self.assertTrue(got["tagged"])

    def test_an_untagged_document_reads_as_untagged(self):
        got = doctags.annotate([doc("d1", "One")])[0]
        self.assertFalse(got["tagged"])
        self.assertEqual(got["tags"]["module"], [])

    def test_a_changed_title_makes_the_entry_pending_again(self):
        """The sidecar is keyed by id AND a hash of what was read, so editing
        a document re-tags it on the next pass."""
        it = doc("d1", "One")
        doctags._write_tags({"d1": {"module": ["reader"]}}, [it])
        s = doctags._read()
        self.assertEqual(doctags._pending([it], s), [])
        self.assertEqual(len(doctags._pending([doc("d1", "Renamed")], s)), 1)

    def test_vocabulary_counts_across_documents(self):
        items = [doc("d1", "One"), doc("d2", "Two")]
        doctags._write_tags(
            {"d1": {"module": ["reader"]}, "d2": {"module": ["reader"]}}, items)
        self.assertEqual(doctags.vocabulary(items)["module"], [("reader", 2)])

    def test_status_counts_pending(self):
        items = [doc("d1", "One"), doc("d2", "Two")]
        doctags._write_tags({"d1": {"module": ["reader"]}}, items)
        st = doctags.status(items)
        self.assertEqual((st["total"], st["tagged"], st["pending"]), (2, 1, 1))


class TagPass(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(doctags, "STORE",
                              Path(self.tmp.name) / "doc-index.json")
        p.start()
        self.addCleanup(p.stop)
        v = mock.patch.object(doctags, "_merged_vocab", return_value=VOCAB)
        v.start()
        self.addCleanup(v.stop)

    def test_rung_one_tags_without_any_model_call(self):
        items = [doc("d1", "The reader queue")]
        with mock.patch.object(doctags.suggest, "complete") as m:
            m.side_effect = AssertionError("must not be called")
            out = doctags.tag_pending(items, batches=0)
        self.assertEqual(out["rung1"], 1)
        self.assertEqual(doctags.annotate(items)[0]["tags"]["module"],
                         ["reader"])

    def test_a_model_outage_keeps_what_rung_one_wrote(self):
        items = [doc("d1", "The reader queue"), doc("d2", "Something else")]
        with mock.patch.object(doctags.suggest, "complete",
                               side_effect=RuntimeError("backend down")):
            out = doctags.tag_pending(items, batches=1)
        self.assertEqual(out["rung1"], 1)
        self.assertEqual(out["tagged"], 0)
        self.assertEqual(doctags.annotate(items)[0]["tags"]["module"],
                         ["reader"])

    def test_the_model_fills_in_what_rung_one_could_not_place(self):
        items = [doc("d1", "Something with no known module")]
        with mock.patch.object(
                doctags.suggest, "complete",
                return_value='{"1": {"module": ["onboarding"]}}'):
            out = doctags.tag_pending(items, batches=1)
        self.assertEqual(out["tagged"], 1)
        self.assertEqual(doctags.annotate(items)[0]["tags"]["module"],
                         ["onboarding"])

    def test_nothing_pending_is_a_no_op(self):
        self.assertEqual(doctags.tag_pending([], batches=3)["batches"], 0)

    def test_refresh_is_bounded(self):
        """A click can never become an unbounded spend."""
        seen = []

        def fake(items=None, batches=1):
            seen.append(batches)
            return {"tagged": 0, "batches": 0, "pending": 0, "rung1": 0}

        with mock.patch.object(doctags, "tag_pending", side_effect=fake):
            doctags.refresh(9999)
        self.assertEqual(seen, [doctags.MAX_BATCHES])

    def test_a_thin_title_puts_an_excerpt_in_the_prompt(self):
        items = [doc("d1", "2026-08-03 day vira", kind="retro",
                     locator="Sessions/2026-08-03 day vira.md")]
        seen = {}

        def capture(prompt):
            seen["p"] = prompt
            return '{"1": {"module": ["reader"]}}'

        with mock.patch.object(doctags, "excerpt",
                               return_value="Rebuilt the Reader tonight."), \
             mock.patch.object(doctags.suggest, "complete",
                               side_effect=capture):
            doctags.tag_pending(items, batches=1)
        self.assertIn("Rebuilt the Reader tonight.", seen["p"])

    def test_a_real_title_does_not_pay_for_a_file_read(self):
        items = [doc("d1", "The Reader, rebuilt around its rooms")]
        with mock.patch.object(doctags, "excerpt") as ex, \
             mock.patch.object(doctags.suggest, "complete",
                               return_value='{"1": {"module": ["reader"]}}'):
            doctags.tag_pending(items, batches=1)
        ex.assert_not_called()


class Wiring(unittest.TestCase):
    """A background worker that is built and never started is dead code that
    reads as a feature.

    doctags.Indexer shipped in the first cut of this module with no
    instantiation and no `.start()` — so the 380 untagged documents on the
    live library would have sat there forever while `status()` cheerfully
    reported them as pending. Same shape as the branch guard's dropped spec
    fields and `model_used`'s missing writer: a reader with no writer, silent
    and looking correct.

    The check is general on purpose. It does not name doctags — it asserts
    that EVERY worker main.py constructs at module level is also started, so
    the next one cannot ship dark either."""

    def _main_src(self):
        return (Path(__file__).resolve().parent.parent
                / "server" / "main.py").read_text(encoding="utf-8")

    def test_every_worker_main_builds_is_also_started(self):
        src = self._main_src()
        built = set(re.findall(
            r"^(\w+)\s*=\s*\w+\.(?:Indexer|Watcher|Poller|Scheduler|Sweeper)\(",
            src, re.M))
        self.assertTrue(built, "no workers found — has main.py moved?")
        unstarted = {n for n in built if f"{n}.start()" not in src}
        self.assertEqual(unstarted, set(),
                         f"built but never started: {sorted(unstarted)}")

    def test_the_document_indexer_is_one_of_them(self):
        src = self._main_src()
        self.assertIn("doctags.Indexer(", src)
        self.assertIn("doc_indexer.start()", src)

    def test_its_interval_key_has_a_default(self):
        """settings.get raises KeyError on a key with no DEFAULTS entry, and
        this one is read at module import — so a missing default is not a
        degraded worker, it is an app that cannot boot (the mail_body_index
        incident, one import earlier)."""
        from server import settings
        self.assertIn("doc_tag_interval_min", settings.DEFAULTS)
        self.assertIsInstance(settings.get("doc_tag_interval_min"), int)


class Prompt(unittest.TestCase):
    def test_the_vocabulary_in_use_is_handed_to_the_tagger(self):
        """Tagging each document independently yields reader/Reader/
        reading-room for one subject, which groups nothing."""
        p = doctags._prompt([doc("d1", "One")], VOCAB)
        self.assertIn("media-index", p)
        self.assertIn("Already in use", p)
        self.assertIn("REUSE", p)

    def test_an_empty_axis_says_so_rather_than_reading_blank(self):
        p = doctags._prompt([doc("d1", "One")],
                            {"module": [], "subproject": [], "theme": [],
                             "concept": []})
        self.assertIn("(nothing yet)", p)

    def test_every_axis_is_taught(self):
        p = doctags._prompt([doc("d1", "One")], VOCAB)
        for ax in doctags.AXIS_IDS:
            self.assertIn(ax, p)


if __name__ == "__main__":
    unittest.main()
