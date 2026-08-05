"""Reading-room items as vault notes — the ingest behind "everything in
the room is in the vault".

A room is a researched catalog of external material (readingroom.py). It
lives in Vira's own store, which makes it renderable and countable but
leaves it INVISIBLE to everything the vault is for: qocha retrieval, the
Obsidian graph, a `[[wikilink]]` from any other note. This module closes
that — it projects a room into the vault so its contents are searchable
and interconnected like any other note.

THE VAULT IS A PROJECTION TARGET, NEVER A SECOND SOURCE OF TRUTH. The
room store is canonical; every note here is derived from it and carries
`room_item_id`, so a re-run finds its own output and UPDATES it rather
than minting a duplicate. That id is the room's own stable id (a hash of
the URL), so a retitled item keeps its note and an item that survives a
full repass keeps its place in the graph. Nothing here WRITES back into
the room — but see "which note belongs to this item" at the foot: the
Reader has to be able to ASK where an item's note ended up, and that
lookup reuses the same `room_item_id` key, derived live rather than
stored.

THE POINTER-NOTE ERA ENDED 2026-08-05 (owner's ruling: pointer pages
"don't help me at all" — every item's material is fully ingested
instead; see server/fullingest.py). This module no longer MINTS
`wiki/rooms/<item>.md` pointer notes. What each item gets now is a REAL
source-summary: fullingest stages the material into raw/reading-room/,
the vault's own nightly ingest (or a bulk fleet) synthesizes the
summary carrying `room_item_id`, and fullingest.reconcile writes the
item's `vault` field and retires the item's legacy pointer note into
pending-user-deletion/. Until an item's summary lands, its legacy
pointer (if one exists from the pointer era) keeps serving; a brand-new
item simply has no note yet and the hub says so by linking nothing.

SO THE OUTPUT IS ONE SHAPE now, plus a legacy one it still reads:

  wiki/<room>-reading-room.md   type: reference — the catalog page. Every
                                item, annotated, linking each item's
                                summary (or its not-yet-retired pointer).

  wiki/rooms/<item>.md          LEGACY pointer notes, read-only here:
                                still indexed (notes_by_item) so the
                                Reader resolves them until reconcile
                                retires each one behind its summary.

Passive instances refuse outright: vault_root lives outside the cloned
data/, so a test clone writing here would land in the live Obsidian vault
(the plans.py precedent).
"""
import os
import re
from datetime import date
from pathlib import Path

from . import readingroom, vault

ROOMS_SUBDIR = "wiki/rooms"
HUB_SUBDIR = "wiki"

HUB_TYPE = "reference"

# A slug long enough to stay readable as a filename and a wikilink.
MAX_SLUG = 72


class IngestError(RuntimeError):
    pass


def _slugify(text, cap=MAX_SLUG):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if len(s) > cap:                       # cut on a word boundary, not mid-word
        s = s[:cap].rsplit("-", 1)[0] or s[:cap]
    return s.strip("-") or "untitled"


def _yaml_str(v):
    """Quote every scalar. An unquoted title carrying a colon breaks the
    Properties pane, and an unquoted [[link]] parses as a nested flow
    sequence (the vault's frontmatter-link-quoting rule)."""
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _existing_stems(root):
    """Every note filename in the vault, so a new item slug can never
    shadow a real note. Obsidian resolves [[link]] by FILENAME across
    directories, so a collision would silently re-point live links."""
    stems = {}
    for p in root.rglob("*.md"):
        stems.setdefault(p.stem, p)
    return stems


def _vault_ref(item, root):
    """The existing note for an already-consumed item, as a slug — or ''
    when the room names a note that is no longer on disk. A pointer that
    does not resolve is worse than none: it reads as consumed and leads
    nowhere."""
    raw = (item.get("vault") or "").strip()
    if not raw:
        return ""
    stem = Path(raw).stem
    if not stem:
        return ""
    return stem if (root / raw).exists() or (root / "wiki" / f"{stem}.md").exists() else ""


def _facts_line(it):
    bits = [it["mode"].capitalize(), it.get("type") or "", it.get("venue") or "",
            it.get("date") or it.get("year") or ""]
    return " · ".join(b for b in bits if b)




MIN_RECURRING = 3      # appearances before an un-paged name is worth naming


