"""Shared hybrid-retrieval primitives: FTS query building, bm25 and
cosine ranking, reciprocal-rank fusion, the float16 blob codec, and a
staleness-cached matrix loader.

One implementation for every retrieval stack on the vira side — the
media index (search.py + mediaindex.py) consumes these directly. The
qocha vault engine (qocha/vault.py) carries a line-for-line parallel of
each primitive: RRF_K, VaultIndex._fts_queries, _rank_fts, _rank_vec,
_vec_matrix, and the float16 pack/unpack in embed_pending. This module
is shaped so a later lift moves qocha onto the same core mechanically:

  fts_queries   <- qocha VaultIndex._fts_queries (identical)
  rank_fts      <- qocha VaultIndex._rank_fts (table + limit are
                   parameters here; qocha hardcodes chunks_fts/200)
  rank_vec      <- qocha VaultIndex._rank_vec (qocha has no candidate
                   filter or seen-dedupe; both are optional here)
  rrf           <- the fusion loop in qocha VaultIndex.search
  pack_vec /
  unpack_vec /
  stack_vecs    <- the float16 blob codec (qocha embed_pending /
                   _vec_matrix)
  MatrixCache   <- qocha VaultIndex._vec_state/_vec_matrix (generation
                   -based there; count+time staleness here)

Everything is deterministic and model-free: callers supply query
vectors and sqlite handles.
"""
import re
import threading
import time

try:
    import numpy as np
except ImportError:  # minimal install: FTS-only retrieval still works
    np = None

RRF_K = 60


def _require_np():
    if np is None:
        raise ImportError("numpy is required for vector retrieval")


# ---------- FTS ----------

def fts_queries(q, phrases=()):
    """User text -> ranked fts5 queries: quoted phrases first (a phrase
    the user typed in quotes is the one thing they are sure of), then
    all-terms (precise hits dominate), then any-term (recall for
    conversational phrasing)."""
    out = []
    for p in phrases:
        terms = re.findall(r"[A-Za-z0-9']+", p)
        if terms:                       # one quoted phrase = one fts5 phrase
            out.append('"' + " ".join(terms) + '"')
    terms = re.findall(r"[A-Za-z0-9']+", q)
    if not terms:
        return out
    all_q = " ".join(f'"{t}"' for t in terms)
    any_q = " OR ".join(f'"{t}"' for t in terms)
    out += [all_q, any_q] if len(terms) > 1 else [any_q]
    return out


def rank_fts(con, q, cand, limit, table="fts", phrases=()):
    """bm25-ordered rowids matching q, best-first: the all-terms query
    ranks ahead of any-term, duplicates and rows outside the cand set
    (None = unfiltered) are dropped. limit caps each fts query AND the
    fused list."""
    out, seen = [], set()
    for fq in fts_queries(q, phrases):
        rows = con.execute(
            f"SELECT rowid, bm25({table}) AS r FROM {table} "
            f"WHERE {table} MATCH ? ORDER BY r LIMIT ?",
            (fq, limit)).fetchall()
        for r in rows:
            rid = r[0]
            if rid in seen or (cand is not None and rid not in cand):
                continue
            seen.add(rid)
            out.append(rid)
        if len(out) >= limit:
            break
    return out[:limit]


# ---------- vectors ----------

def pack_vec(v):
    """Vector -> the on-disk blob format (float16, half the bytes; the
    precision loss is far below embedding noise)."""
    _require_np()
    return v.astype("float16").tobytes()


def unpack_vec(blob):
    """On-disk blob -> float32 vector (float32 for fast matmul)."""
    _require_np()
    return np.frombuffer(blob, dtype="float16").astype("float32")


def stack_vecs(blobs):
    """Blobs -> one float32 matrix, row per vector."""
    _require_np()
    return np.stack([unpack_vec(b) for b in blobs])


def unit(v):
    """L2-normalized copy of a vector (epsilon-guarded)."""
    _require_np()
    return v / (np.linalg.norm(v) + 1e-9)


def unit_rows(M):
    """L2-normalized copy of a matrix, row-wise (epsilon-guarded)."""
    _require_np()
    return M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)


def rank_vec(space, qvec, cand, floor, limit=400):
    """Cosine top-k over an in-memory matrix: space is (ids, matrix) or
    None. Descending similarity, stopping at floor; duplicates (chunked
    ids) and rows outside cand (None = unfiltered) are dropped."""
    if np is None or space is None or qvec is None:
        return []
    ids, M = space
    sims = M @ qvec
    order = np.argsort(-sims)
    out, seen = [], set()
    for i in order:
        if sims[i] < floor:
            break
        s = int(ids[i])
        if s in seen or (cand is not None and s not in cand):
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def best_match(G, v):
    """Argmax cosine of v against gallery rows G -> (row_index, score).
    Callers normalize (unit / unit_rows) when cosine is intended."""
    _require_np()
    sims = G @ v
    i = int(sims.argmax())
    return i, float(sims[i])


# ---------- fusion ----------

def rrf(lists, k=RRF_K):
    """Reciprocal-rank fusion: ranked id lists -> {id: fused score}.
    Ties keep first-list-first insertion order under a stable sort."""
    ranks = {}
    for lst in lists:
        for r, item in enumerate(lst):
            ranks[item] = ranks.get(item, 0.0) + 1.0 / (k + r)
    return ranks


# ---------- matrix cache ----------

class MatrixCache:
    """(ids, float32 matrix) per vector space, loaded from sqlite blob
    tables and refreshed lazily: a load within max_age seconds of the
    last is free, and past it the matrices rebuild only when the row
    count actually changed — a stable index never pays the rebuild.

    spaces: {name: (count_sql, rows_sql)} where rows_sql yields rows
    with the id first and the blob last. The FIRST space is the
    sentinel: its presence marks the cache as loaded (mirroring the
    original search.py behavior where an empty primary table meant a
    reload every call).
    """

    def __init__(self, spaces, max_age=60.0):
        self.spaces = dict(spaces)
        self.max_age = max_age
        self._state = {name: None for name in self.spaces}
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def get(self, name):
        """The (ids, matrix) tuple for a space, or None."""
        return self._state.get(name)

    def invalidate(self):
        self._loaded_at = 0.0

    def load(self, con_factory, force=False):
        if np is None:  # minimal install: no vector spaces
            return
        names = list(self.spaces)
        first = names[0]
        with self._lock:
            if not force and self._state[first] is not None and \
                    time.time() - self._loaded_at < self.max_age:
                return
            con = con_factory()
            if not force and self._state[first] is not None:
                counts = {n: con.execute(self.spaces[n][0]).fetchone()[0]
                          for n in names}
                if all(self._state[n] is not None
                       and counts[n] == len(self._state[n][0])
                       for n in names):
                    self._loaded_at = time.time()
                    con.close()
                    return
            for n in names:
                rows = con.execute(self.spaces[n][1]).fetchall()
                if rows:
                    ids = np.array([r[0] for r in rows], dtype=np.int64)
                    self._state[n] = (ids, stack_vecs([r[-1] for r in rows]))
            con.close()
            self._loaded_at = time.time()
