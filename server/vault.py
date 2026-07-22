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


def note_text(path):
    return _vault().note_text(path)


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
