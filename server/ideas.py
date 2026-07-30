"""Ideas / on-hold backlog — Vira's own cross-session roadmap and the SOURCE
OF TRUTH for it.

Items accumulate here across sessions (seeded from the session retros, then
grown by the owner adding ideas in the Ideas & On-Hold window as they occur
them). `/resume` reads this store instead of scraping the latest retro's
"Ideas" section, and `/close-session` folds a session's new ideas / still-open
items back into it. Stored in data/ideas.json — regenerable UI state in shape,
but the canonical backlog in role, so writes are atomic (tmp+rename) and the
file is worth backing up if it grows valuable.

Item shape:
  { "id": "idea_<hex>", "text": str, "status": open|on-hold|done|dropped,
    "project": str, "source": str, "note": str,
    "created": ISO8601, "updated": ISO8601 }

Every idea belongs to a PROJECT so the backlog can serve all of the owner's
projects, not just Vira itself. The store keeps a curated `projects` list
(projects added by the owner, which may not have any ideas yet) alongside
the items; the effective project list is the union of that list, every
project actually used on an item, and the default project.
"""
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .filelock import locked

STORE = Path(__file__).resolve().parent.parent / "data" / "ideas.json"
# "proposed" = staged by Vira (the muse routine / propose_idea tool),
# awaiting the owner's Approve (-> open, optionally auto-built) or
# Decline (-> dropped). Nothing proposed ever runs without approval.
STATUSES = ("proposed", "open", "on-hold", "done", "dropped")
# Historically every idea was about Vira itself; that stays the default so
# pre-existing (project-less) items land under "Vira" on migration.
DEFAULT_PROJECT = "Vira"

_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load():
    """Fresh read every time — detached job runners close out their idea
    (server/session._mark_idea) from their own process, so a cache here
    would clobber their writes. Migration (stamping project-less items with
    the default) is applied in memory and persists on the next mutation."""
    try:
        # encoding pinned on BOTH ends (see _save): the store is written
        # ensure_ascii=False, so it genuinely holds non-ASCII owner prose
        # — read with the platform default it would mojibake on Windows.
        s = json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        s = {"items": [], "projects": []}
    if not isinstance(s, dict) or "items" not in s:
        s = {"items": [], "projects": []}
    s.setdefault("projects", [])
    for it in s["items"]:
        if not it.get("project"):
            it["project"] = DEFAULT_PROJECT
    return s


