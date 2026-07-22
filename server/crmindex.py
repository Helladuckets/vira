"""The CRM as a searchable database, not just a lookup table.

`data.search_people()` is a lowercase substring scan over name, emails
and phones, so it answers an exactly-spelled name and nothing else:
1,006 people carrying relationship summaries, how-we-met stories,
topics, open loops and personal facts, all unreachable by any other
route. "who works in insurance" returned nothing, ever.

So the CRM gets the same treatment as every other corpus here: a sqlite
sidecar with FTS5 over the whole record plus one embedding per person,
fused with RRF through the shared primitives in retrieval.py. Nothing
about the shape is new — this is mediaindex's pattern at 1/13th the
size.

Two deliberate choices:

  - FTS is built synchronously and vectors fill in behind it. The blob
    for 1,006 people indexes in well under a second; embedding them
    through Ollama does not, and a search must never wait on a model.
    Rows carry `pending` until their vector lands (see embed_pending).
  - Freshness is a source fingerprint, not a watcher. The CRM files
    change when a background enrichment or a profile save rewrites
    them, so the cheap check is people.json + master.json + the
    profiles directory: mtimes and counts, compared on read.
"""
import json
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

from . import data as crm
from . import retrieval, settings

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "crm-index.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS people(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  pid TEXT UNIQUE, name TEXT, text TEXT, pending INTEGER DEFAULT 1);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(name, text);
CREATE TABLE IF NOT EXISTS vecs(seq INTEGER PRIMARY KEY, v BLOB);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, val TEXT);
"""

_matrices = retrieval.MatrixCache({"text": ("SELECT COUNT(*) FROM vecs",
                                            "SELECT seq, v FROM vecs")})
_refreshed_at = 0.0
REFRESH_S = 120


def _con():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def available():
    return DB.exists()


def invalidate():
    global _refreshed_at
    _refreshed_at = 0.0
    _matrices.invalidate()


# ---------- the searchable blob ----------

def _stamp():
    """Source fingerprint: cheap to compute, changes whenever any CRM
    file a person's text is built from changes."""
    root = settings.crm_root()
    parts = []
    for name in ("people.json", "master.json"):
        f = root / name
        parts.append(f"{name}:{f.stat().st_mtime if f.exists() else 0}")
    pdir = root / "profiles"
    if pdir.exists():
        files = sorted(pdir.glob("*.json"))
        newest = max((f.stat().st_mtime for f in files), default=0)
        parts.append(f"profiles:{len(files)}:{newest}")
    return "|".join(parts)


def _person_text(p, master, profile):
    """Everything about one person that is worth matching on, as one
    blob: identity, handles, the master card, and the profile's prose."""
    bits = [p.get("name") or ""]
    bits += (p.get("refs", {}) or {}).get("card_names", []) or []
    h = p.get("handles", {}) or {}
    bits += h.get("emails", []) + h.get("phones10", []) + h.get("imessage", [])
    if master:
        bits += [master.get(k) or "" for k in
                 ("full_name", "company", "title", "relationship",
                  "evidence")]
    if profile:
        bits += [profile.get(k) or "" for k in
                 ("relationship_class", "relationship_summary", "how_we_met",
                  "comms_style")]
        for t in profile.get("topics") or []:
            bits += [t.get("topic") or "", t.get("quote") or ""]
        for loop in (profile.get("open_loops") or []) + \
                (profile.get("resolved_loops") or []):
            bits += [loop.get("what") or "", loop.get("how") or ""]
        for fact in profile.get("personal_facts") or []:
            bits.append(fact.get("fact") or "")
        for hook in profile.get("hooks") or []:
            bits += [hook.get("angle") or "", hook.get("detail") or ""]
    return " \n".join(b for b in bits if b)[:8000]


def refresh(force=False, log=lambda *a: None):
    """Rebuild the index when the CRM files moved underneath it. Full
    rebuild, not incremental: at this size it costs milliseconds and
    cannot drift."""
    global _refreshed_at
    if not force and time.time() - _refreshed_at < REFRESH_S:
        return {"rebuilt": False}
    stamp = _stamp()
    con = _con()
    try:
        row = con.execute("SELECT val FROM state WHERE key='stamp'").fetchone()
        if not force and row and row["val"] == stamp:
            _refreshed_at = time.time()
            return {"rebuilt": False}

        c = crm._load()
        profiles = c["profiles"]
        rows = []
        for p in c["people"]:
            pid = p["id"]
            rows.append((pid, p.get("name") or "",
                         _person_text(p, c["master"].get(pid),
                                      profiles.get(pid))))
        # keep the vectors of people whose text did not change: embedding
        # is the only expensive part of this whole module
        old = {r["pid"]: (r["seq"], r["text"]) for r in
               con.execute("SELECT seq, pid, text FROM people")}
        # `pending` means "still needs a vector", so it is answered by the
        # vecs table, NOT by whether the text changed. Deriving it from
        # sameness alone marked every unchanged-but-never-embedded row as
        # done, and a rebuild that landed mid-fill stranded the rest
        # lexical-only forever.
        embedded = {r[0] for r in con.execute("SELECT seq FROM vecs")}
        con.execute("DELETE FROM people")
        con.execute("DELETE FROM fts")
        keep = []
        for pid, name, text in rows:
            prev = old.get(pid)
            same = prev and prev[1] == text
            seq = prev[0] if same else None
            cur = con.execute(
                "INSERT INTO people(seq, pid, name, text, pending)"
                " VALUES(?,?,?,?,?)",
                (seq, pid, name, text,
                 0 if (same and seq in embedded) else 1))
            seq = seq or cur.lastrowid
            con.execute("INSERT INTO fts(rowid, name, text) VALUES(?,?,?)",
                        (seq, name, text))
            if same:
                keep.append(seq)
        if keep:
            con.execute("DELETE FROM vecs WHERE seq NOT IN (%s)"
                        % ",".join("?" * len(keep)), keep)
        else:                      # NOT IN (NULL) matches nothing in sql,
            con.execute("DELETE FROM vecs")     # so empty means empty here
        con.execute("INSERT OR REPLACE INTO state(key,val) VALUES('stamp',?)",
                    (stamp,))
        con.execute(
            "INSERT OR REPLACE INTO state(key,val) VALUES('built_at',?)",
            (str(time.time()),))
        con.commit()
        log(f"crm index: {len(rows)} people, {len(rows) - len(keep)} changed")
    finally:
        con.close()
    _refreshed_at = time.time()
    _matrices.invalidate()
    return {"rebuilt": True, "people": len(rows)}


