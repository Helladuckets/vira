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
full repass keeps its place in the graph. Nothing here reads back into
the room.

WHAT DOES *NOT* GET A NOTE. An item the owner has already consumed
carries `vault: wiki/<slug>.md` — a real source-summary written from the
material itself. Minting a pointer note beside it would put two nodes in
the graph for one thing and bury the substantive one. So those items are
catalogued in the hub, linking straight at the existing note, and the
existing note is never touched: it is the owner's, written from a raw
source, and a machine pass has no business editing it.

SO THE OUTPUT IS TWO SHAPES, matching the vault's own taxonomy
(~/TC-IL/CLAUDE.md §Taxonomy):

  wiki/<room>-reading-room.md   type: reference — the catalog page. Every
                                item, annotated with the room's own read
                                of why it is there. `reference` is the
                                vault's existing word for a catalog that
                                points outward at external resources.

  wiki/rooms/<item>.md          type: reading-room-item — one per
                                un-consumed item. Deliberately NOT
                                `source-summary`: that type asserts a raw
                                file was read and synthesized, and these
                                are pointers to material the owner has
                                not consumed yet. Claiming otherwise
                                would corrupt the density ladder the
                                whole vault reads by.

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

ITEM_TYPE = "reading-room-item"
HUB_TYPE = "reference"

MAX_PERSON_TAGS = 8
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


def _yaml_list(vals):
    return "[" + ", ".join(_yaml_str(v) for v in vals) + "]"


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


def _person_link(name, stems):
    """Link a person only when their page exists. An unresolved link per
    person across hundreds of items would fill the graph with ghost nodes
    that look like knowledge and hold none."""
    slug = _slugify(name, cap=64)
    return f"[[{slug}]]" if slug in stems else name


def _facts_line(it):
    bits = [it["mode"].capitalize(), it.get("type") or "", it.get("venue") or "",
            it.get("date") or it.get("year") or ""]
    return " · ".join(b for b in bits if b)


def _status_phrase(status):
    return {"HAVE": "already in the vault",
            "PARTIAL": "met secondhand — not consumed directly",
            "MISSING": "not consumed yet"}.get(status, status)


def item_note(it, room, stems, root):
    """Render one item note. Pure — takes everything it needs, so the
    writer can diff it against what is on disk before touching the file."""
    people = [p for p in (it.get("people") or []) if p]
    links = [_person_link(p, stems) for p in people]
    ref = _vault_ref(it, root)
    hub = f"{room['slug']}-reading-room"

    tags = ["reading-room", room["slug"], "cat/reading-room"]
    if it.get("type"):
        tags.append(it["type"])
    for p in people[:MAX_PERSON_TAGS]:
        tags.append(_slugify(p, cap=64))
    seen, uniq = set(), []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)

    fm = [
        "---",
        f"title: {_yaml_str(it['title'])}",
        f"type: {ITEM_TYPE}",
        f"room: {_yaml_str(f'[[{hub}]]')}",
        f"room_slug: {room['slug']}",
        f"room_item_id: {it['id']}",
        f"room_status: {it.get('status', '')}",
        f"item_type: {it.get('type', '')}",
        f"mode: {it.get('mode', '')}",
        f"prio: {it.get('prio', '')}",
    ]
    if it.get("url"):
        fm.append(f"url: {_yaml_str(it['url'])}")
    if it.get("date") or it.get("year"):
        fm.append(f"date: {it.get('date') or it.get('year')}")
    if it.get("venue"):
        fm.append(f"venue: {_yaml_str(it['venue'])}")
    if people:
        fm.append(f"people: {_yaml_list(people)}")
    if it.get("pay"):
        fm.append("paywalled: true")
    fm.append(f"tags: [{', '.join(uniq)}]")
    fm.append(f"created: {date.today().isoformat()}")
    fm.append(f"updated: {date.today().isoformat()}")
    if ref:
        fm.append("sources:")
        fm.append(f"  - {_yaml_str(f'[[{ref}]]')}")
        fm.append("source_count: 1")
    else:
        fm.append("sources: []")
        fm.append("source_count: 0")
    fm.append("---")

    body = ["", f"# {it['title']}", ""]
    if it.get("note"):
        body += [it["note"], ""]
    body.append(f"**{_facts_line(it)}** · {it.get('prio', '')} · "
                f"{_status_phrase(it.get('status', ''))}"
                + (" · paywalled" if it.get("pay") else ""))
    body.append("")
    if it.get("url"):
        body += [f"[Open the source]({it['url']})", ""]
    if it.get("why"):
        body += ["## Why it is in the room", "", it["why"], ""]
    if links:
        body += ["## People", "", ", ".join(links), ""]
    if ref:
        body += ["## Already in the vault", "",
                 f"Consumed and summarized at [[{ref}]].", ""]
    body += ["---", "",
             f"Catalogued in [[{hub}]] — the *{room['title']}* reading room. "
             "This page is a pointer to external material, not a summary of "
             "it; the room store in Vira is the source of truth.", ""]
    return "\n".join(fm + body)


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
          "Un-consumed items link to their pointer page in `wiki/rooms/`; "
          "items already consumed link straight to their summary.", ""]
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
          "this page and every pointer under `wiki/rooms/`.", ""]
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
    """Project one room into the vault. Idempotent: safe to re-run after
    every room refresh. Returns a summary dict."""
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

    rooms_dir = root / ROOMS_SUBDIR
    known = _index_by_item_id(rooms_dir)
    stems = _existing_stems(root)
    # Our own pointer pages must not block slug allocation for themselves.
    ours = {p.stem for p in known.values()}

    rows, written, updated, linked, taken = [], 0, 0, 0, set()
    for it in room["items"]:
        ref = _vault_ref(it, root)
        if ref:
            # Already consumed — the real summary is the target. No pointer.
            rows.append((it, ref))
            linked += 1
            continue

        path = known.get(it["id"])
        if path is None:
            base = _slugify(it["title"])
            cand = base
            n = 0
            while (cand in stems and cand not in ours) or cand in taken:
                n += 1
                cand = (f"{base}-{_slugify(it.get('type') or 'item')}"
                        if n == 1 else f"{base}-{it['id'][:6]}")
                if n > 2:
                    cand = f"{base}-{it['id']}"
                    break
            path = rooms_dir / f"{cand}.md"
        taken.add(path.stem)
        rows.append((it, path.stem))
        text = item_note(it, room, stems, root)
        existed = path.exists()
        if _write(path, text, dry_run):
            if existed:
                updated += 1
            else:
                written += 1

    hub_path = root / HUB_SUBDIR / f"{slug}-reading-room.md"
    hub_changed = _write(hub_path, hub_note(room, rows, stems), dry_run)

    orphans = sorted(p.name for iid, p in known.items()
                     if iid not in {i["id"] for i in room["items"]})
    return {
        "room": slug, "title": room["title"], "items": len(room["items"]),
        "created": written, "updated": updated, "linked_existing": linked,
        "unchanged": len(room["items"]) - linked - written - updated,
        "hub": str(hub_path.relative_to(root)), "hub_changed": hub_changed,
        "orphans": orphans, "dry_run": bool(dry_run),
    }


def ingest_all(dry_run=False):
    return [ingest(r["slug"], dry_run=dry_run) for r in readingroom.list_rooms()]


def summary_line(res):
    return (f"{res['title']}: {res['items']} items — {res['created']} new "
            f"notes, {res['updated']} updated, {res['linked_existing']} "
            f"linked to existing vault notes, {res['unchanged']} unchanged. "
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
