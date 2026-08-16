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
import hashlib
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
SOURCE_PREFIX = "@"
SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")

# shared with the active qocha.Vault so tests can reset the cache in place
_vec_state = {"gen": -1, "ids": None, "mat": None}
_extra_vec_states = {}


def vault_root() -> Path:
    raw = str(settings.get("vault_root") or "").strip()
    # Unset must resolve to a path that never exists — Path("") is the cwd,
    # which would silently index the repo itself. Every consumer treats a
    # missing root as dormant, so a never-created sentinel keeps them all off.
    return (Path(raw).expanduser() if raw
            else Path.home() / ".vira" / "vault-unset")


def vault_dirs():
    return list(settings.get("vault_dirs") or DEFAULT_DIRS)


def _source_id(value, root):
    """Stable, URL/path-safe source id for a configured vault."""
    raw = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    if SOURCE_ID_RE.fullmatch(raw or ""):
        return raw
    base = raw[:36].strip("-") or "vault"
    digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def _inside(path, root):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _overlaps(a, b):
    return _inside(a, b) or _inside(b, a)


def source_specs():
    """Every connected markdown source, primary first.

    `vault_root` remains the one WRITE target used by plans, definitions and
    ingestion. `vault_sources` adds read-only roots for Find and vault chat.
    An old `vault_dirs` entry that points outside the primary root is promoted
    in memory to an extra source; this migrates the pre-feature workaround
    without indexing the same files twice or requiring a config rewrite.
    """
    primary_root = Path(vault_root()).expanduser()
    raw_dirs = vault_dirs()
    primary_dirs, legacy = [], []
    for item in raw_dirs:
        candidate = (primary_root / str(item)).expanduser()
        if _inside(candidate, primary_root):
            rel = candidate.resolve().relative_to(primary_root.resolve())
            primary_dirs.append(rel.as_posix())
        else:
            legacy.append(candidate.resolve())

    specs = [{
        "id": "primary", "name": primary_root.name or "Primary vault",
        "root": primary_root, "dirs": primary_dirs, "primary": True,
        "db": Path(DB_PATH),
    }]
    configured = settings.get("vault_sources") or []
    rows = list(configured) if isinstance(configured, list) else []
    rows += [{"name": p.name, "root": str(p), "legacy": True}
             for p in legacy]
    connected_roots = [primary_root.resolve()]
    used_ids = {"primary"}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_root = str(row.get("root") or "").strip()
        if not raw_root:
            continue
        root = Path(raw_root).expanduser().resolve()
        key = str(root)
        if any(_overlaps(root, connected) for connected in connected_roots):
            continue
        connected_roots.append(root)
        sid = _source_id(row.get("id") or row.get("name") or root.name, root)
        if sid in used_ids:
            sid = _source_id(f"{sid}-{hashlib.sha1(key.encode()).hexdigest()[:6]}",
                             root)
        used_ids.add(sid)
        configured_dirs = row.get("dirs")
        if isinstance(configured_dirs, list):
            dirs = []
            for item in configured_dirs:
                candidate = (root / str(item)).expanduser()
                if _inside(candidate, root):
                    rel = candidate.resolve().relative_to(root.resolve())
                    dirs.append(rel.as_posix())
        else:
            dirs = None
        specs.append({
            "id": sid, "name": str(row.get("name") or root.name or sid),
            "root": root, "dirs": dirs, "primary": False,
            "legacy": bool(row.get("legacy")),
            "db": Path(DB_PATH).parent / "vault-indexes" / f"{sid}.sqlite",
        })
    return specs


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


_active = {"key": None, "vault": None, "rows": []}
_build_lock = threading.Lock()


def _vault_rows():
    """[{spec, vault}], rebuilt when any connected source changes."""
    specs = source_specs()
    key = (tuple((s["id"], str(s["root"]), tuple(s["dirs"] or ()),
                  str(s["db"])) for s in specs),
           str(settings.get("owner_name") or ""))
    with _build_lock:
        if _active["key"] != key:
            built = []
            for spec in specs:
                cfg = _QochaConfig(
                    root=spec["root"], dirs=spec["dirs"], db=spec["db"],
                    owner=settings.get("owner_name") or "the owner")
                v = _QochaVault(cfg.root, config=cfg,
                                embedder=_ViraEmbedder(), answerer=_answer)
                if spec["primary"]:
                    v._vec_state = _vec_state      # public test/atlas seam
                else:
                    v._vec_state = _extra_vec_states.setdefault(
                        spec["id"], {"gen": -1, "ids": None, "mat": None})
                built.append({"spec": spec, "vault": v})
            _active.update(key=key,
                           vault=built[0]["vault"] if built else None,
                           rows=built)
        return _active["rows"]