def embed_pending(limit=128, log=lambda *a: None):
    """Fill vectors for rows FTS is already serving. Runs in the
    background tick; a search never waits for it."""
    from . import localmodels
    con = _con()
    try:
        rows = con.execute(
            "SELECT seq, text FROM people WHERE pending=1 LIMIT ?",
            (limit,)).fetchall()
        if not rows:
            return 0
        vecs = localmodels.ollama_embed(
            [f"search_document: {r['text'][:6000]}" for r in rows])
        if not vecs:
            return 0
        for r, v in zip(rows, vecs):
            con.execute("INSERT OR REPLACE INTO vecs(seq, v) VALUES(?,?)",
                        (r["seq"], retrieval.pack_vec(v)))
            con.execute("UPDATE people SET pending=0 WHERE seq=?", (r["seq"],))
        con.commit()
        _matrices.invalidate()
        log(f"crm index: embedded {len(rows)}")
        return len(rows)
    finally:
        con.close()


@lru_cache(maxsize=256)
def _qvec(q):
    from . import localmodels
    v = localmodels.ollama_embed([f"search_query: {q}"])
    return v[0] if v else None


# ---------- search ----------

def search(q=None, limit=20, exact=False, person=None, order="relevance",
           phrases=()):
    """Hybrid over the CRM. Empty query means browse: most recently
    contacted first, which is what `data.search_people` already
    considers the natural order for people."""
    q = (q or "").strip()
    if person and not q:
        detail = crm.get_person(person)
        if not detail:
            return []
        return [_row(detail["person"], None)]
    if not q:
        rows = crm.search_people(limit=limit,
                                 sort="alpha" if order == "oldest"
                                 else "recent")
        for r in rows:                  # one row shape for the whole group
            r.setdefault("snippet", None)
            r.setdefault("score", None)
        return rows

    refresh()
    _matrices.load(_con)
    con = _con()
    try:
        fts = retrieval.rank_fts(con, q, None, limit=200, phrases=phrases)
        qv = _qvec(q) if not exact else None
        lists = [fts]
        if qv is not None:
            lists.append(retrieval.rank_vec(_matrices.get("text"), qv, None,
                                            floor=0.45))
        ranks = retrieval.rrf(lists)
        top = sorted(ranks, key=ranks.get, reverse=True)
        if exact and fts:
            top = fts + [s for s in top if s not in set(fts)]
        top = top[:limit]
        if not top:
            return []
        got = {r["seq"]: r for r in con.execute(
            "SELECT seq, pid, text FROM people WHERE seq IN (%s)"
            % ",".join("?" * len(top)), top)}
    finally:
        con.close()

    c = crm._load()
    out = []
    for seq in top:
        r = got.get(seq)
        p = c["by_id"].get(r["pid"]) if r else None
        if not p:
            continue
        out.append(_row(p, _snippet(r["text"], q), ranks.get(seq)))
    return out


def _row(p, snippet, score=None):
    row = crm.person_summary(p, crm._load()["profiles"])
    row["snippet"] = snippet
    row["score"] = round(score, 5) if score else None
    return row


def _snippet(text, q):
    """The first line that actually contains a query word — a match the
    reader can see beats a truncated preamble."""
    words = [w.lower() for w in q.split() if len(w) > 2]
    for line in (text or "").split("\n"):
        low = line.lower()
        if any(w in low for w in words) and len(line.strip()) > 12:
            return line.strip()[:240]
    return (text or "").split("\n")[0][:240]


def status():
    if not DB.exists():
        return {"available": False, "people": 0, "vectors": 0}
    con = _con()
    try:
        return {
            "available": True,
            "people": con.execute("SELECT COUNT(*) FROM people").fetchone()[0],
            "vectors": con.execute("SELECT COUNT(*) FROM vecs").fetchone()[0],
            "pending": con.execute(
                "SELECT COUNT(*) FROM people WHERE pending=1").fetchone()[0],
            "built_at": (con.execute(
                "SELECT val FROM state WHERE key='built_at'").fetchone()
                or [None])[0],
        }
    finally:
        con.close()


if __name__ == "__main__":       # python -m server.crmindex [embed]
    import sys
    print(json.dumps(refresh(force=True, log=print), indent=1))
    if "embed" in sys.argv:
        while embed_pending(log=print):
            pass
    print(json.dumps(status(), indent=1))