def _save(s):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_name(STORE.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(STORE)


def list_items():
    return list(_load()["items"])


def list_projects():
    """Effective project list: the default project first, then every other
    project (curated or used on an item) sorted case-insensitively."""
    s = _load()
    used = {(it.get("project") or DEFAULT_PROJECT) for it in s["items"]}
    curated = {p for p in s["projects"] if p}
    names = used | curated | {DEFAULT_PROJECT}
    rest = sorted((n for n in names if n != DEFAULT_PROJECT),
                  key=str.lower)
    return [DEFAULT_PROJECT] + rest


def _register_project(s, name):
    """Add a project to the curated list if it is new (case-insensitive)."""
    name = (name or "").strip()
    if not name:
        return
    existing = {p.lower() for p in s["projects"]}
    if name.lower() != DEFAULT_PROJECT.lower() and name.lower() not in existing:
        s["projects"].append(name)


def add_project(name):
    name = (name or "").strip()
    if not name:
        raise ValueError("empty project name")
    with _lock, locked(STORE):
        s = _load()
        _register_project(s, name)
        _save(s)
    return list_projects()


# ---------- a project's folder on disk ----------
# A project used to be a NAME and nothing else, while every dispatch asked
# the owner to re-type a target repo into the run sheet. The two are the
# same fact, so the project carries it: "connect a project" means point at
# the folder, and Plan/Implement default their cwd to it.
#
# Stored as a SEPARATE map rather than turning `projects` into objects —
# every reader of that list expects strings, and a shape change would be a
# migration for no gain. A name with no entry is simply unconnected.

def project_paths():
    return dict(_load().get("project_paths") or {})


def project_path(name):
    return project_paths().get((name or "").strip(), "")


def set_project_path(name, path):
    """Point a project at a folder. Registers the project if it is new, so
    connecting a folder is one act rather than two."""
    name = (name or "").strip()
    if not name:
        raise ValueError("empty project name")
    path = (path or "").strip()
    if path:
        p = Path(path).expanduser()
        # Refuse rather than store: a path that is not a directory yields a
        # dispatch that dies at launch, and the error would surface far from
        # the moment that caused it.
        if not p.is_dir():
            raise ValueError(f"not a folder: {path}")
        path = str(p.resolve())
    with _lock, locked(STORE):
        s = _load()
        _register_project(s, name)
        paths = dict(s.get("project_paths") or {})
        if path:
            paths[name] = path
        else:
            paths.pop(name, None)     # empty clears the connection
        s["project_paths"] = paths
        _save(s)
    return {"projects": list_projects(), "project_paths": project_paths()}


def add(text, status="open", source="manual", note="", project=None):
    text = (text or "").strip()
    if not text:
        raise ValueError("empty idea")
    project = (project or "").strip() or DEFAULT_PROJECT
    with _lock, locked(STORE):
        s = _load()
        _register_project(s, project)
        now = _now()
        item = {
            "id": "idea_" + uuid.uuid4().hex[:10],
            "text": text,
            "status": status if status in STATUSES else "open",
            "project": project,
            "source": (source or "manual").strip(),
            "note": (note or "").strip(),
            "created": now,
            "updated": now,
        }
        s["items"].insert(0, item)
        _save(s)
    return item


def update(idea_id, text=None, status=None, note=None, project=None,
           tags_add=None, tags_drop=None):
    """`tags_add` / `tags_drop` are the owner's CORRECTIONS to the derived
    tags (server/ideatags.py), and they live here rather than in the tag
    sidecar on purpose: the sidecar is rewritten whenever an idea is
    re-tagged, so a correction stored there would be silently reverted by
    the next pass. Overlaid at read time — the contactcard.py pattern.

    A dropped tag is also removed from tags_add (and vice versa), so the
    two lists can never disagree about one tag."""
    with _lock, locked(STORE):
        s = _load()
        for it in s["items"]:
            if it["id"] == idea_id:
                if text is not None:
                    t = text.strip()
                    if t:
                        it["text"] = t
                if status is not None and status in STATUSES:
                    it["status"] = status
                if note is not None:
                    it["note"] = note.strip()
                if project is not None:
                    p = project.strip()
                    if p:
                        it["project"] = p
                        _register_project(s, p)
                if tags_add is not None:
                    it["tags_add"] = {a: list(v) for a, v in
                                      (tags_add or {}).items() if v}
                if tags_drop is not None:
                    it["tags_drop"] = list(tags_drop or [])
                dropped = set(it.get("tags_drop") or [])
                if dropped and it.get("tags_add"):
                    it["tags_add"] = {
                        a: [t for t in v if t not in dropped]
                        for a, v in it["tags_add"].items()}
                    it["tags_add"] = {a: v for a, v in it["tags_add"].items()
                                      if v}
                it["updated"] = _now()
                _save(s)
                return it
    raise KeyError(idea_id)


def stamp_note(idea_id, text, status=None, append=False):
    """The one composer for outcome notes the automation seams stamp onto
    ideas (session._mark_idea, circuits._finalize, judge.record_and_close,
    the approve/decline routes). Replaces the idea's note with `text` — or,
    with append=True, joins onto any existing note with the ' · '
    separator (the judge convention). Optional status flip rides the same
    write. Raises KeyError for an unknown idea, exactly like update()."""
    if append:
        it = next((i for i in list_items() if i["id"] == idea_id), None)
        prior = (it.get("note") or "") if it else ""
        if prior:
            text = f"{prior} · {text}"
    kw = {"note": text}
    if status is not None:
        kw["status"] = status
    return update(idea_id, **kw)


def remove(idea_id):
    with _lock, locked(STORE):
        s = _load()
        before = len(s["items"])
        s["items"] = [it for it in s["items"] if it["id"] != idea_id]
        if len(s["items"]) == before:
            raise KeyError(idea_id)
        _save(s)
    return {"removed": idea_id}
