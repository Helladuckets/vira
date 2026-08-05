"""Tagging documents with the SAME vocabulary the ideas backlog already uses.

The Reader holds 500-odd documents — plans, walkthroughs, retros, dossiers,
briefs — ordered by nothing but date. Ordered by date, a document library is a
pile: the plan that proposed the Reader, the film of the session that built it,
and the retro of the night it shipped sit weeks apart with nothing saying they
are the same story.

They are the same story, and the app already knows the word for it. ideatags
tags every idea on four axes, and `module` is defined as "the part of the
product this touches" — which is exactly "what feature is this, and where does
it go". So this module mints NOTHING. It reads ideatags.AXES for the axes,
ideatags.vocabulary() for the words in use, and ideatags._clean_tags to
validate, so a document and the idea it came from land under the same tag
rather than under `reader` and `reading-room`.

WHY A SEPARATE STORE AND NOT A FEW MORE ROWS IN ideatags: its `_prune` reads
its item list as "everything that exists" and deletes the rest — the documented
2026-07-27 incident where one Similar panel destroyed 134 of 135 entries. Ideas
and documents are two populations; putting them in one sidecar means every
prune has to be right about both forever. Two stores cannot make that mistake.

TWO RUNGS, and rung 1 stands alone:

  1. DETERMINISTIC. A document whose title or slug leaf literally contains a
     module already in use is tagged with it — no model call, no network.
     MEASURED on this corpus: 24 of 519, about 4%. That is a floor, not the
     mechanism, and the number is here because the first draft of this
     docstring guessed "most walkthroughs" and was wrong by an order of
     magnitude. It earns its place by costing nothing and never guessing.
  2. MODEL. One suggest.complete per batch — the mechanism. Handed the live
     vocabulary and told to reuse it. Same discipline as the idea tagger: the
     model proposes, _clean_tags decides.

WHY RUNG 1 IS SO SMALL HERE, since the number invites a fix that would be
worse: 423 of the 519 documents are per-session retros whose titles are
`2026-08-03 2133 vira` — a date, a time, a project. There is no substring in
that to match, and no cleverness recovers a subject that was never written
down. Those documents are tagged from a bounded excerpt of their opening prose
instead (see `excerpt`), which is read during the pass and never on a request.

Derived, therefore regenerable: the store is keyed by document id AND a hash of
what was read, so retitling a document re-tags it on the next pass. Owner
corrections live on the reading-list entry (`tags_add` / `tags_drop`), never
here — a correction written into a field the next pass rewrites is a correction
with a shelf life.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

from . import ideatags, jsonstore, readinglist, suggest
from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "doc-index.json"

AXES = ideatags.AXES
AXIS_IDS = ideatags.AXIS_IDS

BATCH = 12                 # documents per model call, as ideatags uses
VOCAB_SHOWN = 60           # tags per axis handed to the tagger
MAX_BATCHES = 20           # ceiling on one refresh, so a click is bounded

# A module tag has to be a real word to match inside a slug. Two-letter and
# generic tags would match everything ("ui" inside "build"), which is worse
# than leaving a document untagged — a wrong group is read as fact.
_MIN_MATCH = 4
_STOP_MATCH = frozenset(("vira", "test", "new", "session", "walkthrough",
                         "the", "and", "for", "with", "into"))


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _blank():
    return {"entries": {}, "last_pass": ""}


def _read():
    s = jsonstore.read(STORE, _blank())
    if not isinstance(s, dict):
        s = _blank()
    s.setdefault("entries", {})
    if not isinstance(s["entries"], dict):
        s["entries"] = {}
    return s


def _update(fn):
    """Locked read-modify-write. The Indexer runs out of process (the
    ideatags.run_pass precedent) while a request can tag on demand, so an
    unsynchronized cycle would silently lose one side's tags."""
    STORE.parent.mkdir(parents=True, exist_ok=True)
    with locked(STORE):
        s = _read()
        fn(s)
        jsonstore.write_atomic(STORE, s)
    return s


def _slug_words(locator):
    """The words of a locator's FINAL segment only.

    The parent directories are the document's KIND, not its subject:
    `/docs/plans/2026-07-24-layout-templates.html` lives under `plans` because
    it is a plan, and `/walkthroughs/vira-reader-2026-07-27/` under
    `walkthroughs` because it is a film. Feeding those to a substring match
    tags every plan with the module `plans` — 48 false hits on this corpus, all
    of which read as a confident grouping. Take the leaf, drop the shelf."""
    leaf = str(locator or "").strip("/").split("/")[-1]
    leaf = re.sub(r"\.(html?|md|json)$", "", leaf, flags=re.I)
    return " ".join(w for w in re.split(r"[^a-z0-9]+", leaf.lower())
                    if w and not w.isdigit())


