"""Idea tagging + similarity — the derived layer that makes the Queue
searchable, groupable and self-aware about repetition.

Covers the two-rung split (find.py's pattern): the deterministic half
(tokens, vectors, corpus-relative cosine, fusion) stands entirely on its
own and is tested with no model and no Ollama; the tagging and fold-in
halves each make exactly one model call per batch and are tested with it
mocked, including what happens when the backend is dead.

The calibration cases are the ones worth keeping: a raw cosine floor does
not work on a single-domain backlog, so closeness is scored relative to
the corpus, and a pair that is merely typical must score near zero even
when its raw cosine is 0.64.

Run: .venv/bin/python -m unittest discover tests
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from server import ideas, ideatags


def idea(id_, text, status="open", project="Vira", **kw):
    d = {"id": id_, "text": text, "status": status, "project": project,
         "source": "manual", "note": "",
         "created": "2026-07-01T00:00:00+00:00",
         "updated": "2026-07-01T00:00:00+00:00"}
    d.update(kw)
    return d


class StoreCase(unittest.TestCase):
    """Every test drives its own throwaway sidecar and idea store — the
    live backlog is never touched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = root / "idea-index.json"
        self.ideas_store = root / "ideas.json"
        self.ideas_store.write_text(json.dumps({"items": [], "projects": []}),
                                    encoding="utf-8")
        for p in (mock.patch.object(ideatags, "STORE", self.store),
                  mock.patch.object(ideas, "STORE", self.ideas_store)):
            p.start()
            self.addCleanup(p.stop)
        ideatags._base_cache.clear()

    def seed(self, items):
        self.ideas_store.write_text(
            json.dumps({"items": items, "projects": []}), encoding="utf-8")

    def put_tags(self, id_, text, tags):
        s = ideatags._read()
        s["entries"].setdefault(id_, {})["tags"] = tags
        s["entries"][id_]["hash"] = ideatags._hash(text)
        s["entries"][id_]["tagged"] = "2026-07-01T00:00:00+00:00"
        ideatags._write(s)

    def put_vec(self, item, vec):
        s = ideatags._read()
        e = s["entries"].setdefault(item["id"], {})
        e["vec"] = ideatags._pack(np.array(vec, dtype="float32"))
        e["vhash"] = ideatags._hash(ideatags._embed_text(item))
        ideatags._write(s)


# ---------- tag shape ----------

class NormalizeTests(unittest.TestCase):
    def test_one_spelling_per_tag(self):
        for raw in ("Reading Room", "reading_room", "  READING-ROOM ",
                    "reading   room"):
            self.assertEqual(ideatags.norm_tag(raw), "reading-room")

    def test_rejects_unusable(self):
        for raw in ("", "   ", "!!!", "-", "x" * 40, None):
            self.assertEqual(ideatags.norm_tag(raw), "")

    def test_punctuation_is_normalized_away_not_rejected(self):
        """Deliberately lenient: a tag that is usable after cleanup is
        kept, because throwing away "read/write" would lose a real tag
        over a stray character."""
        self.assertEqual(ideatags.norm_tag("read/write"), "read-write")
        self.assertEqual(ideatags.norm_tag("!!bad"), "bad")

    def test_clean_tags_caps_and_dedupes(self):
        out = ideatags._clean_tags({
            "module": ["Reader", "reader", "queue", "brief", "atlas"],
            "theme": "Mobile Layout",              # a bare string is allowed
            "bogus_axis": ["nope"],
        })

        self.assertEqual(out["module"], ["reader", "queue", "brief"])  # max 3
        self.assertEqual(out["theme"], ["mobile-layout"])
        self.assertEqual(out["subproject"], [])
        self.assertNotIn("bogus_axis", out)

    def test_clean_tags_drops_bad_spellings(self):
        out = ideatags._clean_tags({"concept": ["ok-tag", "!!", "x" * 40]})
        self.assertEqual(out["concept"], ["ok-tag"])