def _people_gap(room, stems):
    """Recurring names in this room that have no page yet, commonest
    first. The alternative was linking them anyway — 165 unresolved links
    across 348 notes, ghost nodes that look like knowledge and hold none.
    Naming the gap here instead turns it into a visible to-do: create the
    page and the next run links every item to it automatically."""
    counts = {}
    for it in room["items"]:
        for p in it.get("people") or []:
            if p and _slugify(p, cap=64) not in stems:
                counts[p] = counts.get(p, 0) + 1
    return sorted(((n, p) for p, n in counts.items() if n >= MIN_RECURRING),
                  key=lambda r: (-r[0], r[1]))


def hub_note(room, rows, stems=None):
    """The catalog page: every item in the room, annotated, grouped by
    priority. `rows` is [(item, target_slug_or_empty)] in room order."""
    d = room.get("definition") or {}
    slug = room["slug"]
    counts = {}
    for it in room["items"]:
        counts[it.get("prio", "?")] = counts.get(it.get("prio", "?"), 0) + 1
    modes, kinds, statuses = {}, {}, {}
    for it in room["items"]:
        modes[it.get("mode", "?")] = modes.get(it.get("mode", "?"), 0) + 1
        kinds[it.get("type", "?")] = kinds.get(it.get("type", "?"), 0) + 1
        statuses[it.get("status", "?")] = statuses.get(it.get("status", "?"), 0) + 1

    tags = ["reading-room", slug, "cat/reading-room", "reference"]
    fm = ["---",
          f"title: {_yaml_str(room['title'] + ' — reading room')}",
          f"type: {HUB_TYPE}",
          f"room_slug: {slug}",
          f"items: {len(room['items'])}",
          f"tags: [{', '.join(tags)}]",
          f"created: {date.today().isoformat()}",
          f"updated: {date.today().isoformat()}",
          # source_count is len(sources:) for every non-source-summary page
          # (the vault's §source_count convention). The catalog size is its
          # own field; conflating them is exactly what lint flags.
          "sources: []",
          "source_count: 0",
          "---"]

    b = ["", f"# {room['title']} — reading room", ""]
    if room.get("subtitle"):
        b += [room["subtitle"], ""]
    b += [f"{len(room['items'])} items. "
          + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items(),
                                                    key=lambda x: -x[1]))
          + ".", ""]
    b += ["| | |", "|---|---|"]
    b += [f"| Priority | " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + " |"]
    b += [f"| Mode | " + ", ".join(f"{v} to {k}" for k, v in sorted(modes.items())) + " |"]
    b += [f"| Consumed | " + ", ".join(f"{k} {v}" for k, v in sorted(statuses.items())) + " |"]
    b += [f"| Built | {room.get('built', '')} · updated {str(room.get('updated', ''))[:10]} |"]
    b += [""]

    if d:
        b += ["## What this room is", ""]
        if d.get("subject"):
            b += [f"**Subject.** {d['subject']}", ""]
        if d.get("why"):
            b += [f"**Why.** {d['why']}", ""]
        if d.get("people"):
            b += [f"**People.** {d['people']}", ""]
        if d.get("notes"):
            b += [f"**Method.** {d['notes']}", ""]

    b += ["## The catalog", "",
          "Each item links to its source-summary in the vault; an unlinked "
          "item's material has not been ingested yet.", ""]
    for prio in ("P1", "P2", "P3"):
        group = [(it, tgt) for it, tgt in rows if it.get("prio") == prio]
        if not group:
            continue
        b += [f"### {prio} — {len(group)} items", ""]
        for it, tgt in sorted(group, key=lambda r: (r[0].get("date") or "")):
            link = f"[[{tgt}]]" if tgt else it["title"]
            facts = _facts_line(it)
            note = f" — {it['note']}" if it.get("note") else ""
            b += [f"- {link} · {facts}{note}"]
        b += [""]
    gap = _people_gap(room, stems or {})
    if gap:
        b += ["## Recurring people with no page yet", "",
              f"Names appearing in {MIN_RECURRING}+ items that the vault has "
              "no page for. Create one and the next ingest links every item "
              "to it.", ""]
        b += [f"- {p} — {n} items" for n, p in gap]
        b += [""]

    b += ["---", "",
          "Generated from Vira's reading-room store (`server/roomvault.py`). "
          "The room is the source of truth; re-running the ingest refreshes "
          "this page. Item material is fully ingested via "
          "`server/fullingest.py` — staged raw in `raw/reading-room/`, "
          "source-summary in `wiki/`.", ""]
    return "\n".join(fm + b)


