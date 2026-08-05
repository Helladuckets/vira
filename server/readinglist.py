"""The Reader's queue: everything worth reading, wherever it actually lives.

Vira generates a lot of documents — audit dossiers, plan-mode output, session
retros, morning briefs — and each one lands wherever its producing process puts
it. Before this they were scattered across as many surfaces as producers, and
the only cross-cutting thing anyone tracked was reading-room done-marks.

This module is the other half of the Reader: a registry of SOFT POINTERS. It
never copies a document and never becomes its home. An entry holds a title, a
kind, and a locator; resolving it is somebody else's job (a served URL for the
dossiers under static/, the vault for anything in the notes tree). If the
source file moves or is deleted, the entry reports `missing` and the queue
carries on — the registry is a view, not a store of record.

THE QUEUE IS ACTIVE WORK, NOT AN ARCHIVE. Marking a document read REMOVES it
from the queue (owner's call, 2026-07-27): "once I mark it complete, it is off
the list because it's still saved in all the places where they came from." The
entry is kept, with a `completed` stamp — both so the backfill sweep cannot
re-add something already read, and so a mistaken mark can be undone. It is
simply not in what `queue()` returns.

PROGRESS DEGRADES GRACEFULLY. A long document with section anchors reports
"9 of 14"; a document without them is one item you either have or have not read.
Section marks are NOT stored here — they reuse reading.py's per-list done store,
keyed by the entry's slug, so the cross-device behaviour and the 20k-key guard
come for free and there is exactly one implementation of "what have I read".

Same cross-process discipline as every other small JSON store: fresh read per
op, locked mutation, atomic write (server/jsonstore.py).
"""
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import jsonstore, reading, settings
from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "reading-list.json"

# Every directory the sweep READS, named here rather than reached for inline —
# a source that resolves its own path is a source a test cannot point
# somewhere else. See _sources().
EXPLAINER_DIR = ROOT / "static" / "explainer"
DOCS_DIR = ROOT / "static" / "docs"
# The session walkthroughs, served in place off lab_root (see walkthroughs.py).
# None means "ask walkthroughs.root()", which reads settings; a test roots it at
# its fixture like every other source here. Declared in THIS module for exactly
# the reason the docstring on _sources() gives.
WALKTHROUGH_DIR = None

# What a locator means, and therefore who resolves it.
#   url   -> a path this server already serves (the dossiers under static/)
#   vault -> a path inside the notes vault, rendered through /api/vault/note
#   plan  -> a plans.py registry id, rendered through /api/plans/{id}
LOCATOR_KINDS = ("url", "vault", "plan")

# The kinds of thing that can sit in the queue. `room` is legacy: reading
# rooms were briefly flattened into the queue (2026-07-27, first cut of this
# module) and the owner overruled it the same week — a room is a SHELF, a
# card beside My Documents, not a row inside it. Rows already registered with
# the kind stay valid (register() must not start refusing an old store), but
# the queue views exclude them and the backfill no longer sweeps them in.
KINDS = ("dossier", "plan", "retro", "brief", "room", "walkthrough")

MAX_ITEMS = 2000
MAX_TEXT = 300

# A document that predates the Reader was never queued by it, and the owner has
# almost certainly already dealt with it — a first sweep over months of retros
# would otherwise hand back a "focused attention" list of 186 things. So the
# sweep files anything older than this as already read. It stays in the store,
# shows under "recently read", and one click puts it back. Fresh documents queue.
FRESH_DAYS = 14

_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")

# A PER-SESSION retro — "2026-07-25 0205 vira" — is raw material, not a read.
# The provenance system writes one per session, so a single busy night can
# produce fifty of them; they are the INPUT to the passes that matter. What is
# written for a human is the aggregate: "<date> day <project>", "<date>
# cross-project", and the morning dossiers. Per-session retros are still
# registered (so they stay findable and can be pulled back) but they are filed
# read rather than queued, exactly like anything past the freshness window.
_PER_SESSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s+\d{3,4}\b")

# Vault subdirectories that carry readable documents, and the kind each yields.
# Empty vault_root leaves all of this dormant, which is the correct state on an
# install with no notes tree.
VAULT_SOURCES = (("Sessions", "retro"), ("Briefs", "brief"))

_H2_ID_RE = re.compile(r"<h2[^>]*\bid=[\"']([a-z0-9][a-z0-9_-]{0,63})[\"'][^>]*>(.*?)</h2>",
                       re.I | re.S)
_MD_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _clean(s, limit=MAX_TEXT):
    return " ".join(str(s or "").split())[:limit]