class OverlayTests(StoreCase):
    def test_owner_add_and_drop_beat_derived(self):
        it = idea("i1", "some idea",
                  tags_add={"module": ["reader"]}, tags_drop=["queue"])
        self.put_tags("i1", "some idea", {"module": ["queue"], "theme": ["x"]})
        tags = ideatags.tags_for(it)
        self.assertEqual(tags["module"], ["reader"])
        self.assertEqual(tags["theme"], ["x"])

    def test_corrections_survive_a_retag(self):
        """The whole reason corrections live on the idea and not in the
        sidecar: re-tagging rewrites derived values wholesale."""
        it = idea("i1", "some idea", tags_drop=["queue"])
        self.put_tags("i1", "some idea", {"module": ["queue"]})
        self.put_tags("i1", "some idea", {"module": ["queue", "brief"]})
        self.assertEqual(ideatags.tags_for(it)["module"], ["brief"])

    def test_update_keeps_add_and_drop_consistent(self):
        self.seed([idea("i1", "some idea")])
        ideas.update("i1", tags_add={"module": ["reader"]})
        it = ideas.update("i1", tags_drop=["reader"])
        self.assertEqual(it.get("tags_add", {}), {})
        self.assertEqual(it["tags_drop"], ["reader"])

    def test_annotate_attaches_tags(self):
        self.seed([idea("i1", "some idea")])
        self.put_tags("i1", "some idea", {"module": ["reader"]})
        rows = ideatags.annotate()
        self.assertEqual(rows[0]["tags"]["module"], ["reader"])
        self.assertTrue(rows[0]["tagged"])

    def test_vocabulary_counts_and_orders(self):
        self.seed([idea("i1", "a"), idea("i2", "b"), idea("i3", "c")])
        for i, t in (("i1", ["reader"]), ("i2", ["reader"]), ("i3", ["brief"])):
            self.put_tags(i, {"i1": "a", "i2": "b", "i3": "c"}[i],
                          {"module": t})
        vocab = ideatags.vocabulary()
        self.assertEqual(vocab["module"], [("reader", 2), ("brief", 1)])


# ---------- the deterministic half ----------

class ClosenessTests(unittest.TestCase):
    """Corpus-relative cosine. Measured on the real 135-idea backlog:
    off-diagonal cosine ran min 0.38 / median 0.64 / p99 0.78, so a fixed
    floor is meaningless — the median pair must score 0 and only the
    unusually close pairs may approach 1."""

    def test_median_pair_scores_zero(self):
        self.assertEqual(ideatags._cos_rel(0.64, 0.64, 0.78), 0.0)

    def test_top_pair_scores_one(self):
        self.assertEqual(ideatags._cos_rel(0.78, 0.64, 0.78), 1.0)

    def test_clamped_both_ends(self):
        self.assertEqual(ideatags._cos_rel(0.20, 0.64, 0.78), 0.0)
        self.assertEqual(ideatags._cos_rel(0.99, 0.64, 0.78), 1.0)

    def test_midpoint_is_midpoint(self):
        self.assertAlmostEqual(ideatags._cos_rel(0.71, 0.64, 0.78), 0.5,
                               places=6)

    def test_degenerate_space_does_not_divide_by_zero(self):
        """A space where every idea is identical has no spread to read,
        so nothing may score as unusually close — including a pair at
        cosine 1.0, which there is merely typical."""
        M = np.ones((6, 4), dtype="float32")
        M /= np.linalg.norm(M, axis=1, keepdims=True)
        base, top = ideatags._baseline(M)
        self.assertGreater(top, base)
        self.assertEqual(ideatags._cos_rel(1.0, base, top), 0.0)


class ScoringTests(StoreCase):
    def test_shared_tags_raise_the_score(self):
        a = idea("a", "make the reader remember what I have read")
        b = idea("b", "completely unrelated wording about invoices")
        c = idea("c", "different words entirely regarding billing")
        self.put_tags("a", a["text"], {"module": ["reader"]})
        self.put_tags("b", b["text"], {"module": ["reader"]})
        self.put_tags("c", c["text"], {"module": ["mail"]})
        rows = {r["id"]: r for r in ideatags.score_pairs(a, [b, c])}
        self.assertGreater(rows["b"]["score"], rows["c"]["score"])
        self.assertIn("same module: reader", rows["b"]["reasons"])

    def test_no_vectors_reports_text_basis(self):
        a = idea("a", "alpha beta gamma delta")
        b = idea("b", "alpha beta gamma epsilon")
        rows = ideatags.score_pairs(a, [b])
        self.assertEqual(rows[0]["basis"], "text")
        self.assertGreater(rows[0]["score"], 0)

    def test_vectors_report_vector_basis(self):
        a = idea("a", "one")
        b = idea("b", "two")
        c = idea("c", "three")
        self.seed([a, b, c])
        self.put_vec(a, [1.0, 0.0, 0.0])
        self.put_vec(b, [0.98, 0.2, 0.0])
        self.put_vec(c, [0.0, 0.0, 1.0])
        rows = {r["id"]: r for r in ideatags.score_pairs(a, [b, c])}
        self.assertEqual(rows["b"]["basis"], "vector")
        self.assertGreater(rows["b"]["closeness"], rows["c"]["closeness"])

    def test_target_never_scores_against_itself(self):
        a = idea("a", "the same idea")
        rows = ideatags.score_pairs(a, [a, idea("b", "another idea")])
        self.assertEqual([r["id"] for r in rows], ["b"])


class RelatedTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.items = [
            idea("a", "alpha beta gamma reader queue"),
            idea("b", "alpha beta gamma reader backlog"),
            idea("c", "alpha beta gamma reader shipped", status="done"),
            idea("d", "entirely different subject matter here"),
        ]
        self.seed(self.items)

    def test_excludes_parked_by_default(self):
        r = ideatags.related("a", floor=0.0)
        self.assertNotIn("c", [x["id"] for x in r["related"]])

    def test_parked_reachable_on_request(self):
        r = ideatags.related("a", floor=0.0, include_parked=True)
        self.assertIn("c", [x["id"] for x in r["related"]])

    def test_floor_excludes_the_unrelated(self):
        r = ideatags.related("a", floor=0.5)
        self.assertNotIn("d", [x["id"] for x in r["related"]])

    def test_unknown_idea_raises(self):
        with self.assertRaises(KeyError):
            ideatags.related("nope")

    def test_duplicates_reports_each_pair_once(self):
        pairs = ideatags.duplicates(floor=0.0)
        keys = [tuple(sorted((p["a"], p["b"]))) for p in pairs]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertNotIn("c", [p["a"] for p in pairs] + [p["b"] for p in pairs])


class CandidateTests(StoreCase):
    """Scoring an idea that is not on the backlog yet — the one moment a
    repeat is cheapest to stop, and the one related()/duplicates() cannot
    reach, because both address an idea by id."""

    def emb(self, *vecs):
        return mock.patch.object(
            ideatags.localmodels, "ollama_embed",
            return_value=[np.array(v, dtype="float32") for v in vecs])

    def seed_space(self, n=20, d=64):
        """A corpus with a spread of its own, because closeness is scored
        RELATIVE to it. Two properties are load-bearing and neither survives
        three hand-written vectors: enough dimensions that unrelated ideas
        are near-orthogonal, and a high-similarity tail (four near-duplicate
        pairs, which any real backlog has — that is what duplicates() is
        for) so p99 sits where a genuine repeat lives rather than where the
        nearest stranger happens to fall."""
        rng = np.random.default_rng(7)
        V = rng.normal(size=(n, d))
        V /= np.linalg.norm(V, axis=1, keepdims=True)
        for i in range(1, 5):        # not V[0] — "a" must stay unique
            V[i] = V[i + 10] + 0.12 * rng.normal(size=d)
            V[i] /= np.linalg.norm(V[i])
        self.vecs = V
        self.typical = rng.normal(size=d)
        self.typical /= np.linalg.norm(self.typical)
        items = [idea("a", "remember which reader pages are finished")]
        items += [idea(f"i{k}", f"unrelated backlog item number {k} here")
                  for k in range(n - 1)]
        self.seed(items)
        for it, v in zip(items, V):
            self.put_vec(it, v)
        return items

    def test_a_reworded_repeat_clears_the_duplicate_floor(self):
        self.seed_space()
        with self.emb(self.vecs[0]):         # sits right on top of "a"
            out = ideatags.check_candidate(
                "remember which pages of the reader I finished", "Vira")
        self.assertEqual(out["basis"], "vector")
        self.assertEqual([m["id"] for m in out["matches"]], ["a"])

    def test_a_merely_typical_idea_is_not_a_duplicate(self):
        """The calibration case: an idea from the same domain sits inside
        the corpus's own band, and a band is not a repeat."""
        self.seed_space()
        with self.emb(self.typical):         # a fresh draw from the same cloud
            out = ideatags.check_candidate("something else entirely", "Vira")
        self.assertEqual(out["matches"], [])

    def test_nothing_is_written_to_the_sidecar(self):
        """A candidate the caller declines to stage must leave no phantom
        entry behind — which is why this does not go through embed_pending."""
        self.seed_space()
        before = ideatags._read()["entries"]
        with self.emb(self.vecs[0]):
            ideatags.check_candidate("remember my finished reader pages",
                                     "Vira")
        after = ideatags._read()["entries"]
        self.assertEqual(set(after), set(before))
        self.assertNotIn(ideatags._CANDIDATE_ID, after)

    def test_no_embedding_is_bought_when_there_is_nothing_to_compare(self):
        """A pool with no vectors scores on words either way, so the Ollama
        round-trip would buy nothing but latency."""
        self.seed([idea("a", "remember which reader pages are finished")])
        with mock.patch.object(ideatags.localmodels, "ollama_embed",
                               side_effect=AssertionError("no network")) as m:
            out = ideatags.check_candidate(
                "remember which reader pages are finished", "Vira")
        m.assert_not_called()
        self.assertEqual(out["basis"], "text")
        self.assertEqual([x["id"] for x in out["matches"]], ["a"])

    def test_ollama_down_degrades_to_text_and_says_so(self):
        self.seed_space()
        with mock.patch.object(ideatags.localmodels, "ollama_embed",
                               return_value=None):
            out = ideatags.check_candidate(
                "remember which reader pages are finished", "Vira")
        self.assertEqual(out["basis"], "text")
        self.assertEqual([x["id"] for x in out["matches"]], ["a"])

    def test_parked_ideas_are_not_duplicates(self):
        """Re-proposing something already shipped or dropped is the owner's
        call to make from the queue, not a repeat to refuse."""
        self.seed([idea("a", "remember which reader pages are finished",
                        status="done")])
        out = ideatags.check_candidate(
            "remember which reader pages are finished", "Vira")
        self.assertEqual(out["matches"], [])

    def test_empty_text_is_not_a_query(self):
        self.seed_space()
        self.assertEqual(ideatags.check_candidate("   ", "Vira")["matches"],
                         [])