def _vault() -> _QochaVault:
    """The primary vault (or first connected source when primary is absent)."""
    rows = _vault_rows()
    if not rows:
        raise RuntimeError("no vault configured")
    return rows[0]["vault"]


def _public_path(spec, rel):
    rel = str(rel).replace("\\", "/").lstrip("/")
    return rel if spec["primary"] else f"{SOURCE_PREFIX}{spec['id']}/{rel}"


def _source_path(path):
    """(row, vault-relative path) for one public source-aware path."""
    raw = str(path or "").strip().replace("\\", "/")
    rows = _vault_rows()
    if raw.startswith(SOURCE_PREFIX):
        head, sep, rel = raw[1:].partition("/")
        if not sep or not rel:
            raise ValueError("invalid vault path")
        row = next((r for r in rows if r["spec"]["id"] == head), None)
        if row is None:
            raise ValueError("unknown vault source")
        return row, rel
    if not rows:
        raise ValueError("no vault configured")
    return rows[0], raw


def _hit(row, hit):
    out = dict(hit)
    spec = row["spec"]
    out["path"] = _public_path(spec, hit.get("path") or "")
    out["vault_id"] = spec["id"]
    out["vault_name"] = spec["name"]
    return out


# ---------- the public surface (unchanged) ----------

def scan_once():
    total = {"changed": 0, "removed": 0, "seen": 0, "vaults": []}
    for row in _vault_rows():
        spec = row["spec"]
        try:
            result = row["vault"].scan()
        except Exception as exc:  # one disconnected disk never blocks others
            result = {"error": str(exc)[:200]}
        total["vaults"].append({"id": spec["id"], "name": spec["name"],
                                **result})
        for key in ("changed", "removed", "seen"):
            total[key] += int(result.get(key) or 0)
    return total


def embed_pending(limit=2000):
    total = 0
    left = max(0, int(limit))
    for row in _vault_rows():
        if left <= 0:
            break
        done = int(row["vault"].embed_pending(limit=left) or 0)
        total += done
        left -= done
    return total


def search(q, limit=10):
    hits = []
    for row in _vault_rows():
        if not row["spec"]["root"].is_dir():
            continue
        try:
            hits.extend(_hit(row, h) for h in
                        row["vault"].search(q, limit=max(limit * 2, 20)))
        except Exception:  # a missing/unmounted source is an honest partial
            continue
    hits.sort(key=lambda h: float(h.get("score") or 0), reverse=True)
    return hits[:limit]


def search_filtered(q, limit=10, since=None, until=None, order="relevance"):
    """Hybrid hits narrowed to a date window and optionally re-ordered by
    note age. qocha ranks by similarity alone; `notes.mtime` has been in
    the schema since the start but nothing ever queried it, which is why
    "the most recent session where..." was unanswerable. ISO dates in,
    hits out with `mtime` attached.

    With no query text this is a pure browse: newest (or oldest) notes in
    the window, one row per note.
    """
    lo, hi = _epoch(since), _epoch(until)
    q = (q or "").strip()
    out = []
    for row in _vault_rows():
        if not row["spec"]["root"].is_dir():
            continue
        try:
            out.extend(_search_filtered_one(row, q, max(limit, 1), lo, hi,
                                            order))
        except Exception:  # a disconnected source must not hide the others
            continue
    if order in ("recent", "oldest"):
        out.sort(key=lambda h: h["mtime"] or 0, reverse=order == "recent")
    elif q:
        out.sort(key=lambda h: float(h.get("score") or 0), reverse=True)
    else:
        out.sort(key=lambda h: h["mtime"] or 0, reverse=True)
    return out[:limit]