def slugify(text, fallback="doc"):
    """A reading.NAME_RE-legal slug, because section marks are stored under it.

    reading._path rejects anything else outright, so a slug that does not match
    would make section progress raise rather than degrade — the opposite of what
    this module promises."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60]
    s = re.sub(r"^[^a-z0-9]+", "", s)
    return s or fallback


def _blank():
    return {"items": []}


def _load():
    s = jsonstore.read(STORE, _blank())
    if not isinstance(s, dict):
        s = _blank()
    s.setdefault("items", [])
    if not isinstance(s["items"], list):
        s["items"] = []
    return s


# ---------------------------------------------------------------- registry ---

def register(title, kind, locator, locator_kind="url", *,
             ref=None, source="", created=None):
    """Add a document to the queue, or return the entry that already covers it.

    Idempotent on (locator_kind, locator): a producer may call this on every
    run, and the backfill sweep re-walks the same directories, so re-registering
    must never duplicate a row NOR resurrect one already read."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    if locator_kind not in LOCATOR_KINDS:
        raise ValueError(f"unknown locator kind: {locator_kind}")
    locator = _clean(locator, 600)
    if not locator:
        raise ValueError("locator required")
    title = _clean(title) or locator

    def fn(s):
        for it in s["items"]:
            if it.get("locator") == locator and it.get("locator_kind") == locator_kind:
                # Refresh the title (a regenerated dossier may be renamed) but
                # never touch `completed` — re-registering is not un-reading.
                it["title"] = title
                return s
        s["items"].append({
            "id": "rl_" + uuid.uuid4().hex[:10],
            "title": title,
            "kind": kind,
            "locator": locator,
            "locator_kind": locator_kind,
            "ref": _clean(ref, 64) or None,
            "source": _clean(source, 64),
            "created": _clean(created, 40) or _now(),
            "added": _now(),
            "completed": None,
            "slug": slugify(title, fallback=kind),
        })
        if len(s["items"]) > MAX_ITEMS:
            s["items"] = s["items"][-MAX_ITEMS:]
        return s

    jsonstore.mutate(STORE, fn, _blank())
    return find_by_locator(locator, locator_kind)


def find_by_locator(locator, locator_kind):
    for it in _load()["items"]:
        if it.get("locator") == locator and it.get("locator_kind") == locator_kind:
            return it
    return None


def get(item_id):
    for it in _load()["items"]:
        if it.get("id") == item_id:
            return it
    return None


def complete(item_id, done=True):
    """Mark read (drops out of the queue) or restore it. Returns the entry."""
    found = {}

    def fn(s):
        for it in s["items"]:
            if it.get("id") == item_id:
                it["completed"] = _now() if done else None
                found.update(it)
                return s
        raise KeyError(item_id)

    jsonstore.mutate(STORE, fn, _blank())
    return found


def forget(item_id):
    """Drop the pointer entirely. The document itself is never touched."""
    def fn(s):
        before = len(s["items"])
        s["items"] = [i for i in s["items"] if i.get("id") != item_id]
        if len(s["items"]) == before:
            raise KeyError(item_id)
        return s
    jsonstore.mutate(STORE, fn, _blank())
    return True


# ---------------------------------------------------------------- resolving --

def _vault_file(rel):
    root = settings.get("vault_root")
    if not root:
        return None
    p = (Path(root).expanduser() / rel)
    try:
        p = p.resolve()
        base = Path(root).expanduser().resolve()
        p.relative_to(base)            # containment, the media.py pattern
    except (OSError, ValueError):
        return None
    return p


def source_path(it):
    """The file behind an entry, when there is one we can stat. None means the
    locator is resolved by another module (a plan id) or is not a local file."""
    lk = it.get("locator_kind")
    if lk == "url":
        rel = it["locator"].lstrip("/")
        p = ROOT / "static" / rel
        return p / "index.html" if p.is_dir() else p
    if lk == "vault":
        return _vault_file(it["locator"])
    return None


def _missing(it):
    if it.get("locator_kind") == "plan":
        return False               # plans.py owns that answer, via its own list
    p = source_path(it)
    return not (p and p.is_file())


# ---------------------------------------------------------------- sections ---

def sections(it):
    """The anchored sections of a document, or [] when it has none.

    An empty list is the graceful path, not a failure: the caller falls back to
    treating the whole document as one item."""
    p = source_path(it)
    if not p or not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8", errors="replace")[:400_000]
    except OSError:
        return []
    out = []
    if p.suffix.lower() in (".html", ".htm"):
        for sid, inner in _H2_ID_RE.findall(text):
            label = _clean(_TAG_RE.sub(" ", inner), 80)
            out.append({"id": sid, "title": label or sid})
    else:
        seen = set()
        for head in _MD_H2_RE.findall(text):
            sid = slugify(head, fallback="")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append({"id": sid, "title": _clean(head, 80)})
    return out if len(out) > 1 else []      # one heading is not progress


