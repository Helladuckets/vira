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

_stem_cache = {"key": None, "map": None, "root": None, "at": 0.0}

# The cache KEY is a filesystem walk, so computing it to decide whether to
# rebuild cost as much as rebuilding — measured on the real vault, 1.4s of
# rglob per call before assets were indexed and 5.3s after. `resolve_ref` is
# called once per link and an index page carries thousands, so the walk is
# gated behind a short clock: a burst of links pays for one walk, and an edit
# is still picked up within a few seconds without any explicit invalidation.
_STEM_TTL = 5.0

# Never resolvable, because Obsidian does not resolve them either: dotfolders
# (.git, .obsidian, .smart-env, and any agent worktree checked out INSIDE the
# vault), plus the soft-delete staging area, which the rest of the system
# already treats as gone. Leaving these in is not merely untidy — `sorted()`
# puts a dotfolder ahead of `wiki/`, so a stale worktree won every stem
# collision and `[[supra]]` opened a months-old copy of the real note.
SKIP_DIRS = ("pending-user-deletion",)
# On a genuine tie, the curated layer wins. Anything unlisted sorts last.
DIR_RANK = ("wiki", "", "Sessions", "Briefs", "retros", "brain-retros")


def _visible(root, pattern="*.md"):
    for p in root.rglob(pattern):
        if pattern != "*.md" and not p.is_file():
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        if rel.parts and rel.parts[0] in SKIP_DIRS:
            continue
        yield rel, p


def _visible_notes(root):
    return _visible(root, "*.md")


def _visible_assets(root):
    """Every non-markdown file. `![[chart.png]]` is a wikilink too, and it was
    never in the stem map — measured 2026-08-11, all 15,143 asset embeds in
    the vault fell through to the search fallback, so an image ref answered
    with an unrelated NOTE at `exact: False`. Assets resolve by FULL filename
    (extension included), which is how Obsidian addresses them."""
    for rel, p in _visible(root, "*"):
        if rel.suffix.lower() != ".md":
            yield rel, p


def _rank(rel):
    top = rel.parts[0] if len(rel.parts) > 1 else ""
    try:
        return (DIR_RANK.index(top), len(rel.parts), rel.as_posix())
    except ValueError:
        return (len(DIR_RANK), len(rel.parts), rel.as_posix())


def _best(a, b):
    """The better of two candidates for the same key, or None-safe passthrough.

    Ranked comparison on (directory, depth) only. The third element of `_rank`
    is a lexical path tie-break, which must NOT decide a case collision — with
    it, a real `NASA.md` beside a real `nasa.md` resolves both refs to `NASA`
    because uppercase sorts first. Equal rank falls through to the caller's
    case-exact preference instead.
    """
    if a is None or b is None:
        return a or b
    return a if _rank(a)[:2] <= _rank(b)[:2] else b


def _stem_map():
    """{'exact': {stem: rel}, 'lower': {stem.lower(): rel}, 'assets': {...}}.

    Two maps, not one. The single map this replaced wrote both `p.stem` and
    `p.stem.lower()` with `setdefault` over rank-sorted notes, so a best-ranked
    file claimed only its own casing and a worst-ranked file could still claim
    the still-free case-exact key: `wiki/anthropic.md` took `anthropic`, then
    `raw/Anthropic.md` took `Anthropic`. `resolve_ref` asked for the case-exact
    key FIRST, so DIR_RANK was bypassed rather than outranked and 223 links
    opened a 0-byte stub. Keeping the two keyspaces apart lets the lookup
    compare ranks across them instead of racing them.
    """
    root = Path(vault_root())
    # Setting `key` to None is still the explicit invalidation, so a test or a
    # caller that knows the vault changed can force a rebuild.
    if (_stem_cache["key"] is not None
            and _stem_cache["root"] == str(root)
            and time.monotonic() - _stem_cache["at"] < _STEM_TTL):
        return _stem_cache["map"]
    if not root.exists():
        return {"exact": {}, "lower": {}, "assets": {}, "assets_lower": {}}
    notes = list(_visible_notes(root))
    assets = list(_visible_assets(root))
    key = (str(root), len(notes), len(assets),
           max((p.stat().st_mtime_ns for _, p in notes), default=0),
           max((p.stat().st_mtime_ns for _, p in assets), default=0))
    _stem_cache["root"], _stem_cache["at"] = str(root), time.monotonic()
    if _stem_cache["key"] == key:
        return _stem_cache["map"]
    m = {"exact": {}, "lower": {}, "names": {}, "assets": {}, "assets_lower": {}}
    for rel, _ in notes:
        # Best-ranked writer wins per key, so resolution is stable and a
        # duplicate stem elsewhere can never silently re-point existing links.
        m["exact"][rel.stem] = _best(m["exact"].get(rel.stem), rel)
        low = rel.stem.lower()
        m["lower"][low] = _best(m["lower"].get(low), rel)
        # Full filename, case-sensitive. An author who typed the extension
        # said more than one who did not, and `_clean_ref` throws it away:
        # `[[CLAUDE.md]]` (101 links, meaning the vault's spec file at the
        # root) would otherwise rank-lose to `wiki/claude.md`, since the two
        # are structurally identical to the `raw/Anthropic.md` shadowing this
        # fix exists to kill. An exact filename hit is not a guess.
        m["names"][rel.name] = _best(m["names"].get(rel.name), rel)
    for rel, _ in assets:
        m["assets"][rel.name] = _best(m["assets"].get(rel.name), rel)
        low = rel.name.lower()
        m["assets_lower"][low] = _best(m["assets_lower"].get(low), rel)
    _stem_cache["key"], _stem_cache["map"] = key, m
    return m