class UntaggedTargetTests(StoreCase):
    def test_a_missing_tag_signal_is_dropped_not_counted_as_zero(self):
        """An untagged target scores tag_j == 0 against everything, which is
        a missing signal rather than evidence of difference. Counting it
        capped every score at 0.70 — under which a verbatim repeat could not
        clear DUP_FLOOR."""
        a = idea("a", "alpha beta gamma delta")
        b = idea("b", "alpha beta gamma delta")
        self.assertGreaterEqual(ideatags.score_pairs(a, [b])[0]["score"],
                                ideatags.DUP_FLOOR)

    def test_dropping_it_does_not_reorder_the_pool(self):
        """Safe by construction: a constant divisor rescales, never sorts."""
        a = idea("a", "reader pages finished remember")
        pool = [idea("b", "reader pages finished"),
                idea("c", "reader pages"),
                idea("d", "wholly unrelated invoice wording")]
        self.assertEqual([r["id"] for r in ideatags.score_pairs(a, pool)],
                         ["b", "c", "d"])

    def test_a_tagged_target_is_scored_exactly_as_before(self):
        a = idea("a", "alpha beta")
        b = idea("b", "alpha beta")
        self.put_tags("a", a["text"], {"module": ["reader"]})
        self.put_tags("b", b["text"], {"module": ["reader"]})
        row = ideatags.score_pairs(a, [b])[0]
        # The pre-change text formula, verbatim: no vector for this pair, so
        # (W_TAG * tag_j + W_TOK * tok_j) / (W_TAG + W_TOK), both jaccards 1.
        expect = ((ideatags.W_TAG * 1.0 + ideatags.W_TOK * 1.0)
                  / (ideatags.W_TAG + ideatags.W_TOK))
        self.assertAlmostEqual(row["score"], expect, places=4)


