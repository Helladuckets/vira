"""Person pages in the vault, as an index the app can type against.

The vault carries `type: person` pages (201 at build time — cat-wu.md,
dianne-penn.md, every co-founder), each one THE identity of a specific
human: name, role, sources, wikilinks. This module turns them into the
grounding layer for a reading room's people pills — typing "Cat" in the
Details panel offers the Cat Wu who heads Claude Code product, carrying
her page path, not a bare string that could be anyone with that name.

DERIVED, NEVER STORED: the index is rebuilt from the wiki directory and
cached on its mtime (the roomvault.notes_by_item pattern). A renamed or
deleted page falls out on the next scan; nothing here can go stale
against the vault, because nothing here persists.

The one write — `create_stub` — mints a minimal person page for a name
the index cannot resolve, so curating a room grows the people graph as a
side effect. Passive instances refuse it outright: vault_root lives
outside a test clone's data/, so the write would land in the live
Obsidian vault (the plans.py / roomvault precedent).
"""
import os
import re
from datetime import date
from pathlib import Path

from . import vault

WIKI_SUBDIR = "wiki"
QUALIFIER_CAP = 160
_TYPE_RE = re.compile(r"^type:\s*person\s*$", re.M)
_TITLE_RE = re.compile(r'^title:\s*"?([^"\n]+?)"?\s*$', re.M)
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]")
_SLUG_RE = re.compile(r"[^a-z0-9]+")

_cache = {}   # wiki_dir -> (mtime, [entry])


def _strip_wikilinks(text):
    """[[claude-code|Claude Code]] -> Claude Code; [[anthropic]] -> anthropic."""
    return _WIKILINK_RE.sub(lambda m: m.group(2) or m.group(1), text)


def _parse_page(path, root):
    """One page -> an index entry, or None when it is not a person page.

    Only the frontmatter block plus the first prose line are read into the
    entry; the qualifier is what disambiguates ("Head of Product for
    Claude Code at Anthropic"), so it comes from the first body line that
    is neither a heading nor blank."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    fm = text[3:end]
    if not _TYPE_RE.search(fm):
        return None
    m = _TITLE_RE.search(fm)
    name = (m.group(1).strip() if m else "") or path.stem.replace("-", " ").title()
    qualifier = ""
    for line in text[end + 4:].splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        qualifier = _strip_wikilinks(line)[:QUALIFIER_CAP]
        break
    return {"name": name,
            "ref": str(path.relative_to(root).as_posix()),
            "qualifier": qualifier}


def people_index(root=None):
    """Every person page in the vault's wiki, cached on the dir mtime."""
    root = Path(root or vault.vault_root()).expanduser()
    wiki = root / WIKI_SUBDIR
    try:
        stamp = wiki.stat().st_mtime
    except OSError:
        return []
    hit = _cache.get(str(wiki))
    if hit and hit[0] == stamp:
        return hit[1]
    entries = []
    for p in sorted(wiki.glob("*.md")):
        e = _parse_page(p, root)
        if e:
            entries.append(e)
    _cache[str(wiki)] = (stamp, entries)
    return entries


def search(q, limit=8, root=None):
    """Typeahead: prefix-of-a-word matches rank ahead of substring matches,
    so "cat" offers Cat Wu before someone whose page mentions concatenation
    — the match is on NAMES only, never page bodies."""
    q = (q or "").strip().lower()
    if not q:
        return []
    starts, contains = [], []
    for e in people_index(root):
        name = e["name"].lower()
        if any(w.startswith(q) for w in name.split()):
            starts.append(e)
        elif q in name:
            contains.append(e)
    return (starts + contains)[:limit]


def create_stub(name, qualifier="", root=None):
    """Mint a minimal person page for a name the index cannot resolve.

    Refuses an existing page rather than touching it — an existing page is
    the owner's — and refuses under VIRA_PASSIVE (the write lands in the
    real vault). Returns the new entry, index-shaped."""
    if os.environ.get("VIRA_PASSIVE"):
        raise PermissionError(
            "passive instance: person pages write the live vault")
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("name required")
    qualifier = " ".join((qualifier or "").split())[:QUALIFIER_CAP]
    root = Path(root or vault.vault_root()).expanduser()
    wiki = root / WIKI_SUBDIR
    if not wiki.is_dir():
        raise FileNotFoundError("no vault wiki to write into")
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    if not slug:
        raise ValueError(f"cannot slug {name!r}")
    path = wiki / f"{slug}.md"
    if path.exists():
        raise FileExistsError(f"wiki/{slug}.md already exists")
    today = date.today().isoformat()
    body = (f'---\ntitle: "{name}"\ntype: person\ntags:\n  - {slug}\n'
            f"created: {today}\nupdated: {today}\n---\n\n# {name}\n\n"
            + (qualifier + "\n" if qualifier else ""))
    path.write_text(body, encoding="utf-8")
    _cache.pop(str(wiki), None)
    return {"name": name, "ref": f"wiki/{slug}.md", "qualifier": qualifier}