def doc_text(it):
    """What the tagger reads: the title, the kind, and the document's own slug.

    Deliberately NOT the document body. A retro is 8KB and a dossier is four
    HTML pages; reading 519 of them would be a file-IO sweep for a signal that
    is almost entirely in the title. The slug's leaf carries the subject, which
    for a walkthrough IS the feature (`vira-reader-2026-07-27`)."""
    return (f"{it.get('kind') or 'document'}: {it.get('title') or ''} "
            f"[{_slug_words(it.get('locator'))}]")


# How much of a document to read when its title says nothing. Enough to carry
# a retro's goal line and its first shipped item; small enough that 423 of them
# is a quick sweep rather than a corpus scan.
EXCERPT_CHARS = 700

# A title with no words in it beyond a date, a time and a project name. The
# per-session retros are all of this shape — `2026-08-03 2133 vira` — and there
# is no tagging them from a title that names only WHEN. Measured: 423 of 519
# documents here are retros and their titles carry zero feature signal.
_DATE_HEAD = re.compile(r"^\s*\d{4}-\d{2}-\d{2}(\s+\d{3,4})?\s*")
_HTML_TAG = re.compile(r"<[^>]+>")
_FRONTMATTER = re.compile(r"^---\s*\n.*?\n---\s*\n", re.S)

# What a datestamped title has left once the date and time come off: a project
# name, and sometimes a bucket word. Anything at or under this is no subject.
_THIN_WORDS = 2


def _is_thin(title):
    """True when a title carries no subject — only a date, a time, a project.

    THE DATE IS THE TEST, not the word count. `2026-08-03 day vira` and
    `Layout templates` both hold two real words, so counting alone calls a
    perfectly good title thin and pays for a file read it does not need. A
    title with no datestamp at the front is a title someone WROTE, however
    short."""
    t = str(title or "")
    stripped = _DATE_HEAD.sub("", t)
    if stripped == t:
        return False                    # nobody stamps a real title with a date
    words = [w for w in re.split(r"[^A-Za-z]+", stripped) if len(w) > 2]
    return len(words) <= _THIN_WORDS


def excerpt(it, limit=EXCERPT_CHARS):
    """The document's opening prose, for titles that carry no subject.

    Read ONLY for thin titles, and only during a tagging pass — never on a
    request. A miss returns '' and the document is tagged from its title alone,
    which is the honest degradation: an unreadable file must not stop the pass.
    """
    try:
        p = readinglist.source_path(it)
        if not p or not p.is_file():
            return ""
        raw = p.read_text(encoding="utf-8", errors="replace")[:limit * 6]
    except (OSError, ValueError):
        return ""
    raw = _FRONTMATTER.sub("", raw)
    if p.suffix.lower() in (".html", ".htm"):
        raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
        raw = _HTML_TAG.sub(" ", raw)
    return " ".join(raw.split())[:limit]


def match_text(it):
    """What rung 1 matches against: title + slug leaf, WITHOUT the kind.

    The kind is real context for the model and pure noise for a substring
    match — a document of kind `brief` would match the module `brief` on every
    daily brief ever written, which says only that a brief is a brief."""
    return f"{it.get('title') or ''} {_slug_words(it.get('locator'))}"


def _hash(text):
    return ideatags._hash(text)


def _live_vocab():
    """The ideas backlog's vocabulary — the words this corpus must converge on.

    Read from ideatags rather than accumulated here, so the two populations
    cannot drift into synonyms. A backlog that has never been tagged returns
    empty and the model is simply told to coin carefully."""
    try:
        return ideatags.vocabulary()
    except Exception:
        return {ax: [] for ax in AXIS_IDS}


def _doc_vocab(s=None):
    """The vocabulary the DOCUMENTS themselves have accumulated, so later
    batches in one run reuse what earlier batches minted."""
    s = s or _read()
    counts = {ax: {} for ax in AXIS_IDS}
    for e in s["entries"].values():
        for ax, vals in (e.get("tags") or {}).items():
            if ax not in counts:
                continue
            for t in vals:
                counts[ax][t] = counts[ax].get(t, 0) + 1
    return {ax: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))
            for ax, c in counts.items()}