def _index_by_item_id(rooms_dir):
    """Existing pointer notes, keyed by the room item id in frontmatter —
    this is what makes a re-run an UPDATE instead of a duplicate."""
    found = {}
    if not rooms_dir.exists():
        return found
    for p in sorted(rooms_dir.glob("*.md")):
        head = p.read_text(encoding="utf-8", errors="replace")[:2000]
        m = re.search(r"^room_item_id:\s*(\S+)\s*$", head, re.M)
        if m:
            found[m.group(1)] = p
    return found


def _preserve_created(new_text, path):
    """Keep the original `created:` date on an update — a re-run must not
    rewrite the day a note entered the vault. Also returns unchanged text
    when only `updated:` would differ, so an idempotent run makes no git
    diff at all."""
    if not path.exists():
        return new_text, True
    old = path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^created:\s*(\S+)\s*$", old, re.M)
    if m:
        new_text = re.sub(r"^created:.*$", f"created: {m.group(1)}",
                          new_text, count=1, flags=re.M)
    strip = lambda t: re.sub(r"^updated:.*$", "", t, count=1, flags=re.M)
    return new_text, strip(old) != strip(new_text)


def _write(path, text, dry_run):
    text, changed = _preserve_created(text, path)
    if changed and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    return changed


def ingest(slug, dry_run=False):
    """Project one room's CATALOG into the vault. Since 2026-08-05 this
    mints no pointer notes — it renders the hub, linking each item to the
    best note that exists: the owner/reconciled summary, else the item's
    synthesized summary awaiting reconcile, else its legacy pointer note.
    Idempotent: safe to re-run after every room refresh."""
    if os.environ.get("VIRA_PASSIVE"):
        raise IngestError(
            "passive instance: vault_root is outside the cloned data/, so "
            "this would write the live Obsidian vault. Refusing.")
    room = readingroom.load_room(slug)
    if room is None:
        raise IngestError(f"no such room: {slug}")
    root = vault.vault_root()
    if not root.exists():
        raise IngestError(f"vault root does not exist: {root}")

    from . import fullingest
    known = _index_by_item_id(root / ROOMS_SUBDIR)
    summaries = fullingest.summaries_by_item(root)
    stems = _existing_stems(root)

    rows, linked, pending = [], 0, 0
    for it in room["items"]:
        ref = _vault_ref(it, root)
        if not ref and it["id"] in summaries:
            ref = summaries[it["id"]].stem
        if not ref and it["id"] in known:
            ref = known[it["id"]].stem
        rows.append((it, ref))
        if ref:
            linked += 1
        else:
            pending += 1

    hub_path = root / HUB_SUBDIR / f"{slug}-reading-room.md"
    hub_changed = _write(hub_path, hub_note(room, rows, stems), dry_run)

    orphans = sorted(p.name for iid, p in known.items()
                     if iid not in {i["id"] for i in room["items"]})
    return {
        "room": slug, "title": room["title"], "items": len(room["items"]),
        "linked": linked, "pending": pending,
        "hub": str(hub_path.relative_to(root)), "hub_changed": hub_changed,
        "orphans": orphans, "dry_run": bool(dry_run),
    }


def sync(slug):
    """Best-effort projection for a real entry point (the create_reading_room
    tool, the update route). Returns the summary dict, or None when the
    vault is unset, passive-blocked or unwritable — a room is never worth
    losing over its projection.

    This is deliberately NOT called from readingroom.build(). build() is a
    pure store write, and a cross-boundary write hung off it fires for
    every caller that never heard of a vault — including tests, which is
    exactly how 11 fixture rooms landed in the live Obsidian vault on
    2026-07-29. Entry points sync; store functions do not.

    Since 2026-08-05 it also kicks the full ingest (stage + reconcile,
    fullingest.sync — a daemon thread) so a room refresh's new items get
    their material staged for the nightly synthesis without anyone
    remembering to run anything."""
    try:
        res = ingest(slug)
    except Exception:  # noqa: BLE001 — see above
        return None
    try:
        from . import fullingest
        fullingest.sync(slug)
    except Exception:  # noqa: BLE001 — the projection already succeeded
        pass
    return res


def ingest_all(dry_run=False):
    return [ingest(r["slug"], dry_run=dry_run) for r in readingroom.list_rooms()]