def progress(it):
    """{done, total} across a document's sections; total 0 means whole-document."""
    secs = sections(it)
    if not secs:
        return {"done": 0, "total": 0}
    try:
        marked = set(reading.get_done(it.get("slug") or ""))
    except ValueError:
        return {"done": 0, "total": 0}
    ids = {s["id"] for s in secs}
    return {"done": len(marked & ids), "total": len(secs)}


# ------------------------------------------------------------------- views ---

def _decorate(it, *, with_progress=True):
    out = dict(it)
    out["missing"] = _missing(it)
    out["progress"] = progress(it) if with_progress else {"done": 0, "total": 0}
    return out


def _dedupe(items):
    """Collapse entries that are the same document reached two ways.

    A plan is registered TWICE: once by `plans.save_plan` as `plan:pl_...`, and
    again by the sitedocs sweep as `url:/docs/plans/....html`, because
    register() is idempotent per (locator_kind, locator) and those are two
    different keys. Measured here: 11 of 551. It went unnoticed while the list
    was a flat date-ordered column and became obvious the moment documents were
    grouped by feature — one build reading as two.

    The key is the SLUG, and that is not a heuristic: `slug` is what
    reading.py stores section marks under, so two entries sharing one already
    share their read state — they are one document by the store's own
    reckoning. The `plan` locator wins because plans.py resolves it (it serves
    the markdown and knows whether the file is still there); a `url` row for a
    deleted plan would render as present."""
    rank = {"plan": 0, "vault": 1, "url": 2}
    best = {}
    for it in items:
        key = it.get("slug") or it.get("id")
        cur = best.get(key)
        if cur is None or rank.get(it.get("locator_kind"), 9) < \
                rank.get(cur.get("locator_kind"), 9):
            best[key] = it
    return _keep(items, best)


def _read_slugs():
    """Slugs of every document with a completed stamp.

    Needed because de-duplicating queue() and completed() SEPARATELY leaves a
    twin pair split across the two lists — mark one copy read and the other is
    still queued, which is the same document asking to be read again. Measured:
    2 survived the per-list pass. Reading one copy is reading the document."""
    return {it.get("slug") for it in _load()["items"]
            if it.get("completed") and it.get("slug")}


def _keep(items, best):
    # Preserve the caller's ordering rather than dict order, so the sort a
    # caller applied before de-duplicating still means something.
    keep = {id(v) for v in best.values()}
    return [it for it in items if id(it) in keep]


def queue():
    """What is left to read, newest first. Completed entries are NOT here,
    and neither are rooms — a room is its own card in the Reader, tracked by
    its own done-mark store, not a row in My Documents."""
    read = _read_slugs()
    live = [i for i in _load()["items"]
            if not i.get("completed") and i.get("kind") != "room"
            and i.get("slug") not in read]
    live.sort(key=lambda i: i.get("created") or i.get("added") or "", reverse=True)
    return [_decorate(i) for i in _dedupe(live)]


def completed(limit=50):
    """Recently finished, newest first — for an undo affordance, not a library."""
    done = [i for i in _load()["items"]
            if i.get("completed") and i.get("kind") != "room"]
    done.sort(key=lambda i: i.get("completed") or "", reverse=True)
    return [_decorate(i, with_progress=False) for i in _dedupe(done)[:limit]]


def counts():
    items = [i for i in _load()["items"] if i.get("kind") != "room"]
    return {"queued": sum(1 for i in items if not i.get("completed")),
            "completed": sum(1 for i in items if i.get("completed")),
            "total": len(items)}


# ---------------------------------------------------------------- backfill ---

def _explainer_dossiers():
    """Generated HTML under static/explainer/ — already served, so url locators.

    The four hand-authored atlas pages at the top level are the system
    explainer, not reading; only subdirectories (one dossier each) are taken."""
    found = []
    try:
        dirs = sorted(d for d in EXPLAINER_DIR.iterdir() if d.is_dir())
    except OSError:
        return found
    for d in dirs:
        index = d / "index.html"
        if not index.is_file():
            continue
        try:
            head = index.read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            continue
        m = _TITLE_RE.search(head)
        title = _clean(_TAG_RE.sub(" ", m.group(1))) if m else d.name
        found.append({"title": title, "kind": "dossier",
                      "locator": f"/explainer/{d.name}/", "locator_kind": "url",
                      "created": _file_created(index)})
    return found


