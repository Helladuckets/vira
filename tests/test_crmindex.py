"""The CRM as a corpus: blob composition, rebuild-on-change, and the
hybrid query path with the embedder stubbed.

Real fixture files on disk, because the freshness check is a source
fingerprint (mtimes and counts) rather than an in-memory flag.

Run: .venv/bin/python -m unittest tests.test_crmindex
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from server import crmindex
from server import data as crm
from server import retrieval, settings

PEOPLE = [
    {"id": "p_broker", "name": "Bob Grant",
     "handles": {"emails": ["bob@nfp.example"], "phones10": ["9175551212"]},
     "activity": {"imsg_n": 40, "imsg_last": "2026-05-01"}},
    {"id": "p_sponsor", "name": "Sam Muhs", "handles": {},
     "activity": {"imsg_n": 900, "imsg_last": "2026-07-01"}},
    {"id": "p_quiet", "name": "Dana Okonkwo", "handles": {},
     "activity": {"imsg_n": 2, "imsg_last": "2024-01-01"}},
]
MASTER = [
    {"id": "p_broker", "full_name": "Bob Grant", "company": "NFP",
     "title": "Account Executive", "relationship": "insurance broker",
     "evidence": "handled the property insurance renewals"},
    {"id": "p_sponsor", "full_name": "Sam Muhs", "company": "Sotelo Capital",
     "title": "Principal", "relationship": "friend and sponsor",
     "evidence": "pitches multifamily deals"},
]
PROFILE = {
    "id": "p_sponsor", "name": "Sam Muhs", "relationship_class": "friend",
    "relationship_summary": "Closest real-estate peer; trades underwriting "
                            "notes and sponsors multifamily deals.",
    "topics": [{"topic": "Charleston Grove raise", "quote": "rounding out"}],
    "open_loops": [{"what": "review the Charleston Grove deck"}],
    "personal_facts": [{"fact": "Has three sons"}],
    "hooks": [{"angle": "Ask how the raise closed", "detail": "sent a deck"}],
}


def _fixture_root(tmp):
    root = Path(tmp) / "crm"
    (root / "profiles").mkdir(parents=True)
    (root / "people.json").write_text(json.dumps({"people": PEOPLE}))
    (root / "master.json").write_text(json.dumps(MASTER))
    (root / "profiles" / "p_sponsor.json").write_text(json.dumps(PROFILE))
    return root


class CrmIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = _fixture_root(self.tmp.name)
        self.db = Path(self.tmp.name) / "crm-index.sqlite"
        patches = [
            mock.patch.object(settings, "crm_root", lambda: self.root),
            mock.patch.object(crmindex, "DB", self.db),
            mock.patch.object(crmindex, "_matrices", retrieval.MatrixCache(
                {"text": ("SELECT COUNT(*) FROM vecs",
                          "SELECT seq, v FROM vecs")})),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        crm._cache.clear()
        crmindex.invalidate()
        crmindex._qvec.cache_clear()
        self.addCleanup(crm._cache.clear)

    # ---------- the blob ----------

    def test_profile_prose_is_indexed_not_just_the_name(self):
        crmindex.refresh(force=True)
        hits = crmindex.search("multifamily underwriting", limit=3)
        self.assertEqual(hits[0]["id"], "p_sponsor")

    def test_master_fields_are_searchable(self):
        crmindex.refresh(force=True)
        self.assertEqual(crmindex.search("insurance", limit=3)[0]["id"],
                         "p_broker")

    def test_handles_are_searchable(self):
        crmindex.refresh(force=True)
        self.assertEqual(crmindex.search("bob@nfp.example")[0]["id"],
                         "p_broker")

    def test_snippet_shows_a_matching_line(self):
        crmindex.refresh(force=True)
        hit = crmindex.search("Charleston Grove", limit=1)[0]
        self.assertIn("Charleston", hit["snippet"])

    # ---------- freshness ----------

    def test_second_refresh_is_a_no_op(self):
        self.assertTrue(crmindex.refresh(force=True)["rebuilt"])
        crmindex.invalidate()
        self.assertFalse(crmindex.refresh()["rebuilt"])

    def test_a_changed_profile_rebuilds(self):
        crmindex.refresh(force=True)
        crmindex.invalidate()
        crm._cache.clear()
        time.sleep(0.01)
        (self.root / "profiles" / "p_new.json").write_text(
            json.dumps({"id": "p_quiet", "name": "Dana Okonkwo",
                        "relationship_summary": "runs a ceramics studio"}))
        self.assertTrue(crmindex.refresh()["rebuilt"])

    def test_unchanged_people_keep_their_vectors(self):
        crmindex.refresh(force=True)
        con = crmindex._con()
        seq = con.execute("SELECT seq FROM people WHERE pid='p_broker'"
                          ).fetchone()[0]
        con.execute("INSERT INTO vecs(seq, v) VALUES(?,?)",
                    (seq, retrieval.pack_vec(np.array([1.0, 0.0],
                                                      dtype=np.float32))))
        con.execute("UPDATE people SET pending=0 WHERE seq=?", (seq,))
        con.commit()
        con.close()
        crmindex.refresh(force=True)          # nothing changed on disk
        con = crmindex._con()
        try:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM vecs").fetchone()[0], 1)
        finally:
            con.close()

    # ---------- ranking ----------

    def test_vectors_join_the_ranking(self):
        crmindex.refresh(force=True)
        vec = np.array([1.0, 0.0], dtype=np.float32)
        con = crmindex._con()
        seq = con.execute("SELECT seq FROM people WHERE pid='p_quiet'"
                          ).fetchone()[0]
        con.execute("INSERT INTO vecs(seq, v) VALUES(?,?)",
                    (seq, retrieval.pack_vec(vec)))
        con.commit()
        con.close()
        crmindex.invalidate()
        with mock.patch.object(crmindex, "_qvec", lambda q: vec):
            ids = [h["id"] for h in crmindex.search("ceramics", limit=5)]
        self.assertIn("p_quiet", ids)         # nothing lexical matched

    def test_exact_skips_the_vector_layer(self):
        crmindex.refresh(force=True)
        with mock.patch.object(crmindex, "_qvec",
                               side_effect=AssertionError("embedded!")):
            hits = crmindex.search("9175551212", exact=True, limit=3)
        self.assertEqual(hits[0]["id"], "p_broker")

    def test_empty_query_browses_by_recency(self):
        crmindex.refresh(force=True)
        rows = crmindex.search("", limit=3)
        self.assertEqual(rows[0]["id"], "p_sponsor")     # newest contact
        self.assertIn("snippet", rows[0])                # one row shape

    def test_status_counts(self):
        crmindex.refresh(force=True)
        st = crmindex.status()
        self.assertEqual(st["people"], 3)
        self.assertEqual(st["pending"], 3)
        self.assertTrue(st["available"])


if __name__ == "__main__":
    unittest.main()