# ===================== WHICH NOTE BELONGS TO THIS ITEM =====================
# The projection above is one-directional by design, which left the Reader
# unable to answer a question its own cards were asking: is this in the
# vault? An item's `vault` field is the OWNER'S SUMMARY and nothing else —
# ingest() mints a pointer note precisely when that field is EMPTY — so
# reading it as "does a note exist" reports the opposite of the truth for
# every item this module has ever catalogued. On 2026-07-30 that was 263 of
# 348 cards saying "No vault note yet" over a note sitting in the vault.
#
# THE POINTER PATH IS DERIVED, NEVER STORED. It is a fact about what is on
# disk right now, and a copy of it in the room store is a copy that goes
# stale the moment a note is renamed, moved or deleted — which is the exact
# failure being fixed here, so storing it would only move the staleness. The
# join is EXACT, not a guess: ingest() writes `room_item_id` into every
# note's frontmatter for its own idempotency, and that is the key read back.
# So there is no refresh step to run and none to forget.
#
# The owner overlay is the deliberate exception. A note that ingest() did
# not write carries no `room_item_id`, so nothing can derive the link — the
# owner attaching one by hand is genuinely new information. It lives in its
# own store and is applied at read time (the contactcard.py pattern), so it
# survives every room rebuild instead of being overwritten by the next one.

# beside the done-marks, not beside the rooms: this is owner state about a
# room, not part of the room's own regenerable store
LINKS_PATH = readingroom.ROOMS_DIR.parent / "room-links.json"

_notes_cache = {}   # rooms_dir -> (mtime, {item_id: path})


def notes_by_item(root=None):
    """Every pointer note this module has written, keyed by room item id.

    Cached on the directory's mtime: the index is a few hundred small
    header reads, cheap once and wasteful on every room load.
    """
    root = Path(root or vault.vault_root()).expanduser()
    rooms_dir = root / ROOMS_SUBDIR
    try:
        stamp = rooms_dir.stat().st_mtime
    except OSError:
        return {}
    hit = _notes_cache.get(str(rooms_dir))
    if hit and hit[0] == stamp:
        return hit[1]
    idx = {k: str(p.relative_to(root).as_posix())
           for k, p in _index_by_item_id(rooms_dir).items()}
    _notes_cache[str(rooms_dir)] = (stamp, idx)
    return idx


def _load_links():
    from . import jsonstore
    return jsonstore.read(LINKS_PATH, {"links": {}}).get("links", {})


def link_key(slug, item_id):
    return f"{slug}:{item_id}"


def set_link(slug, item_id, path):
    """Attach a vault note to an item by hand — the owner asserting a link
    the derivation cannot make. An empty path clears it."""
    from . import jsonstore
    key = link_key(slug, item_id)

    def mutate(d):
        links = d.setdefault("links", {})
        if path:
            links[key] = path
        else:
            links.pop(key, None)
        return d

    jsonstore.mutate(LINKS_PATH, mutate, {"links": {}})
    return path


def resolve(slug, items, root=None):
    """Annotate each item with where its vault note actually is.

    Three states, and the caller must be able to tell them apart because
    they mean different things to a reader:
      owner   — `vault`, a summary written from the material itself
      room    — a synthesized summary awaiting reconcile, a legacy pointer
                note, or a note the owner attached by hand
      absent  — nothing, and only then may a surface say so
    """
    from . import fullingest
    idx = notes_by_item(root)
    links = _load_links()
    vroot = Path(root or vault.vault_root()).expanduser()
    summaries = fullingest.summaries_by_item(vroot)
    for it in items:
        owner = (it.get("vault") or "").strip()
        if owner:
            it["vault_note"] = owner
            it["vault_note_kind"] = "owner"
            continue
        found = links.get(link_key(slug, it.get("id", "")))
        if not found:
            note = summaries.get(it.get("id", ""))
            if note is not None:
                found = str(note.relative_to(vroot).as_posix())
        found = found or idx.get(it.get("id", ""))
        it["vault_note"] = found or ""
        it["vault_note_kind"] = "room" if found else ""
    return items


def summary_line(res):
    return (f"{res['title']}: {res['items']} items — {res['linked']} linked "
            f"to vault notes, {res['pending']} awaiting full ingest. "
            f"Hub: {res['hub']}."
            + (f" {len(res['orphans'])} orphaned notes (item left the room)."
               if res["orphans"] else "")
            + (" [dry run]" if res["dry_run"] else ""))


if __name__ == "__main__":  # pragma: no cover — thin CLI over the functions
    import json
    import sys
    args = sys.argv[1:]
    dry = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    if args and args[0] == "ingest" and len(args) >= 2:
        print(json.dumps(ingest(args[1], dry_run=dry), indent=1))
    elif args and args[0] == "ingest-all":
        print(json.dumps(ingest_all(dry_run=dry), indent=1))
    else:
        print("usage: python -m server.roomvault ingest <slug> [--dry-run]\n"
              "       python -m server.roomvault ingest-all [--dry-run]")
