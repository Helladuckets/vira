"""TC-IL vault index + grounded ask — now a thin adapter over qocha.

The engine that lived here was extracted 2026-07-20 into the standalone
qocha package (pip-installed editable from ~/workspace/qocha; see that
repo's README): heading-path chunking, FTS5 + local-embedding hybrid
search with RRF fusion, citation-validated ask, the sqlite sidecar
schema — all unchanged, so the existing data/vault-index.sqlite keeps
working with no re-index. This module keeps Vira's public surface and
seams exactly as they were:

  - config comes from settings (vault_root / vault_dirs), re-read on
    every access so a config.json edit takes effect without a restart
  - embeddings route through localmodels.ollama_embed (one Ollama
    client for the whole app, and the tests' mock seam)
  - ask() answers through suggest.complete (the backend ladder +
    aihealth accounting, and the tests' mock seam)
  - module-level DB_PATH / _vec_state / _connect / _init stay
    patchable — tests and atlas._vault_edges depend on them

Everything else delegates to a lazily (re)built qocha.Vault.
"""
import re
import threading
import time
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path

from qocha import Config as _QochaConfig, Vault as _QochaVault
from qocha.chunker import (CHUNK_MAX, CHUNK_TARGET,  # noqa: F401 — re-export
                           chunk_markdown)

from . import settings

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "vault-index.sqlite"

VAULT_RESCAN_S = 300
DEFAULT_DIRS = ["wiki", "Briefs", "Sessions", "retros", "brain-retros"]

# shared with the active qocha.Vault so tests can reset the cache in place
_vec_state = {"gen": -1, "ids": None, "mat": None}


def vault_root() -> Path:
    raw = str(settings.get("vault_root") or "").strip()
    # Unset must resolve to a path that never exists — Path("") is the cwd,
    # which would silently index the repo itself. Every consumer treats a
    # missing root as dormant, so a never-created sentinel keeps them all off.
    return (Path(raw).expanduser() if raw
            else Path.home() / ".vira" / "vault-unset")


def vault_dirs():
    return list(settings.get("vault_dirs") or DEFAULT_DIRS)


class _ViraEmbedder:
    """qocha embedder protocol over Vira's shared Ollama client."""

    def embed_documents(self, texts):
        from . import localmodels
        return localmodels.ollama_embed(
            [f"search_document: {t}"[:6000] for t in texts])

    def embed_query(self, text):
        from . import localmodels
        vecs = localmodels.ollama_embed([f"search_query: {text}"[:6000]])
        return vecs[0] if vecs else None


def _answer(prompt):
    from . import suggest
    return suggest.complete(prompt)


_active = {"key": None, "vault": None}
_build_lock = threading.Lock()


def _vault() -> _QochaVault:
    """The active qocha.Vault, rebuilt when the settings that shape it
    change (root, dirs, owner) or when a test patches DB_PATH."""
    key = (str(vault_root()), tuple(vault_dirs()), str(DB_PATH),
           str(settings.get("owner_name") or ""))
    with _build_lock:
        if _active["key"] != key:
            cfg = _QochaConfig(
                root=vault_root(), dirs=vault_dirs(), db=DB_PATH,
                owner=settings.get("owner_name") or "the owner")
            v = _QochaVault(cfg.root, config=cfg,
                            embedder=_ViraEmbedder(), answerer=_answer)
            v._vec_state = _vec_state          # shared, test-resettable
            _active.update(key=key, vault=v)
        return _active["vault"]


# ---------- the public surface (unchanged) ----------

def scan_once():
    return _vault().scan()


def embed_pending(limit=2000):
    return _vault().embed_pending(limit=limit)


def search(q, limit=10):
    return _vault().search(q, limit=limit)