def _merged_vocab(s=None):
    """Ideas first, documents second — one ranked list per axis."""
    a, b = _live_vocab(), _doc_vocab(s)
    out = {}
    for ax in AXIS_IDS:
        counts = {}
        for tag, n in (a.get(ax) or []) + (b.get(ax) or []):
            counts[tag] = counts.get(tag, 0) + n
        out[ax] = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return out


# ---------------------------------------------------------------- rung one ---

def guess_module(text, vocab):
    """A module tag whose name appears whole in the document's own words.

    This is the cheap rung and it carries most of this corpus: a walkthrough is
    named after what it changed. Longest match wins, so `media-index` beats
    `media` on a document that says both. Multi-word tags are matched with their
    separator relaxed, because a slug spells `reading-room` and a title spells
    `reading room`."""
    words = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    best = ""
    for tag, _n in (vocab.get("module") or []):
        if len(tag) < _MIN_MATCH or tag in _STOP_MATCH:
            continue
        probe = tag.replace("-", " ")
        if re.search(rf"\b{re.escape(probe)}\b", words) and len(tag) > len(best):
            best = tag
    return best


def _deterministic(it, vocab):
    """{axis: [tag]} from rung 1 alone — module only. The other three axes are
    genuinely judgment (a theme is "what KIND of problem", which no substring
    match can see), so they are left for the model rather than guessed."""
    m = guess_module(match_text(it), vocab)
    return {ax: ([m] if ax == "module" and m else []) for ax in AXIS_IDS}


# ---------------------------------------------------------------- rung two ---

def _prompt(batch, vocab):
    """One tagging call. The vocabulary block is the whole point: tagging each
    document independently yields reader/Reader/reading-room/reader-queue for
    one subject, which groups nothing."""
    lines = []
    for ax in AXES:
        have = ", ".join(t for t, _ in (vocab.get(ax["id"]) or [])[:VOCAB_SHOWN])
        lines.append(f"- {ax['id']} (max {ax['max']}): {ax['hint']}\n"
                     f"  Already in use: {have or '(nothing yet)'}")
    axes_block = "\n".join(lines)
    rows = []
    for i, d in enumerate(batch):
        line = f'{i + 1}. [{d.get("kind")}] {d.get("title")}'
        # A thin title is a date and nothing else, so the opening prose is the
        # only material there is. Read once here, never on a request path.
        if _is_thin(d.get("title")):
            ex = excerpt(d)
            if ex:
                line += f"\n   opens: {ex}"
        rows.append(line)
    docs = "\n".join(rows)
    return (
        "You are tagging DOCUMENTS in a personal software project's library so "
        "they can be grouped by what they are about. Each document is a plan, a "
        "session walkthrough (a film of one build session), a retro, a dossier, "
        "or a daily brief.\n\n"
        "Tag each on these axes:\n" + axes_block + "\n\n"
        "RULES:\n"
        "- REUSE a tag from 'Already in use' whenever one fits. Only coin a new "
        "tag when nothing in the list covers the document. This vocabulary is "
        "shared with the project's idea backlog, so a document about the same "
        "subject as an idea must carry the same tag.\n"
        "- lowercase-kebab-case only.\n"
        "- 'module' is the single most important axis: name the part of the "
        "product the document is about. Leave it empty rather than guessing.\n"
        "- An axis with nothing to say gets an empty list. Do not pad.\n\n"
        "DOCUMENTS:\n" + docs + "\n\n"
        'Reply with ONLY a JSON object: {"1": {"module": ["..."], '
        '"subproject": [], "theme": ["..."], "concept": []}, "2": {...}}'
    )


