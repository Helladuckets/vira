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
        s = json.loads(STORE.read_text())
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
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
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


def update(idea_id, text=None, status=None, note=None, project=None):
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
                it["updated"] = _now()
                _save(s)
                return it
    raise KeyError(idea_id)


def remove(idea_id):
    with _lock, locked(STORE):
        s = _load()
        before = len(s["items"])
        s["items"] = [it for it in s["items"] if it["id"] != idea_id]
        if len(s["items"]) == before:
            raise KeyError(idea_id)
        _save(s)
    return {"removed": idea_id}