class EmbedTests(StoreCase):
    def test_ollama_down_is_reported_not_swallowed(self):
        self.seed([idea("a", "some idea")])
        with mock.patch.object(ideatags.localmodels, "ollama_embed",
                               return_value=None):
            out = ideatags.embed_pending()
        self.assertEqual(out["embedded"], 0)
        self.assertIn("ollama", out["reason"])

    def test_edited_text_restales_the_vector(self):
        it = idea("a", "first wording")
        self.seed([it])
        with mock.patch.object(ideatags.localmodels, "ollama_embed",
                               return_value=[np.array([1.0, 0.0],
                                                      dtype="float32")]):
            self.assertEqual(ideatags.embed_pending()["embedded"], 1)
            self.assertEqual(ideatags.embed_pending()["embedded"], 0)
            self.seed([idea("a", "second wording")])
            self.assertEqual(ideatags.embed_pending()["embedded"], 1)

    def test_embedding_one_idea_does_not_wipe_the_others(self):
        """The bug this exists to prevent, found by observation on a live
        instance: embed_pending([one_idea]) pruned 134 of 135 entries and
        silently destroyed every tag and vector in the sidecar, because
        the prune read its item list as "everything that exists". Opening
        a Similar panel on a freshly edited idea was enough to trigger it."""
        a, b = idea("a", "one"), idea("b", "two")
        self.seed([a, b])
        self.put_vec(b, [0.0, 1.0])
        self.put_tags("b", "two", {"module": ["reader"]})
        with mock.patch.object(ideatags.localmodels, "ollama_embed",
                               return_value=[np.array([1.0, 0.0],
                                                      dtype="float32")]):
            ideatags.embed_pending([a])
        entries = ideatags._read()["entries"]
        self.assertIn("b", entries)
        self.assertEqual(entries["b"]["tags"]["module"], ["reader"])
        self.assertIn("a", entries)

    def test_related_on_a_new_idea_keeps_the_rest_of_the_index(self):
        """The path that actually triggered it: related() embeds its
        target on demand, so the duplicate nudge must not cost the index."""
        a, b = idea("a", "brand new idea"), idea("b", "an older idea")
        self.seed([a, b])
        self.put_vec(b, [0.0, 1.0])
        with mock.patch.object(ideatags.localmodels, "ollama_embed",
                               return_value=[np.array([1.0, 0.0],
                                                      dtype="float32")]):
            r = ideatags.related("a", floor=0.0)
        self.assertIn("b", ideatags._read()["entries"])
        self.assertEqual(r["basis"], "vector")

    def test_refresh_prunes_deleted_ideas(self):
        a, b = idea("a", "one"), idea("b", "two")
        self.seed([a, b])
        self.put_vec(a, [1.0, 0.0])
        self.put_vec(b, [0.0, 1.0])
        self.seed([a])
        with mock.patch.object(ideatags.localmodels, "ollama_embed",
                               return_value=None), \
             mock.patch.object(ideatags.suggest, "complete",
                               return_value=json.dumps({"tags": []})):
            ideatags.refresh()
        self.assertNotIn("b", ideatags._read()["entries"])
        self.assertIn("a", ideatags._read()["entries"])


# ---------- the tagging pass (one model call per batch) ----------

class TagPassTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.seed([idea("i1", "make the reader remember read state"),
                   idea("i2", "the phone page keeps getting wider")])

    def reply(self, payload):
        return mock.patch.object(ideatags.suggest, "complete",
                                 return_value=json.dumps(payload))

    def test_tags_are_stored_and_validated(self):
        with self.reply({"tags": [
            {"id": "i1", "module": ["Reader"], "theme": ["read-tracking"]},
            {"id": "i2", "module": ["mobile shell"], "theme": ["Mobile Layout"],
             "concept": ["viewport", "x" * 40, "!!"]},
        ]}):
            out = ideatags.tag_pending()
        self.assertEqual(out["tagged"], 2)
        s = ideatags._read()
        self.assertEqual(s["entries"]["i1"]["tags"]["module"], ["reader"])
        self.assertEqual(s["entries"]["i2"]["tags"]["theme"], ["mobile-layout"])
        self.assertEqual(s["entries"]["i2"]["tags"]["concept"], ["viewport"])

    def test_verdicts_for_unknown_ideas_are_dropped(self):
        with self.reply({"tags": [{"id": "not-a-real-id",
                                   "module": ["ghost"]}]}):
            out = ideatags.tag_pending()
        self.assertEqual(out["tagged"], 0)
        self.assertNotIn("not-a-real-id", ideatags._read()["entries"])

    def test_nothing_pending_makes_no_model_call(self):
        with self.reply({"tags": [{"id": "i1"}, {"id": "i2"}]}):
            ideatags.tag_pending()
        with mock.patch.object(ideatags.suggest, "complete") as m:
            out = ideatags.tag_pending()
        m.assert_not_called()
        self.assertEqual(out["pending"], 0)

    def test_edited_text_makes_the_idea_pending_again(self):
        with self.reply({"tags": [{"id": "i1", "module": ["reader"]},
                                  {"id": "i2", "module": ["mobile"]}]}):
            ideatags.tag_pending()
        self.seed([idea("i1", "a completely rewritten idea"),
                   idea("i2", "the phone page keeps getting wider")])
        pending = ideatags._pending(ideas.list_items(), ideatags._read())
        self.assertEqual([p["id"] for p in pending], ["i1"])

    def test_dead_backend_leaves_ideas_pending(self):
        """A failed batch must not stamp its ideas tagged-with-nothing —
        the next pass has to retry them."""
        with mock.patch.object(ideatags.suggest, "complete",
                               side_effect=RuntimeError("backend down")):
            out = ideatags.tag_pending()
        self.assertEqual(out["tagged"], 0)
        self.assertEqual(out["pending"], 2)
        self.assertTrue(out["errors"])
        self.assertEqual(ideatags._read()["entries"], {})

    def test_prompt_carries_the_existing_vocabulary(self):
        """Convergence depends on the tagger seeing what is already in
        use; without it the same subject gets four spellings."""
        self.put_tags("i1", "make the reader remember read state",
                      {"module": ["reader"]})
        seen = {}

        def capture(prompt):
            seen["p"] = prompt
            return json.dumps({"tags": []})

        with mock.patch.object(ideatags.suggest, "complete", capture):
            ideatags.tag_pending()
        self.assertIn("reader x1", seen["p"])
        self.assertIn("module:", seen["p"])

    def test_refresh_reports_both_halves(self):
        with mock.patch.object(ideatags.localmodels, "ollama_embed",
                               return_value=None), \
             self.reply({"tags": [{"id": "i1", "module": ["reader"]}]}):
            out = ideatags.refresh()
        self.assertEqual(out["embedded"], 0)
        self.assertEqual(out["tagged"], 1)


# ---------- fold analysis ----------

class FoldTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.seed([idea("t", "rebuild the reader queue"),
                   idea("c1", "reader should track section progress"),
                   idea("c2", "unrelated mail sync work")])

    def reply(self, payload):
        return mock.patch.object(ideatags.suggest, "complete",
                                 return_value=json.dumps(payload))

    def test_verdicts_are_returned(self):
        with self.reply({"verdicts": [
            {"id": "c1", "verdict": "fold", "why": "same surface"},
            {"id": "c2", "verdict": "separate", "why": "different module"}]}):
            out = ideatags.fold_analysis("t", ["c1", "c2"])
        by = {v["id"]: v for v in out["verdicts"]}
        self.assertEqual(by["c1"]["verdict"], "fold")
        self.assertEqual(by["c2"]["verdict"], "separate")
        self.assertEqual(out["analyzed"], 2)

    def test_silence_defaults_to_separate(self):
        """Nothing widens the dispatch by default — a candidate the model
        skipped is left for its own session, never folded in."""
        with self.reply({"verdicts": [{"id": "c1", "verdict": "fold",
                                       "why": "same surface"}]}):
            out = ideatags.fold_analysis("t", ["c1", "c2"])
        by = {v["id"]: v for v in out["verdicts"]}
        self.assertEqual(by["c2"]["verdict"], "separate")
        self.assertIn("not assessed", by["c2"]["why"])

    def test_verdicts_about_uninvited_ideas_are_dropped(self):
        with self.reply({"verdicts": [
            {"id": "c1", "verdict": "fold", "why": "ok"},
            {"id": "somebody-else", "verdict": "fold", "why": "invented"}]}):
            out = ideatags.fold_analysis("t", ["c1"])
        self.assertEqual([v["id"] for v in out["verdicts"]], ["c1"])

    def test_unknown_verdict_word_reads_as_separate(self):
        with self.reply({"verdicts": [{"id": "c1", "verdict": "maybe",
                                       "why": "unsure"}]}):
            out = ideatags.fold_analysis("t", ["c1"])
        self.assertEqual(out["verdicts"][0]["verdict"], "separate")

    def test_the_target_is_never_its_own_candidate(self):
        with mock.patch.object(ideatags.suggest, "complete") as m:
            out = ideatags.fold_analysis("t", ["t"])
        m.assert_not_called()
        self.assertEqual(out["analyzed"], 0)

    def test_unknown_target_raises(self):
        with self.assertRaises(KeyError):
            ideatags.fold_analysis("nope", ["c1"])


