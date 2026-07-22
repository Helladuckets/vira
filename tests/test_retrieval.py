"""Shared retrieval-core tests: the primitives in server/retrieval.py
directly (FTS query building, RRF math, cosine top-k, the float16
codec, matrix-cache staleness, best-match), plus an end-to-end
search.search() pass over a tiny synthetic media-index sqlite with the
embedding functions stubbed to fixed vectors — the full hybrid path
runs with no models (no SigLIP, no Ollama).

Run: .venv/bin/python -m unittest tests.test_retrieval
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from server import data as crm, mediaindex, retrieval, search


class FtsQueryTests(unittest.TestCase):
    def test_multi_term_all_then_any(self):
        self.assertEqual(retrieval.fts_queries("beach dog"),
                         ['"beach" "dog"', '"beach" OR "dog"'])

    def test_single_term(self):
        self.assertEqual(retrieval.fts_queries("snowmobile"),
                         ['"snowmobile"'])

    def test_punctuation_tokenized(self):
        self.assertEqual(retrieval.fts_queries("Steve's boat, 2024"),
                         ['"Steve\'s" "boat" "2024"',
                          '"Steve\'s" OR "boat" OR "2024"'])

    def test_empty(self):
        self.assertEqual(retrieval.fts_queries(""), [])
        self.assertEqual(retrieval.fts_queries("!?"), [])


class RrfTests(unittest.TestCase):
    def test_hand_computed_scores(self):
        ranks = retrieval.rrf([[1, 2], [2, 3]])
        self.assertAlmostEqual(ranks[1], 1 / 60)
        self.assertAlmostEqual(ranks[2], 1 / 61 + 1 / 60)
        self.assertAlmostEqual(ranks[3], 1 / 61)
        top = sorted(ranks, key=ranks.get, reverse=True)
        self.assertEqual(top, [2, 1, 3])

    def test_custom_k(self):
        ranks = retrieval.rrf([[7]], k=1)
        self.assertAlmostEqual(ranks[7], 1.0)

    def test_tie_keeps_first_list_first(self):
        # a and b tie exactly (rank 0 in one list each); stable sort must
        # keep a (first list) ahead of b
        ranks = retrieval.rrf([["a"], ["b"]])
        self.assertEqual(ranks["a"], ranks["b"])
        top = sorted(ranks, key=ranks.get, reverse=True)
        self.assertEqual(top, ["a", "b"])


class CodecTests(unittest.TestCase):
    def test_roundtrip_float16_precision(self):
        v = np.array([0.123456789, -0.5, 1.0, 0.0], dtype=np.float32)
        blob = retrieval.pack_vec(v)
        self.assertEqual(len(blob), 4 * 2)          # float16 = 2 bytes
        back = retrieval.unpack_vec(blob)
        self.assertEqual(back.dtype, np.float32)
        # float16 keeps ~3 decimal digits; exact for -0.5/1.0/0.0
        np.testing.assert_allclose(back, v, atol=1e-3)
        self.assertEqual(back[1], -0.5)
        self.assertEqual(back[2], 1.0)

    def test_stack(self):
        blobs = [retrieval.pack_vec(np.array([1.0, 0.0], dtype=np.float32)),
                 retrieval.pack_vec(np.array([0.0, 1.0], dtype=np.float32))]
        M = retrieval.stack_vecs(blobs)
        self.assertEqual(M.shape, (2, 2))
        self.assertEqual(M.dtype, np.float32)
        np.testing.assert_array_equal(M, np.eye(2, dtype=np.float32))


class RankVecTests(unittest.TestCase):
    def _space(self):
        ids = np.array([10, 11, 12, 13], dtype=np.int64)
        M = np.array([[1.0, 0.0],      # sim 1.0 to qvec
                      [0.8, 0.6],      # sim 0.8
                      [0.0, 1.0],      # sim 0.0
                      [0.8, -0.6]],    # sim 0.8 (ties with 11)
                     dtype=np.float32)
        return ids, M

    def test_descending_with_floor(self):
        qvec = np.array([1.0, 0.0], dtype=np.float32)
        out = retrieval.rank_vec(self._space(), qvec, None, floor=0.5)
        # 12 (sim 0.0) is below the floor; 11 ties 13 at 0.8 and argsort
        # is stable descending so the earlier row wins
        self.assertEqual(out, [10, 11, 13])

    def test_floor_cuts_everything(self):
        qvec = np.array([1.0, 0.0], dtype=np.float32)
        out = retrieval.rank_vec(self._space(), qvec, None, floor=1.5)
        self.assertEqual(out, [])

    def test_candidate_filter_and_limit(self):
        qvec = np.array([1.0, 0.0], dtype=np.float32)
        out = retrieval.rank_vec(self._space(), qvec, {11, 12, 13},
                                 floor=-1.0, limit=2)
        self.assertEqual(out, [11, 13])

    def test_duplicate_ids_deduped(self):
        # chunked text vectors repeat the same id; first (best) wins
        ids = np.array([5, 5, 6], dtype=np.int64)
        M = np.array([[1.0, 0.0], [0.9, 0.1], [0.5, 0.5]],
                     dtype=np.float32)
        qvec = np.array([1.0, 0.0], dtype=np.float32)
        out = retrieval.rank_vec((ids, M), qvec, None, floor=0.0)
        self.assertEqual(out, [5, 6])

    def test_none_space_or_qvec(self):
        qvec = np.array([1.0, 0.0], dtype=np.float32)
        self.assertEqual(retrieval.rank_vec(None, qvec, None, 0.0), [])
        self.assertEqual(
            retrieval.rank_vec(self._space(), None, None, 0.0), [])


class BestMatchTests(unittest.TestCase):
    def test_argmax_and_score(self):
        G = retrieval.unit_rows(np.array(
            [[1.0, 0.0], [0.0, 1.0]], dtype=np.float32))
        v = retrieval.unit(np.array([0.1, 2.0], dtype=np.float32))
        i, score = retrieval.best_match(G, v)
        self.assertEqual(i, 1)
        self.assertIsInstance(score, float)
        self.assertGreater(score, 0.99)

    def test_match_faces_routes_through_core(self):
        # synthetic gallery + faces in a real schema db: the known face
        # matches above FACE_MATCH_T, the stranger stays unnamed
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "media-index.sqlite"
            con = sqlite3.connect(db)
            con.executescript(mediaindex.SCHEMA)
            ref = np.array([1.0, 0.0, 0.0], dtype=np.float32)
            near = np.array([0.9, 0.1, 0.0], dtype=np.float32)
            far = np.array([0.0, 0.0, 1.0], dtype=np.float32)
            con.execute(
                "INSERT INTO face_gallery(person_id, src, v) VALUES(?,?,?)",
                ("p_alice", "photo-cache", retrieval.pack_vec(ref)))
            con.execute(
                "INSERT INTO faces(seq, v) VALUES(1, ?)",
                (retrieval.pack_vec(near),))
            con.execute(
                "INSERT INTO faces(seq, v) VALUES(2, ?)",
                (retrieval.pack_vec(far),))
            con.commit()
            n = mediaindex._match_faces(con)
            self.assertEqual(n, 1)
            rows = con.execute(
                "SELECT seq, person_id, match_score FROM faces "
                "ORDER BY seq").fetchall()
            self.assertEqual(rows[0][1], "p_alice")
            self.assertGreaterEqual(rows[0][2], mediaindex.FACE_MATCH_T)
            self.assertIsNone(rows[1][1])
            con.close()


def _vec_db(path, scene=(), text=()):
    """A bare db with just the vector tables (matrix-cache tests)."""
    con = sqlite3.connect(path)
    con.executescript(mediaindex.SCHEMA)
    for seq, v in scene:
        con.execute("INSERT OR REPLACE INTO vec_scene(seq,v) VALUES(?,?)",
                    (seq, retrieval.pack_vec(np.array(v, dtype=np.float32))))
    for seq, chunk, v in text:
        con.execute(
            "INSERT OR REPLACE INTO vec_text(seq,chunk,v) VALUES(?,?,?)",
            (seq, chunk, retrieval.pack_vec(np.array(v, dtype=np.float32))))
    con.commit()
    con.close()


SPACES = {
    "scene": ("SELECT COUNT(*) FROM vec_scene",
              "SELECT seq, v FROM vec_scene"),
    "text": ("SELECT COUNT(*) FROM vec_text",
             "SELECT seq, chunk, v FROM vec_text"),
}


class MatrixCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "vec.sqlite"
        _vec_db(self.db, scene=[(1, [1.0, 0.0])], text=[(1, 0, [0.5, 0.5])])

    def _con(self):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        return con

    def test_load_and_get(self):
        cache = retrieval.MatrixCache(SPACES)
        cache.load(self._con)
        ids, M = cache.get("scene")
        self.assertEqual(list(ids), [1])
        self.assertEqual(M.dtype, np.float32)
        self.assertIsNotNone(cache.get("text"))

    def test_fresh_within_max_age_skips_db(self):
        cache = retrieval.MatrixCache(SPACES, max_age=60)
        cache.load(self._con)
        before = cache.get("scene")
        _vec_db(self.db, scene=[(2, [0.0, 1.0])])       # grow the table
        cache.load(self._con)                           # still fresh
        self.assertIs(cache.get("scene"), before)       # not rebuilt

    def test_stale_unchanged_count_touches_not_rebuilds(self):
        cache = retrieval.MatrixCache(SPACES, max_age=0)
        cache.load(self._con)
        before = cache.get("scene")
        cache.load(self._con)          # stale, but counts unchanged
        self.assertIs(cache.get("scene"), before)

    def test_stale_grown_rebuilds(self):
        cache = retrieval.MatrixCache(SPACES, max_age=0)
        cache.load(self._con)
        _vec_db(self.db, scene=[(2, [0.0, 1.0])])
        cache.load(self._con)
        ids, _M = cache.get("scene")
        self.assertEqual(sorted(ids), [1, 2])

    def test_invalidate_forces_recheck(self):
        cache = retrieval.MatrixCache(SPACES, max_age=3600)
        cache.load(self._con)
        _vec_db(self.db, scene=[(2, [0.0, 1.0])])
        cache.invalidate()
        cache.load(self._con)
        ids, _M = cache.get("scene")
        self.assertEqual(sorted(ids), [1, 2])

    def test_empty_table_stays_none(self):
        empty = Path(self.tmp.name) / "empty.sqlite"
        _vec_db(empty)
        cache = retrieval.MatrixCache(SPACES)
        con = lambda: sqlite3.connect(empty)  # noqa: E731
        cache.load(con)
        self.assertIsNone(cache.get("scene"))
        self.assertIsNone(cache.get("text"))


# ---------- end-to-end: search.search() over a synthetic index ----------

APPLE_EPOCH_NS = 700_000_000 * 1_000_000_000   # a fixed, valid date_ns


def _media_db(path):
    """Three items: a beach photo (fts+scene+text), a receipt doc
    (fts only), a dog photo (scene only)."""
    con = sqlite3.connect(path)
    con.executescript(mediaindex.SCHEMA)
    rows = [
        (1, "photo", 101, "beach.jpg", "p_alice", 0),
        (2, "doc", 102, "receipt.pdf", "p_alice", 1),
        (3, "photo", 103, "dog.heic", "p_bob", 0),
    ]
    for seq, kind, aid, name, pid, from_me in rows:
        con.execute(
            """INSERT INTO items(seq, kind, id, chat_pid, sender_pid,
                                 from_me, date_ns, name, purged)
               VALUES(?,?,?,?,?,?,?,?,0)""",
            (seq, kind, aid, pid, "me" if from_me else pid,
             from_me, APPLE_EPOCH_NS + seq * 1_000_000_000, name))
        con.execute("INSERT INTO content(seq, context) VALUES(?, '')",
                    (seq,))
    con.execute(
        "INSERT INTO fts(rowid, name, context) VALUES(1, 'beach.jpg', "
        "'sunset at the beach')")
    con.execute(
        "INSERT INTO fts(rowid, name, doc_text) VALUES(2, 'receipt.pdf', "
        "'beach house receipt')")
    con.execute(
        "INSERT INTO fts(rowid, name, context) VALUES(3, 'dog.heic', "
        "'good boy')")
    # scene space: seq 1 points at [1,0], seq 3 at [0.9, 0.1] — both
    # match a [1,0] query vector, 1 stronger
    for seq, v in ((1, [1.0, 0.0]), (3, [0.9, 0.1])):
        con.execute("INSERT INTO vec_scene(seq,v) VALUES(?,?)",
                    (seq, retrieval.pack_vec(np.array(v, dtype=np.float32))))
    con.execute("INSERT INTO vec_text(seq,chunk,v) VALUES(1, 0, ?)",
                (retrieval.pack_vec(np.array([1.0, 0.0], dtype=np.float32)),))
    con.commit()
    con.close()


def _crm_cache():
    return {"by_id": {
        "p_alice": {"id": "p_alice", "name": "Alice Larkspur"},
        "p_bob": {"id": "p_bob", "name": "Bob Finch"},
    }}


class SearchEndToEndTests(unittest.TestCase):
    """The full hybrid pipeline — candidates, FTS, both vector spaces,
    RRF fusion, hydration — with embedders stubbed to fixed vectors."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / "media-index.sqlite"
        _media_db(db)
        qvec = np.array([1.0, 0.0], dtype=np.float32)
        patches = [
            mock.patch.object(mediaindex, "DB", db),
            mock.patch.object(search, "_matrices",
                              retrieval.MatrixCache(SPACES)),
            mock.patch.object(search, "_scene_qvec", lambda q: qvec),
            mock.patch.object(search, "_text_qvec", lambda q: qvec),
            mock.patch.object(crm, "_load", _crm_cache),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_no_query_newest_first(self):
        out = search.search(limit=10)
        self.assertEqual([e["seq"] for e in out], [3, 2, 1])
        self.assertEqual(out[0]["sender"], "Bob Finch")
        self.assertIsNone(out[0]["score"])

    def test_hybrid_fusion_orders_all_layers(self):
        out = search.search(q="beach")
        seqs = [e["seq"] for e in out]
        # seq 1 leads: fts rank 0 + scene rank 0 + text rank 0;
        # 2 (fts-only) and 3 (scene-only) both follow with one rank-1 —
        # seq 2 first because the fts list fused first
        self.assertEqual(seqs, [1, 2, 3])
        self.assertGreater(out[0]["score"], out[1]["score"])
        self.assertGreaterEqual(out[1]["score"], out[2]["score"])

    def test_kind_filter_constrains_candidates(self):
        out = search.search(q="beach", kind="doc")
        self.assertEqual([e["seq"] for e in out], [2])
        self.assertEqual(out[0]["ext"], "PDF")

    def test_sender_filter(self):
        out = search.search(q="beach", sender_pid="p_bob")
        self.assertEqual([e["seq"] for e in out], [3])

    def test_direction_filter_no_query(self):
        out = search.search(direction="sent")
        self.assertEqual([e["seq"] for e in out], [2])
        self.assertEqual(out[0]["sender"], "you")

    def test_hydrate_carries_context(self):
        out = search.search(q="sunset")
        self.assertEqual(out[0]["seq"], 1)
        self.assertEqual(out[0]["person"], "Alice Larkspur")


if __name__ == "__main__":
    unittest.main()
