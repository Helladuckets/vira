"""The Inflow — the nightly ingest, as one browsable shelf.

Five routines feed this vault every night: x-bookmarks-poll, youtube-subs-poll,
the Voice Memos export, plaud-ingest, and the Notes-app export. Each lands a
raw capture in `raw/`, and the nightly `tcil-full-ingest` turns it into a
`type: source-summary` note in `wiki/` — which is where the reading actually
is: the takeaways, the extracted charts with their timestamps, the links back
to the original. Nothing surfaced those as a SET. They were reachable one at a
time through Find, and only if you already knew what you were looking for.

THE ITEM IS THE SUMMARY, NOT THE RAW FILE. That is the whole modelling
decision and it is forced by the data: five voice memos from one morning
became ONE summary (`coffee-and-cap-rates-summer-2026`), and a raw with no
summary yet has nothing to read. The summary is the readable unit; its raws
are provenance hanging off it.

DERIVED, NEVER STORED — the atlasvault/walkthroughs discipline. A nightly
routine keeps adding to this corpus, so a stored copy would be stale by
morning. The whole scan is a 1.24s read of 8,239 notes (measured on the real
vault), cached on a fingerprint of (count, newest mtime) that costs 0.03s to
compute, so an unchanged vault answers from memory and an ingest that landed
overnight is picked up on the first read after it.

BUCKETING IS A THREE-RUNG LADDER, and the second rung is not optional. The
obvious implementation reads the `source: "[[raw/...]]"` pointer, and on this
vault that alone finds 626 of the 1,027 items — it misses every YouTube
summary written without a raw pointer, and it misses voice memos entirely,
because the merged one names its recordings only in prose. Tags and the
source_url domain recover the rest. Measured: 626 -> 1,027.

WHAT IS LEFT OUT IS REPORTED, NEVER SILENT. Instagram is 5,454 summaries on
this vault against 1,027 for the five named routines, so including it by
default would bury the shelf in exactly the way ranking buried the retros in
Find. It is a SOURCE THAT IS OFF, not a source that is absent: the chip is
there, the count is stated, one click turns it on.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import settings

ROOT = Path(__file__).resolve().parent.parent
THUMB_DIR = ROOT / "data" / "vault-thumbs"
THUMB_PX = 520

# The done-mark list name. reading.py owns the store, so a read here is the
# same cross-device mark the room pages use — one implementation of "what have
# I read", not a second one keyed differently.
DONE_LIST = "inflow"

# One table drives the bucketing, the chips, the labels and the defaults, so a
# sixth ingest routine is a row rather than an edit in four places.
#   paths  — fragments of the `source:` raw pointer (rung 1)
#   tags   — whole-word tag matches (rung 2)
#   hosts  — source_url domains (rung 3)
SOURCES = [
    {"id": "youtube", "label": "YouTube", "on": True, "glyph": "video",
     "paths": ("/youtube-subs/",), "tags": ("youtube",),
     "hosts": ("youtube.com", "youtu.be")},
    {"id": "x", "label": "X", "on": True, "glyph": "post",
     "paths": ("/x-bookmarks/",), "tags": ("x-bookmark",),
     "hosts": ("x.com", "twitter.com")},
    {"id": "voice-memo", "label": "Voice memos", "on": True, "glyph": "audio",
     "paths": ("/voice-memos/", "voicememo"), "tags": ("voice-memo",),
     "hosts": ()},
    {"id": "plaud", "label": "Plaud", "on": True, "glyph": "audio",
     "paths": ("plaud",), "tags": ("plaud-transcript",), "hosts": ()},
    {"id": "notes", "label": "Notes", "on": True, "glyph": "note",
     "paths": ("/notes-app/",), "tags": ("notes-app",), "hosts": ()},
    # Off by default. Real ingest, wrong scale for this shelf — see the module
    # docstring. The count is always reported so "off" never reads as "none".
    {"id": "instagram", "label": "Instagram", "on": False, "glyph": "image",
     "paths": ("/instagram/",), "tags": ("instagram",),
     "hosts": ("instagram.com",)},
    {"id": "reading-room", "label": "Reading rooms", "on": False,
     "glyph": "read", "paths": ("/reading-room/",), "tags": (), "hosts": ()},
]
BY_ID = {s["id"]: s for s in SOURCES}
DEFAULT_ON = tuple(s["id"] for s in SOURCES if s["on"])

FM_RE = re.compile(r"^---\n(.*?)\n---\n?", re.S)
SUMMARY_RE = re.compile(r"^type:\s*source-summary\s*$", re.M)
# `![[path]]` and `![[path|size]]` — Obsidian's embed. Every one of the 12,002
# in this vault carries a full vault-relative path and every one resolves on
# disk (measured), so the card never needs the stem map to show a picture.
EMBED_RE = re.compile(r"!\[\[([^\]|\n]+?)(?:\|[^\]\n]*)?\]\]")
# A Visuals row: the embed, then a bold label, then prose, then a deep link.
VIS_RE = re.compile(
    r"^\s*[-*]\s*!\[\[([^\]|\n]+?)(?:\|[^\]\n]*)?\]\]\s*(.*)$", re.M)
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
DEEP_RE = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
WIKI_RE = re.compile(r"\[\[([^\]|\n]+?)(?:\|([^\]\n]+))?\]\]")
HEAD_RE = re.compile(r"^#{1,6}\s")
TIME_RE = re.compile(r"^(\d{1,2}:\d{2}(?::\d{2})?)\s*[-\u2014]\s*(.*)$")
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".heic"}

# Tags that say which pipe a note came down rather than what it is about. The
# card already states its source, so repeating it as a topic chip is noise.
PLATFORM_TAGS = {
    "youtube", "x-bookmark", "x-com", "instagram", "notes-app", "voice-memo",
    "plaud-transcript", "note-to-self", "reading-room",
}

_cache = {"key": None, "cards": None, "counts": None, "root": None, "at": 0.0}
_TTL = 4.0        # a burst of requests pays for one fingerprint walk


# ------------------------------------------------------------------ helpers --

def vault_root():
    root = settings.get("vault_root")
    if not root:
        return None
    p = Path(root).expanduser()
    return p if p.exists() else None


def _fm_field(fm, name):
    """One scalar out of the frontmatter, unquoted.

    A DOUBLE-QUOTED value carries YAML escapes and they have to come back
    off: 163 titles in this vault are quotes-within-a-quoted-scalar
    (`title: "\\"Please stay\\" — saved music clip"`), and stripping only the
    outer quotes renders the backslashes on the card."""
    m = re.search(r"^%s:\s*(.+)$" % re.escape(name), fm, re.M)
    if not m:
        return ""
    v = m.group(1).strip()
    if len(v) > 1 and v[0] == '"' and v[-1] == '"':
        return v[1:-1].replace('\\"', '"').replace("\\\\", "\\").strip()
    return v.strip("'").strip('"').strip()


def _fm_tags(fm):
    """Tags in either shape this vault writes: an inline `[a, b]` list or a
    block of `  - a` rows."""
    m = re.search(r"^tags:\s*\[(.*?)\]", fm, re.S | re.M)
    if m:
        raw = m.group(1)
    else:
        m = re.search(r"^tags:\s*\n((?:\s*-\s*.+\n?)+)", fm, re.M)
        raw = re.sub(r"^\s*-\s*", "", m.group(1), flags=re.M) if m else ""
    out = []
    for t in re.split(r"[,\n]", raw):
        t = t.strip().strip('"').strip("'").strip()
        if t and t not in out:
            out.append(t)
    return out


def _plain(s):
    """Prose with the markup that only means something inside a note taken
    out: wikilinks become their label, bold/italic markers go."""
    s = WIKI_RE.sub(lambda m: (m.group(2) or m.group(1).split("/")[-1]), s)
    s = re.sub(r"\*\*|__|`", "", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


def _bucket(pointer, tags, url):
    """Which ingest routine this came down. Ladder order is precedence: the
    raw pointer is a fact, the tag is an assertion, the domain is a guess."""
    tagset = {t.lower() for t in tags}
    for rung in ("paths", "tags", "hosts"):
        for s in SOURCES:
            if rung == "paths" and pointer:
                if any(x in pointer for x in s["paths"]):
                    return s["id"]
            elif rung == "tags":
                if tagset & set(s["tags"]):
                    return s["id"]
            elif rung == "hosts" and url:
                if any(h in url for h in s["hosts"]):
                    return s["id"]
    return "other"


def _blurb(body, cap=340):
    """The first real paragraph — what this note is, in the author's words.

    Skips headings, the Visuals rows, blockquotes (this vault opens several
    summaries on a transcription caveat, which is true and is not what the
    note is about) and bare embeds."""
    for raw in body.split("\n"):
        line = raw.strip()
        if not line or HEAD_RE.match(line) or line.startswith(">"):
            continue
        if line.startswith("!") or line.startswith("|"):
            continue
        if re.match(r"^\s*[-*]\s*!\[\[", raw):
            continue
        text = _plain(line)
        if len(text) < 40:
            continue
        if len(text) <= cap:
            return text
        cut = text[:cap].rsplit(" ", 1)[0]
        return cut.rstrip(",;:.-") + "\u2026"
    return ""


def _visuals(body, root):
    """Every extracted image, with whatever the note says about it.

    A Visuals row carries four things and they are worth keeping apart: the
    embed, a bold `1:29 - Archival reactor core cross-sections` label, the
    prose that reads it, and a `[-> video](...&t=89s)` deep link back to the
    exact second. The timestamp and that link are what turn a contact sheet
    into 'watch from here', which is the decision this shelf exists to serve.
    """
    seen, out = set(), []
    for m in VIS_RE.finditer(body):
        rel, rest = m.group(1).strip(), m.group(2) or ""
        if rel in seen:
            continue
        seen.add(rel)
        out.append(_image(rel, rest, root))
    # Embeds outside the Visuals list still count as pictures — a note that
    # opens on a chart should show it.
    for m in EMBED_RE.finditer(body):
        rel = m.group(1).strip()
        if rel in seen:
            continue
        seen.add(rel)
        out.append(_image(rel, "", root))
    return [i for i in out if i]


def _image(rel, rest, root):
    if Path(rel).suffix.lower() not in IMG_EXT:
        return None
    if not (root / rel).is_file():
        return None
    label = ""
    bold = BOLD_RE.search(rest)
    if bold:
        label = _plain(bold.group(1)).rstrip(".")
        rest = rest[:bold.start()] + rest[bold.end():]
    link = ""
    deep = DEEP_RE.search(rest)
    if deep:
        link = deep.group(1)
        rest = rest[:deep.start()] + rest[deep.end():]
    at = ""
    tm = TIME_RE.match(label)
    if tm:
        at, label = tm.group(1), tm.group(2)
    return {"path": rel, "label": label, "at": at, "link": link,
            "caption": _plain(rest)[:300]}


def _pointers(fm, body):
    """The raw captures behind this summary. Frontmatter first; the merged
    voice-memo summary names its five recordings only in the prose, so the
    body is swept for `raw/` wikilinks too."""
    out = []
    for m in WIKI_RE.finditer(fm + "\n" + body):
        ref = m.group(1).strip()
        if ref.startswith("raw/") and ref not in out:
            out.append(ref)
    return out[:8]


def _card(path, text, root):
    m = FM_RE.match(text)
    if not m or not SUMMARY_RE.search(m.group(1)):
        return None
    fm, body = m.group(1), text[m.end():]
    tags = _fm_tags(fm)
    url = _fm_field(fm, "source_url")
    pointers = _pointers(fm, body)
    src = _bucket(pointers[0] if pointers else _fm_field(fm, "source"),
                  tags, url)
    date = (_fm_field(fm, "created") or _fm_field(fm, "published")
            or _fm_field(fm, "date") or _fm_field(fm, "updated"))[:10]
    images = _visuals(body, root)
    words = len(body.split())
    topics = [t for t in tags
              if t.lower() not in PLATFORM_TAGS and not t.startswith("cat/")]
    rel = "wiki/" + path.name
    return {
        # HASHED, NOT THE STEM. `reading._clean_id` caps a done-key at 64
        # characters, and 102 of this vault's 8,239 note names are longer than
        # that — those marks would have been STORED under a truncated key and
        # read back under the full one, so the item would never show as read
        # and nothing would say why. A digest is stable across rebuilds and
        # cannot collide with the cap.
        "id": "if" + hashlib.sha1(rel.encode("utf-8")).hexdigest()[:14],
        "path": rel,
        "title": _plain(_fm_field(fm, "title")) or path.stem.replace("-", " "),
        "source": src,
        "kind": _fm_field(fm, "source_type"),
        "date": date,
        "url": url,
        "raws": pointers,
        "duration": _fm_field(fm, "duration"),
        "blurb": _blurb(body),
        "images": images,
        "image_count": len(images),
        "topics": topics[:8],
        "links": len(set(WIKI_RE.findall(body))),
        "words": words,
        "minutes": max(1, round(words / 220)),
    }


# --------------------------------------------------------------------- scan --

def _fingerprint(wiki):
    n = 0
    newest = 0
    for p in wiki.glob("*.md"):
        try:
            st = p.stat()
        except OSError:
            continue
        n += 1
        if st.st_mtime_ns > newest:
            newest = st.st_mtime_ns
    return (str(wiki), n, newest)


def scan(force=False):
    """Every source-summary in the vault, as cards. Cached on the fingerprint.

    Returns (cards, counts) where counts is per-source over EVERYTHING found,
    including the sources that are off — that is what lets the UI state what
    it is not showing."""
    root = vault_root()
    if root is None:
        return [], {}
    wiki = root / "wiki"
    if not wiki.is_dir():
        return [], {}
    now = time.monotonic()
    if (not force and _cache["cards"] is not None
            and _cache["root"] == str(root) and now - _cache["at"] < _TTL):
        return _cache["cards"], _cache["counts"]
    key = _fingerprint(wiki)
    _cache["root"], _cache["at"] = str(root), now
    if not force and _cache["key"] == key and _cache["cards"] is not None:
        return _cache["cards"], _cache["counts"]
    cards, counts = [], {}
    for p in sorted(wiki.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        card = _card(p, text, root)
        if not card:
            continue
        cards.append(card)
        counts[card["source"]] = counts.get(card["source"], 0) + 1
    cards.sort(key=lambda c: (c["date"], c["title"]), reverse=True)
    _cache["key"], _cache["cards"], _cache["counts"] = key, cards, counts
    return cards, counts


def feed(sources=None, force=False):
    """The shelf: cards for the selected sources, plus what was left out."""
    cards, counts = scan(force=force)
    want = set(sources) if sources else set(DEFAULT_ON)
    shown = [c for c in cards if c["source"] in want]
    from . import reading
    try:
        done = set(reading.get_done(DONE_LIST))
    except (OSError, ValueError):
        done = set()
    # Copies, never the cached dicts: `scan()` hands back the cache itself, so
    # stamping read-state onto those rows would leak one caller's marks into
    # every later reader of the cache.
    shown = [dict(c, done=c["id"] in done) for c in shown]
    known = {s["id"] for s in SOURCES}
    chips = [{"id": s["id"], "label": s["label"], "glyph": s["glyph"],
              "count": counts.get(s["id"], 0), "on": s["id"] in want}
             for s in SOURCES]
    other = sum(v for k, v in counts.items() if k not in known)
    if other:
        chips.append({"id": "other", "label": "Other captures",
                      "glyph": "note", "count": other,
                      "on": "other" in want})
    return {
        "items": shown,
        "sources": chips,
        "total": len(cards),
        "shown": len(shown),
        "images": sum(c["image_count"] for c in shown),
        "done": sum(1 for c in shown if c["done"]),
        "vault": bool(vault_root()),
    }


def status():
    cards, counts = scan()
    return {"vault": bool(vault_root()), "summaries": len(cards),
            "by_source": counts, "list": DONE_LIST}


# ------------------------------------------------------------------- assets --

def asset_path(rel):
    """A vault-relative path to a real file inside the vault, or None.

    Containment is checked at READ time and against the resolved path, so a
    `..` segment or a symlink pointing out of the vault refuses rather than
    serving whatever it lands on — the _vault_file pattern."""
    root = vault_root()
    if not root or not rel:
        return None
    try:
        p = (root / rel).resolve()
        p.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    if not p.is_file() or p.suffix.lower() == ".md":
        return None
    return p


def thumb_path(rel):
    """A downscaled copy for grid tiles, or the original when we cannot make
    one. The source mtime is IN THE NAME, so an image replaced in place gets a
    fresh thumb instead of a stale cached one (the docthumbs invalidation)."""
    src = asset_path(rel)
    if src is None or src.suffix.lower() not in IMG_EXT:
        return None
    try:
        mt = int(src.stat().st_mtime)
    except OSError:
        return None
    key = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
    out = THUMB_DIR / ("%s-%d.jpg" % (key, mt))
    if out.is_file():
        return out
    sips = shutil.which("sips")
    if not sips:
        return src            # honest fallback: full size beats no picture
    try:
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sips, "-s", "format", "jpeg", "-Z", str(THUMB_PX),
             str(src), "--out", str(out)],
            capture_output=True, timeout=25, check=True)
    except (OSError, subprocess.SubprocessError):
        return src
    if not out.is_file():
        return src
    for old in THUMB_DIR.glob(key + "-*.jpg"):
        if old != out:
            try:
                old.unlink()
            except OSError:
                pass
    return out