# ---------- routes ----------

class RouteTests(StoreCase):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server import main
        self.seed([idea("a", "alpha beta reader queue"),
                   idea("b", "alpha beta reader backlog")])
        self.client = TestClient(main.app)

    def test_ideas_carry_tags_vocab_and_status(self):
        self.put_tags("a", "alpha beta reader queue", {"module": ["reader"]})
        r = self.client.get("/api/ideas").json()
        self.assertEqual(r["items"][0]["tags"]["module"], ["reader"])
        self.assertEqual(r["vocab"]["module"], [["reader", 1]])
        self.assertEqual(r["tag_status"]["total"], 2)

    def test_related_route(self):
        r = self.client.get("/api/ideas/a/related?floor=0").json()
        self.assertEqual(r["id"], "a")
        self.assertIn("b", [x["id"] for x in r["related"]])

    def test_related_unknown_is_404(self):
        self.assertEqual(self.client.get("/api/ideas/zz/related").status_code,
                         404)

    def test_tag_drop_round_trips(self):
        self.put_tags("a", "alpha beta reader queue", {"module": ["reader"]})
        r = self.client.put("/api/ideas/a", json={"tags_drop": ["reader"]})
        self.assertEqual(r.json()["tags"]["module"], [])

    def test_fold_route(self):
        with mock.patch.object(ideatags.suggest, "complete",
                               return_value=json.dumps({"verdicts": [
                                   {"id": "b", "verdict": "fold",
                                    "why": "same thing"}]})):
            r = self.client.post("/api/ideas/a/fold-analysis",
                                 json={"candidates": ["b"]}).json()
        self.assertEqual(r["verdicts"][0]["verdict"], "fold")

    def test_fold_route_reports_a_dead_backend(self):
        with mock.patch.object(ideatags.suggest, "complete",
                               side_effect=RuntimeError("no backend")):
            r = self.client.post("/api/ideas/a/fold-analysis",
                                 json={"candidates": ["b"]})
        self.assertEqual(r.status_code, 503)

    def test_reindex_is_bounded(self):
        """The cap is on the pass, wherever the pass runs. It moved
        out-of-process (ideatags.run_pass) so a 20-batch reindex cannot
        freeze the server it was clicked in, but a click must still never
        turn into unbounded model spend."""
        with mock.patch.object(ideatags, "run_pass",
                               return_value={"tagged": 0}) as m:
            self.client.post("/api/ideas/reindex", json={"batches": 999})
        self.assertEqual(m.call_args.kwargs["batches"], 20)

    def test_reindex_does_not_run_the_pass_in_this_process(self):
        """The regression guard for the 2026-07-27 stalls: the heaviest
        thing the Queue can ask for must not execute in the server's own
        interpreter, where it competes with the event loop for the GIL."""
        with mock.patch.object(ideatags, "run_pass",
                               return_value={"tagged": 0}), \
             mock.patch.object(ideatags, "refresh") as inproc:
            self.client.post("/api/ideas/reindex", json={"batches": 3})
        inproc.assert_not_called()

    def test_defer_route_files_the_idea_and_says_so(self):
        from server import ideas as ideasstore
        r = self.client.post("/api/ideas/a/defer").json()
        self.assertEqual(r["status"], "deferred")
        self.assertIn("deferred by the owner", r["note"])
        self.assertEqual(ideasstore.list_items()[0]["status"], "deferred")

    def test_defer_unknown_is_404(self):
        self.assertEqual(self.client.post("/api/ideas/zz/defer").status_code,
                         404)

    def test_a_deferred_idea_is_still_scored_for_similarity(self):
        """Not the done/dropped treatment: deferred work is meant to be
        revisited, so it must keep showing up as 'like this one'."""
        self.client.post("/api/ideas/b/defer")
        r = self.client.get("/api/ideas/a/related?floor=0").json()
        self.assertIn("b", [x["id"] for x in r["related"]])


if __name__ == "__main__":
    unittest.main()