def _file_created(p, name=""):
    """When a document was made. The filename's date prefix is the truth where
    there is one — the retros and briefs are named `YYYY-MM-DD ...` and their
    mtime is whenever the file was last rsynced, which is not the same thing."""
    m = _DATE_PREFIX_RE.match(name or (p.name if p else ""))
    if m:
        return m.group(1) + "T00:00:00"
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).astimezone() \
            .isoformat(timespec="seconds")
    except OSError:
        return _now()


def _vault_documents():
    root = settings.get("vault_root")
    if not root:
        return []
    base = Path(root).expanduser()
    found = []
    for sub, kind in VAULT_SOURCES:
        d = base / sub
        try:
            files = sorted(d.glob("*.md"))
        except OSError:
            continue
        for f in files:
            found.append({"title": f.stem, "kind": kind,
                          "locator": f"{sub}/{f.name}", "locator_kind": "vault",
                          "created": _file_created(f, f.stem),
                          "queue": not _PER_SESSION_RE.match(f.stem)})
    return found


def _rooms():
    """RETIRED from the backfill (2026-07-27, owner's call): rooms are cards
    in the Reader's home, not queue rows. Kept as a named seam because a
    future 'register a room's page itself' need would land here."""
    out = []
    for p in reading.list_pages():
        out.append({"title": p["title"], "kind": "room",
                    "locator": p["url"], "locator_kind": "url"})
    return out


def _plans():
    from . import plans                       # lazy: plans imports settings too
    out = []
    try:
        rows = plans.list_plans()
    except Exception:                         # noqa: BLE001 — a dormant vault
        return out                            # is a state, not a failure
    for p in rows:
        out.append({"title": p.get("title") or "Untitled plan", "kind": "plan",
                    "locator": p["id"], "locator_kind": "plan",
                    "ref": p["id"], "created": p.get("created")})
    return out


def _sitedocs():
    """Documents migrated off thedurham.nyc plus every plan the repointed
    plan-mode hook has rendered since — static/docs/, served at /docs/.
    sitedocs owns the registry/manifest read; lazy import breaks the cycle
    (sitedocs imports this module for producer registration).

    DOCS_DIR is passed EXPLICITLY. registry_rows() falls back to sitedocs's own
    module-level path when it is not, and that fallback is invisible from here
    — which is exactly how this sweep spent a fortnight reading the owner's
    real documents out from under every test that thought it had isolated it."""
    try:
        from . import sitedocs
        return sitedocs.registry_rows(DOCS_DIR)
    except Exception:
        return []


def _walkthroughs():
    """The build films off lab_root, served in place at /walkthroughs/.
    Dormant (empty) when no lab_root is configured, which is every install but
    the owner's."""
    try:
        from . import walkthroughs
        return walkthroughs.rows(WALKTHROUGH_DIR)
    except Exception:
        return []


def _sources():
    """Every producer the sweep reads, declared in one place.

    A source listed here must take its root from THIS module (DOCS_DIR,
    EXPLAINER_DIR) or from settings — never from a path its own module resolved
    at import time. Otherwise a test that patches everything it knows about
    still sweeps the live checkout, and the failure looks like a bad assertion
    rather than a missing seam. tests/test_readinglist.py roots all of these at
    one fixture directory and keeps a guard test that an empty root finds
    nothing; a new source that skips the seam fails that test on sight."""
    return (_explainer_dossiers() + _plans() + _vault_documents()
            + _sitedocs() + _walkthroughs())


def _is_stale(created, days=FRESH_DAYS):
    """Older than the freshness window, so the sweep files it as already read."""
    try:
        made = datetime.fromisoformat(str(created))
    except (TypeError, ValueError):
        return False                    # unknown age is treated as fresh
    if made.tzinfo is None:
        made = made.astimezone()
    return (datetime.now(timezone.utc) - made).days > days


def backfill(source="backfill", days=FRESH_DAYS):
    """Register everything already on disk. Safe to re-run: register() is
    idempotent on the locator and never clears a completed stamp.

    Anything older than the freshness window is filed as already read rather
    than queued — see FRESH_DAYS. Only ever applied to an entry the sweep is
    seeing for the first time, so it can never re-file something the owner
    deliberately put back."""
    added = queued = 0
    for row in _sources():
        before = find_by_locator(row["locator"], row["locator_kind"])
        try:
            it = register(row["title"], row["kind"], row["locator"],
                          row["locator_kind"], ref=row.get("ref"),
                          source=source, created=row.get("created"))
        except ValueError:
            continue
        if before:
            continue
        added += 1
        if row.get("queue", True) and not _is_stale(it.get("created"), days):
            queued += 1
        else:
            complete(it["id"])
    return {"added": added, "queued_new": queued, **counts()}
