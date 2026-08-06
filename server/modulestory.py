"""The build story behind a module — right-click a window, "What is this?".

Answers with the module registry's own description plus every document the
library holds about that surface: the films, plans, dossiers, retros and
briefs that chronicle how it was built. The owner's framing, from the day
the built-history library shipped: this stuff is the story of how everything
got built — and the module itself should be its front door.

Everything is DERIVED at read time — the blurb from modulemap, the documents
from readinglist + doctags, the film thumbs from walkthroughs — so the story
cannot drift from the stores it reads (the onboard.steps discipline). The one
curated thing here is WINDOWS: which doc-tag spellings and which registry
entry each window id means. Doc tags are model-minted on ideatags' module
axis, so the table names the spellings in use today; a tag the vocabulary
later converges differently simply stops matching and the panel reports the
thinner answer honestly, rather than any layer guessing.

Read-only; no store of its own; nothing here needs a passive guard.
"""
from __future__ import annotations


# Window id -> the story's ingredients.
#   map   the modulemap registry id whose `what` describes this surface to a
#         stranger (the blurb at the top of the panel)
#   tags  module-axis doc tags that count as this window's story. The window
#         id itself always counts too, so a vocabulary that converges on the
#         id needs no table edit.
#   alias companions resolve to their host window's story.
WINDOWS = {
    "feed":         {"map": "feed-win",
                     "tags": ["sources", "mail", "send", "whatsapp"]},
    "people":       {"map": "people-win",
                     "tags": ["people", "radar", "contact-card", "groups"]},
    "work":         {"map": "work-win",
                     "tags": ["queue", "job-runner", "session-cockpit",
                              "routines", "circuits", "flows", "forge",
                              "orphan-work"]},
    "brief":        {"map": "brief-win", "tags": ["brief"]},
    "journal":      {"map": "journal-win", "tags": ["journal", "brief"]},
    "triage":       {"map": "triage-win", "tags": ["triage"]},
    "applications": {"map": "applications-win",
                     "tags": ["applications", "job-search", "job-boards"]},
    "find":         {"map": "find-win",
                     "tags": ["find", "search-and-recall", "brain",
                              "define"]},
    "find-cloud":   {"alias": "find"},
    "find-related": {"alias": "find"},
    "find-define":  {"alias": "find"},
    "evidence":     {"map": "evidence-win",
                     "tags": ["evidence-ledger", "evidence"]},
    "atlas":        {"map": "atlas-win", "tags": ["atlas", "network"]},
    "map":          {"map": "map-win", "tags": ["module-map", "system-map"]},
    "subs":         {"map": "subs-win", "tags": ["subscriptions"]},
    "subsviz":      {"map": "picker-win",
                     "tags": ["morning-picker", "subs-visuals"]},
    "design":       {"map": "design-studio",
                     "tags": ["design-studio", "skins", "genre-studio"]},
    "reader":       {"map": "reader-win",
                     "tags": ["reader", "reading-room", "reading-list"]},
    "setup":        {"map": "setup-win",
                     "tags": ["config", "models", "onboarding", "sources",
                              "environment-doctor"]},
}

# How a story orders its shelves; the client renders one section per kind.
KIND_ORDER = ("walkthrough", "dossier", "plan", "brief", "retro")


def resolve(win_id):
    """The canonical window id a story is asked about, or None."""
    row = WINDOWS.get(win_id)
    if row and row.get("alias"):
        win_id = row["alias"]
        row = WINDOWS.get(win_id)
    return (win_id, row) if row else (win_id, None)


def _registry_entry(map_id):
    """The modulemap row behind a window, or {} — a missing registry (fresh
    install, fixture clone) is a thinner story, never an error."""
    if not map_id:
        return {}
    try:
        from . import modulemap
        for m in modulemap.list_modules():
            if m.get("id") == map_id:
                return m
    except Exception:
        pass
    return {}


def story(win_id):
    """{title, what, docs, counts, pending} for one window, or None.

    `docs` is every library entry (read and unread — the build story is
    mostly read documents) whose module-axis tags intersect the window's
    tag set, film metadata joined the same way /api/reading/list joins it.
    `pending` is how many library documents have no tags yet, so the panel
    can say a thin story is still being tagged rather than implying this is
    all there ever was."""
    win_id, row = resolve(win_id)
    if not row:
        return None
    from . import doctags, readinglist, walkthroughs

    tags = set(row.get("tags") or []) | {win_id}
    docs = doctags.annotate(readinglist.library())
    hits = [d for d in docs
            if tags & set((d.get("tags") or {}).get("module") or [])]

    films = {}
    try:
        films = {f["url"]: f for f in
                 walkthroughs.films(readinglist.WALKTHROUGH_DIR)}
    except Exception:
        pass
    for d in hits:
        f = films.get(d.get("locator"))
        if f:
            d["film"] = {"thumb": f.get("thumb"), "motion": f.get("motion"),
                         "project": f.get("project"),
                         "subject": f.get("subject"),
                         "description": f.get("description")}

    try:
        from . import docthumbs
        docthumbs.annotate(hits)
    except Exception:
        pass

    reg = _registry_entry(row.get("map"))
    counts = {}
    for d in hits:
        counts[d.get("kind")] = counts.get(d.get("kind"), 0) + 1
    return {
        "id": win_id,
        "title": reg.get("name") or "",
        "what": reg.get("what") or "",
        "tags": sorted(tags),
        "docs": hits,
        "counts": counts,
        "pending": sum(1 for d in docs if not d.get("tagged")),
    }