def _clean_ref(ref, keep_ext=False):
    """Strip the parts of a wikilink that are not the note identity.

    `keep_ext` leaves a typed `.md` on, for the caller that wants to try the
    filename verbatim before falling back to stem matching.
    """
    r = (ref or "").strip()
    r = r.split("|", 1)[0].strip()          # [[note|Label]]
    r = re.split(r"[#^]", r, maxsplit=1)[0].strip()   # [[note#h]], [[note^b]]
    if not keep_ext and r.lower().endswith(".md"):
        r = r[:-3]
    return r.strip("/ ")


def resolve_ref(ref):
    """{path, exact} for a wikilink, or None.

    `exact` False means this came from the search fallback and the caller
    should say so rather than present it as the linked note.
    """
    raw_ref = _clean_ref(ref, keep_ext=True)
    r = _clean_ref(ref)
    if not r:
        return None
    root = Path(vault_root())
    m = _stem_map()
    rel = None
    if "/" not in raw_ref and raw_ref != r:
        # An explicitly-typed `.md`, matched case-sensitively on the whole
        # filename. Only an exact hit counts — anything looser is the guess
        # the ranked path below is there to make.
        rel = m["names"].get(raw_ref)
    if rel is None and "/" in r:
        # Path-qualified. Try the literal path BEFORE forcing `.md` onto it —
        # `.with_suffix()` turns `wiki/assets/x.png` into `wiki/assets/x.md`,
        # so every path-qualified asset embed missed and fell to search.
        for cand in ((root / r), (root / r).with_suffix(".md")):
            if not cand.is_file():
                continue
            try:                            # `..` must never escape the vault
                rel = cand.resolve().relative_to(root.resolve())
            except ValueError:
                return None
            break
    if rel is None:
        # Rank decides across the two keyspaces; case-exactness is only the
        # tie-break, so a real `NASA.md`/`nasa.md` pair still resolves by case
        # while a worst-ranked stub can no longer shadow the curated note.
        exact, lower = m["exact"].get(r), m["lower"].get(r.lower())
        if exact is not None and lower is not None:
            rel = exact if _rank(exact)[:2] <= _rank(lower)[:2] else lower
        else:
            rel = exact if exact is not None else lower
    if rel is None:
        rel = m["assets"].get(r) or m["assets_lower"].get(r.lower())
    if rel is not None:
        return {"path": rel.as_posix(), "exact": True}
    found = search(r, limit=1) or []
    if found:
        return {"path": found[0]["path"], "exact": False}
    return None


def known_stems():
    """Every resolvable name, so a client can dim unresolved links without a
    round-trip per link — an index page carries thousands.

    Names only, never paths: the client strips any directory off a
    path-qualified ref before checking, so `[[wiki/anthropic|Anthropic]]` is
    tested as `anthropic`. Sending full paths instead would multiply the
    payload for a set the client would still have to normalise.
    """
    if not Path(vault_root()).exists():
        return []
    # Read straight off the resolver's own index rather than re-walking, so
    # this list cannot drift from what `resolve_ref` will actually accept —
    # the client dims links with it, and a list that disagreed would dim
    # links the server resolves fine and light up links it will refuse.
    # Assets are in here for that reason too: `![[chart.png]]` resolves.
    m = _stem_map()
    return sorted(set(m["exact"]) | set(m["assets"]))


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