def _parse(raw, batch):
    """Model output -> {doc_id: {axis: [tag]}}, validated by ideatags."""
    txt = str(raw or "").strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-z]*\s*|\s*```$", "", txt, flags=re.S).strip()
    try:
        obj = json.loads(txt)
    except ValueError:
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            return {}
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            return {}
    if not isinstance(obj, dict):
        return {}
    out = {}
    for key, val in obj.items():
        try:
            idx = int(str(key).strip()) - 1
        except ValueError:
            continue
        if not (0 <= idx < len(batch)) or not isinstance(val, dict):
            continue
        out[batch[idx]["id"]] = ideatags._clean_tags(val)
    return out


def _pending(items, s):
    """Documents with no entry, or whose text has changed since it was made."""
    out = []
    for it in items:
        e = s["entries"].get(it["id"])
        if not e or e.get("hash") != _hash(doc_text(it)):
            out.append(it)
    return out


def tag_pending(items=None, batches=1):
    """Tag up to `batches` batches. Rung 1 first for everything pending (free),
    then rung 2 on what is left over — so a model outage still improves the
    grouping instead of doing nothing."""
    items = _all_docs() if items is None else items
    s = _read()
    todo = _pending(items, s)
    if not todo:
        return {"tagged": 0, "batches": 0, "pending": 0, "rung1": 0}

    vocab = _merged_vocab(s)
    rung1 = {}
    for it in todo:
        d = _deterministic(it, vocab)
        if d.get("module"):
            rung1[it["id"]] = d
    if rung1:
        _write_tags(rung1, items, rung="deterministic")

    done = 0
    used = 0
    for n in range(max(0, int(batches))):
        s = _read()
        left = [it for it in _pending(items, s)]
        if not left:
            break
        batch = left[:BATCH]
        vocab = _merged_vocab(s)
        try:
            raw = suggest.complete(_prompt(batch, vocab))
        except Exception:
            break                       # honest stop: keep what rung 1 wrote
        got = _parse(raw, batch)
        if not got:
            break
        _write_tags(got, items, rung="model")
        done += len(got)
        used += 1
    s = _read()
    return {"tagged": done, "batches": used, "rung1": len(rung1),
            "pending": len(_pending(items, s))}


def _write_tags(tagmap, items, rung=""):
    by_id = {it["id"]: it for it in items}

    def apply(s):
        for did, tags in tagmap.items():
            it = by_id.get(did)
            if not it:
                continue
            s["entries"][did] = {"tags": tags, "hash": _hash(doc_text(it)),
                                 "rung": rung, "tagged": _now()}
        s["last_pass"] = _now()
    _update(apply)


# ---------------------------------------------------------------- read side --

def _all_docs():
    """Every document the Reader tracks — queued AND completed. Grouping a
    library by feature is worthless if it only covers what is unread; 425 of
    the 519 entries here are filed read."""
    return readinglist.queue() + readinglist.completed(limit=10000)


def _overlay(derived, item):
    """Derived tags with the owner's corrections applied. Same shape as
    ideatags._overlay and for the same reason — corrections live on the item,
    which the tagging pass never rewrites."""
    add = item.get("tags_add") or {}
    drop = {ideatags.norm_tag(t) for t in (item.get("tags_drop") or [])}
    out = {}
    for ax in AXIS_IDS:
        vals = list(derived.get(ax) or [])
        for t in (add.get(ax) or []):
            t = ideatags.norm_tag(t)
            if t and t not in vals:
                vals.append(t)
        out[ax] = [t for t in vals if t not in drop]
    return out


def annotate(items):
    """Attach `tags` and `tagged` to reading-list rows. One store read."""
    s = _read()
    out = []
    for it in items:
        e = s["entries"].get(it.get("id")) or {}
        row = dict(it)
        row["tags"] = _overlay(e.get("tags") or {}, it)
        row["tagged"] = bool(e.get("tagged")) or any(row["tags"].values())
        out.append(row)
    return out


def vocabulary(items=None):
    """{axis: [(tag, count)]} over the documents — what the filter offers."""
    items = _all_docs() if items is None else items
    tagged = annotate(items)
    counts = {ax: {} for ax in AXIS_IDS}
    for it in tagged:
        for ax, vals in (it.get("tags") or {}).items():
            if ax not in counts:
                continue
            for t in vals:
                counts[ax][t] = counts[ax].get(t, 0) + 1
    return {ax: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))
            for ax, c in counts.items()}


def status(items=None):
    items = _all_docs() if items is None else items
    s = _read()
    pend = len(_pending(items, s))
    return {"total": len(items), "tagged": len(items) - pend, "pending": pend,
            "last_pass": s.get("last_pass") or "",
            "axes": [{"id": a["id"], "label": a["label"]} for a in AXES]}


def refresh(batches=1):
    return tag_pending(batches=min(int(batches or 1), MAX_BATCHES))


class Indexer(threading.Thread):
    """Tag at most one batch per tick, so a fresh library tags itself over an
    hour or two and nothing ever waits on it. Skipped under VIRA_PASSIVE like
    every worker."""

    def __init__(self, interval_min=None):
        super().__init__(daemon=True)
        self.interval = max(1, int(interval_min or 10)) * 60

    def run(self):
        while True:
            time.sleep(self.interval)
            try:
                if os.environ.get("VIRA_PASSIVE"):
                    continue
                tag_pending(batches=1)
            except Exception:
                continue