def search_filtered(q, limit=10, since=None, until=None, order="relevance"):
    """Hybrid hits narrowed to a date window and optionally re-ordered by
    note age. qocha ranks by similarity alone; `notes.mtime` has been in
    the schema since the start but nothing ever queried it, which is why
    "the most recent session where..." was unanswerable. ISO dates in,
    hits out with `mtime` attached.

    With no query text this is a pure browse: newest (or oldest) notes in
    the window, one row per note.
    """
    lo = _epoch(since)
    hi = _epoch(until)
    q = (q or "").strip()
    con = _connect()
    try:
        _init(con)
        if not q:
            where, params = [], []
            if lo is not None:
                where.append("n.mtime >= ?")
                params.append(lo)
            if hi is not None:
                where.append("n.mtime < ?")
                params.append(hi)
            rows = con.execute(
                "SELECT n.path, n.title, n.mtime, c.heading, c.text "
                "FROM notes n LEFT JOIN chunks c"
                " ON c.path=n.path AND c.seq=0"
                + (" WHERE " + " AND ".join(where) if where else "")
                + " ORDER BY n.mtime " + ("ASC" if order == "oldest"
                                          else "DESC")
                + " LIMIT ?", (*params, limit)).fetchall()
            return [{"path": r["path"], "title": r["title"],
                     "heading": r["heading"] or "", "text": r["text"] or "",
                     "mtime": r["mtime"], "score": None} for r in rows]

        # A filtered or re-ordered search has to over-fetch, and by a lot:
        # "the newest note about X" means the newest of ALL the notes
        # about X, not the newest of the ten the ranker happened to like
        # best. A date window can cut the entire similarity head too.
        deep = (max(limit * 8, 200)
                if (lo is not None or hi is not None or order != "relevance")
                else limit)
        hits = _vault().search(q, limit=deep)
        mt = {r["path"]: r["mtime"] for r in
              con.execute("SELECT path, mtime FROM notes")}
    finally:
        con.close()

    out = []
    for h in hits:
        m = mt.get(h["path"])
        if lo is not None and (m is None or m < lo):
            continue
        if hi is not None and (m is None or m >= hi):
            continue
        out.append(dict(h, mtime=m))
    if order in ("recent", "oldest"):
        out.sort(key=lambda h: h["mtime"] or 0, reverse=order == "recent")
    return out[:limit]


def grep_notes(text, limit=None, since=None, until=None, order="recent"):
    """Literal, exhaustive substring match over every indexed chunk.

    Nothing here ranks and nothing here truncates by relevance. This is the
    path that was missing entirely: a similarity retriever cannot answer
    "show me every note that mentions X", and that is most of what a work
    record is asked for. The engine returned the top 8 by cosine and the
    right note sat at rank 34 (2026-07-25) -- no amount of tuning fixes a
    question the contract cannot express.

    Ordered by note age, because when you have every match the useful axis is
    time, not score.
    """
    text = (text or "").strip()
    if not text:
        return []
    lo, hi = _epoch(since), _epoch(until)
    con = _connect()
    try:
        _init(con)
        sql = ("SELECT c.path, n.title, c.heading, c.text, n.mtime "
               "FROM chunks c JOIN notes n ON n.path = c.path "
               "WHERE c.text LIKE ? ESCAPE '\\' OR n.title LIKE ? ESCAPE '\\' "
               "   OR c.path LIKE ? ESCAPE '\\'")
        params = []
        pat = "%" + text.replace("\\", "\\\\").replace(
            "%", "\\%").replace("_", "\\_") + "%"
        params.extend([pat, pat, pat])
        if lo is not None:
            sql += " AND n.mtime >= ?"
            params.append(lo)
        if hi is not None:
            sql += " AND n.mtime < ?"
            params.append(hi)
        sql += " ORDER BY n.mtime " + ("ASC" if order == "oldest" else "DESC")
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    out, seen = [], set()
    for r in rows:
        key = (r["path"], r["heading"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"path": r["path"], "title": r["title"],
                    "heading": r["heading"] or "", "text": r["text"] or "",
                    "mtime": r["mtime"], "score": None, "literal": True})
    return out


def _epoch(iso):
    """ISO date -> local-midnight unix seconds (mtime's own units)."""
    if not iso:
        return None
    try:
        return datetime.combine(date.fromisoformat(str(iso)[:10]),
                                dtime.min).timestamp()
    except ValueError:
        return None


def ask(question, k=10, hits=None):
    return _vault().ask(question, k=k, hits=hits)


def note_text(path, cap=None):
    """Uncapped by default -- the Reader serves a note whole.

    `cap` is for context-window callers and truncates HONESTLY (the
    engine appends an in-band marker). See qocha's note_text docstring.
    """
    return _vault().note_text(path, cap=cap)


# ------------------------------------------------------- wikilink resolution
# A `[[wikilink]]` is a FILENAME identifier, not a search query. Obsidian
# resolves it by exact stem across the whole vault; resolving it through the
# ranked hybrid search instead was measured wrong on 27% of the links in this
# vault that point at notes which genuinely exist — `[[claude]]` opened
# `types-of-claude-interfaces`, `[[supra]]` opened a consultation transcript.
# A wrong note presented as the right one is worse than an honest miss, so
# exact match answers first and search is only ever a labelled fallback.

_stem_cache = {"key": None, "map": None}