def _search_filtered_one(row, q, limit, lo, hi, order):
    con = row["vault"]._connect()
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
            return [_hit(row, {"path": r["path"], "title": r["title"],
                                "heading": r["heading"] or "",
                                "text": r["text"] or "",
                                "mtime": r["mtime"], "score": None})
                    for r in rows]

        # A filtered or re-ordered search has to over-fetch, and by a lot:
        # "the newest note about X" means the newest of ALL the notes about
        # X, not the newest of the ten the ranker happened to like best.
        deep = (max(limit * 8, 200)
                if (lo is not None or hi is not None or order != "relevance")
                else limit)
        hits = row["vault"].search(q, limit=deep)
        mt = {r["path"]: r["mtime"] for r in
              con.execute("SELECT path, mtime FROM notes")}
    finally:
        con.close()

    out = []
    for h in hits:
        mtime = mt.get(h["path"])
        if lo is not None and (mtime is None or mtime < lo):
            continue
        if hi is not None and (mtime is None or mtime >= hi):
            continue
        out.append(_hit(row, dict(h, mtime=mtime)))
    return out


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
    out = []
    for row in _vault_rows():
        if not row["spec"]["root"].is_dir():
            continue
        try:
            out.extend(_grep_one(row, text, lo, hi))
        except Exception:
            continue
    out.sort(key=lambda h: h["mtime"] or 0, reverse=order != "oldest")
    return out[:limit] if limit else out


def _grep_one(row, text, lo, hi):
    con = row["vault"]._connect()
    try:
        _init(con)
        sql = ("SELECT c.path, n.title, c.heading, c.text, n.mtime "
               "FROM chunks c JOIN notes n ON n.path = c.path "
               "WHERE (c.text LIKE ? ESCAPE '\\' "
               "OR n.title LIKE ? ESCAPE '\\' "
               "OR c.path LIKE ? ESCAPE '\\')")
        pat = "%" + text.replace("\\", "\\\\").replace(
            "%", "\\%").replace("_", "\\_") + "%"
        params = [pat, pat, pat]
        if lo is not None:
            sql += " AND n.mtime >= ?"
            params.append(lo)
        if hi is not None:
            sql += " AND n.mtime < ?"
            params.append(hi)
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    out, seen = [], set()
    for result in rows:
        key = (result["path"], result["heading"])
        if key in seen:
            continue
        seen.add(key)
        out.append(_hit(row, {
            "path": result["path"], "title": result["title"],
            "heading": result["heading"] or "", "text": result["text"] or "",
            "mtime": result["mtime"], "score": None, "literal": True,
        }))
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
    merged = search(question, limit=k) if hits is None else hits
    return _vault().ask(question, k=k, hits=merged)


def note_text(path, cap=None):
    """Uncapped by default -- the Reader serves a note whole.

    `cap` is for context-window callers and truncates HONESTLY (the
    engine appends an in-band marker). See qocha's note_text docstring.
    """
    row, rel = _source_path(path)
    return row["vault"].note_text(rel, cap=cap)


def asset_path(path):
    """Resolve a source-aware asset path without ever leaving its vault."""
    try:
        row, rel = _source_path(path)
    except ValueError:
        return None
    root = row["spec"]["root"].resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target if target.is_file() and target.suffix.lower() != ".md" else None


def primary_path(path):
    """Whether a public path belongs to the primary/write vault."""
    try:
        row, _ = _source_path(path)
    except ValueError:
        return False
    return bool(row["spec"]["primary"])


# ------------------------------------------------------- wikilink resolution
# A `[[wikilink]]` is a FILENAME identifier, not a search query. Obsidian
# resolves it by exact stem across the whole vault; resolving it through the
# ranked hybrid search instead was measured wrong on 27% of the links in this
# vault that point at notes which genuinely exist — `[[claude]]` opened
# `types-of-claude-interfaces`, `[[supra]]` opened a consultation transcript.
# A wrong note presented as the right one is worse than an honest miss, so
# exact match answers first and search is only ever a labelled fallback.

_stem_cache = {"key": None, "map": None, "root": None, "at": 0.0}
_extra_stem_caches = {}

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


def _stem_map(row=None):
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
    if row is None:
        rows = _vault_rows()
        row = rows[0] if rows else None
    root = row["spec"]["root"] if row else Path(vault_root())
    cache = (_stem_cache if not row or row["spec"]["primary"] else
             _extra_stem_caches.setdefault(
                 row["spec"]["id"],
                 {"key": None, "map": None, "root": None, "at": 0.0}))
    # Setting `key` to None is still the explicit invalidation, so a test or a
    # caller that knows the vault changed can force a rebuild.
    if (cache["key"] is not None
            and cache["root"] == str(root)
            and time.monotonic() - cache["at"] < _STEM_TTL):
        return cache["map"]
    if not root.exists():
        return {"exact": {}, "lower": {}, "names": {}, "assets": {},
                "assets_lower": {}}
    notes = list(_visible_notes(root))
    assets = list(_visible_assets(root))
    key = (str(root), len(notes), len(assets),
           max((p.stat().st_mtime_ns for _, p in notes), default=0),
           max((p.stat().st_mtime_ns for _, p in assets), default=0))
    cache["root"], cache["at"] = str(root), time.monotonic()
    if cache["key"] == key:
        return cache["map"]
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
    cache["key"], cache["map"] = key, m
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