# Never resolvable, because Obsidian does not resolve them either: dotfolders
# (.git, .obsidian, .smart-env, and any agent worktree checked out INSIDE the
# vault), plus the soft-delete staging area, which the rest of the system
# already treats as gone. Leaving these in is not merely untidy — `sorted()`
# puts a dotfolder ahead of `wiki/`, so a stale worktree won every stem
# collision and `[[supra]]` opened a months-old copy of the real note.
SKIP_DIRS = ("pending-user-deletion",)
# On a genuine tie, the curated layer wins. Anything unlisted sorts last.
DIR_RANK = ("wiki", "", "Sessions", "Briefs", "retros", "brain-retros")


def _visible_notes(root):
    for p in root.rglob("*.md"):
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        if rel.parts and rel.parts[0] in SKIP_DIRS:
            continue
        yield rel, p


def _rank(rel):
    top = rel.parts[0] if len(rel.parts) > 1 else ""
    try:
        return (DIR_RANK.index(top), len(rel.parts), rel.as_posix())
    except ValueError:
        return (len(DIR_RANK), len(rel.parts), rel.as_posix())


def _stem_map():
    root = Path(vault_root())
    if not root.exists():
        return {}
    notes = sorted(_visible_notes(root), key=lambda t: _rank(t[0]))
    key = (str(root), len(notes),
           max((p.stat().st_mtime_ns for _, p in notes), default=0))
    if _stem_cache["key"] == key:
        return _stem_cache["map"]
    m = {}
    for _, p in notes:
        # Best-ranked writer wins, so resolution is stable and a duplicate
        # stem elsewhere can never silently re-point existing links.
        m.setdefault(p.stem, p)
        m.setdefault(p.stem.lower(), p)
    _stem_cache["key"], _stem_cache["map"] = key, m
    return m


def _clean_ref(ref):
    """Strip the parts of a wikilink that are not the note identity."""
    r = (ref or "").strip()
    r = r.split("|", 1)[0].strip()          # [[note|Label]]
    r = re.split(r"[#^]", r, maxsplit=1)[0].strip()   # [[note#h]], [[note^b]]
    if r.lower().endswith(".md"):
        r = r[:-3]
    return r.strip("/ ")


def resolve_ref(ref):
    """{path, exact} for a wikilink, or None.

    `exact` False means this came from the search fallback and the caller
    should say so rather than present it as the linked note.
    """
    r = _clean_ref(ref)
    if not r:
        return None
    root = Path(vault_root())
    m = _stem_map()
    hit = m.get(r) or m.get(r.lower())
    if hit is None and "/" in r:
        cand = (root / r).with_suffix(".md")
        if cand.exists():
            hit = cand
    if hit is not None:
        try:
            rel = hit.resolve().relative_to(root.resolve())
        except ValueError:
            return None                     # outside the vault: never serve
        return {"path": rel.as_posix(), "exact": True}
    found = search(r, limit=1) or []
    if found:
        return {"path": found[0]["path"], "exact": False}
    return None


def known_stems():
    """Every note stem, so a client can dim unresolved links without a
    round-trip per link — an index page carries thousands."""
    root = Path(vault_root())
    if not root.exists():
        return []
    # Same filter as `_stem_map`, or the client would dim links the server
    # resolves fine, and light up links it will refuse.
    return sorted({p.stem for _, p in _visible_notes(root)})


def status():
    return _vault().status()


def person_notes(name, limit=6):
    """Vault notes that mention a person — the person-page seam."""
    name = (name or "").strip()
    if not name:
        return []
    hits = search(name, limit=24)
    by_path, order = {}, []
    for h in hits:
        if h["path"] not in by_path:
            by_path[h["path"]] = h
            order.append(h["path"])
    return [{"path": p, "title": by_path[p]["title"],
             "heading": by_path[p]["heading"],
             "snippet": by_path[p]["text"][:280]}
            for p in order[:limit]]


def _connect():
    """Raw connection to the index (atlas's FTS-only co-mention signal)."""
    return _vault()._connect()


def _init(con):
    _QochaVault._init(con)


class VaultIndexer(threading.Thread):
    """Background maintainer: incremental rescan + vector fill. Dormant
    (cheap no-op ticks) when the vault root does not exist."""

    def __init__(self):
        super().__init__(daemon=True, name="vira-vault-indexer")
        self._stop = threading.Event()

    def run(self):
        time.sleep(5)                    # let the server finish booting
        while not self._stop.is_set():
            try:
                scan_once()
                embed_pending()
            except Exception:  # noqa: BLE001 — the indexer never dies
                pass
            self._stop.wait(VAULT_RESCAN_S)

    def stop(self):
        self._stop.set()