def _resolve_ref_one(row, ref):
    """{path, exact} for a wikilink, or None.

    `exact` False means this came from the search fallback and the caller
    should say so rather than present it as the linked note.
    """
    raw_ref = _clean_ref(ref, keep_ext=True)
    r = _clean_ref(ref)
    if not r:
        return None
    root = row["spec"]["root"]
    m = _stem_map(row)
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
        # `.md` is APPENDED, never `with_suffix`: pathlib reads the last
        # dotted run as the extension, so `Claude 3.5 Sonnet for sparking
        # creativity` becomes `Claude 3.md` and the note is unreachable.
        # 34 links in this vault have a dot mid-filename.
        for cand in ((root / r), Path(str(root / r) + ".md")):
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
        hit = {"path": _public_path(row["spec"], rel.as_posix()),
               "exact": True}
        if not row["spec"]["primary"]:
            hit.update(vault_id=row["spec"]["id"],
                       vault_name=row["spec"]["name"])
        return hit
    return None


def resolve_ref(ref, from_path=None):
    """Resolve within the current note's source first, then every other.

    A source context prevents `[[index]]` in a secondary vault from opening
    the primary vault's same-named note. Without context, the primary vault
    keeps the historical precedence.
    """
    if not _clean_ref(ref):
        return None
    rows = list(_vault_rows())
    if from_path:
        try:
            context, _ = _source_path(from_path)
            rows = [context] + [r for r in rows if r is not context]
        except ValueError:
            pass
    for row in rows:
        if not row["spec"]["root"].is_dir():
            continue
        hit = _resolve_ref_one(row, ref)
        if hit is not None:
            return hit
    found = search(_clean_ref(ref), limit=1) or []
    if found:
        return {"path": found[0]["path"], "exact": False,
                "vault_id": found[0].get("vault_id", "primary"),
                "vault_name": found[0].get("vault_name", "Primary vault")}
    return None


def known_stems():
    """Every resolvable name, so a client can dim unresolved links without a
    round-trip per link — an index page carries thousands.

    Names only, never paths: the client strips any directory off a
    path-qualified ref before checking, so `[[wiki/anthropic|Anthropic]]` is
    tested as `anthropic`. Sending full paths instead would multiply the
    payload for a set the client would still have to normalise.
    """
    # Read straight off the resolver's own index rather than re-walking, so
    # this list cannot drift from what `resolve_ref` will actually accept —
    # the client dims links with it, and a list that disagreed would dim
    # links the server resolves fine and light up links it will refuse.
    # Assets are in here for that reason too: `![[chart.png]]` resolves.
    names = set()
    for row in _vault_rows():
        if not row["spec"]["root"].exists():
            continue
        m = _stem_map(row)
        names.update(m["exact"])
        names.update(m["assets"])
    return sorted(names)


def status():
    vaults = []
    for row in _vault_rows():
        spec = row["spec"]
        try:
            state = row["vault"].status()
        except Exception as exc:
            state = {"root": str(spec["root"]), "db": str(spec["db"]),
                     "notes": 0, "chunks": 0, "vectors": 0,
                     "last_scan": None, "available": False,
                     "error": str(exc)[:200]}
        vaults.append({**state, "id": spec["id"], "name": spec["name"],
                       "primary": spec["primary"],
                       "legacy": bool(spec.get("legacy"))})
    first = vaults[0] if vaults else {}
    return {
        "root": first.get("root", str(vault_root())),
        "db": first.get("db", str(DB_PATH)),
        "notes": sum(int(v.get("notes") or 0) for v in vaults),
        "chunks": sum(int(v.get("chunks") or 0) for v in vaults),
        "vectors": sum(int(v.get("vectors") or 0) for v in vaults),
        "last_scan": max((str(v.get("last_scan") or "") for v in vaults),
                         default="") or None,
        "available": any(v.get("available") for v in vaults),
        "vaults": vaults,
    }


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
             "vault_id": by_path[p].get("vault_id", "primary"),
             "vault_name": by_path[p].get("vault_name"),
             "heading": by_path[p]["heading"],
             "snippet": by_path[p]["text"][:280]}
            for p in order[:limit]]


def _connect():
    """Primary raw index connection (atlas's co-mention signal is local)."""
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
