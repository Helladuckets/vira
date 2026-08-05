"""Session walkthroughs — the films of how each feature was built.

A walkthrough is the animated fly-through captured at the end of a build
session: what was asked, what shipped, and the evidence. Thirty-odd of them
accumulated under thedurham.nyc's lab while Vira's own document queue knew
nothing about them — so the app that holds the plan (why), the retro (what
happened) and the dossier (the thinking) was missing the one artifact that
shows what the work actually LOOKED like.

SERVED IN PLACE, NEVER COPIED. Two reasons, and the second is the binding
one:

  - 103MB of MP4s and 2x screenshots would be a second home for content that
    already has one, and the two would drift the moment a film was recaptured.
  - readinglist.py's whole contract is the soft pointer: "It never copies a
    document and never becomes its home." A walkthrough is a document like any
    other; it does not get a special rule.

So main.py mounts <lab_root>/walkthroughs at /walkthroughs/ the way it already
mounts the design-foundation repo at /design — and an install with no lab_root
simply has no walkthroughs, which is the correct state rather than an error.

The site's own prototypes.json is the metadata source where it has a row (it
carries the hand-written name and description that went out with the share
card). A directory with no row still lists, off its slug — the registry is a
publication record, and a film that was never registered is still a film.
"""
import json
import re
from pathlib import Path

from . import settings

# <lab_root>/walkthroughs/<slug>/index.html is the film; motion.mp4 is the
# tile loop; thumb.jpg the still. Anything else in the directory (_src/, the
# capture scripts) is build material and is not served.
SUBDIR = "walkthroughs"

# A slug carries its date, and not always at the end — `vira-2026-07-13-test-badge`
# puts it in the middle. Search, never split.
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")

# Directories that are not films. `_src` holds capture material; a leading
# underscore or dot is the house convention for "not content".
def _is_film_dir(d):
    return d.is_dir() and not d.name.startswith((".", "_")) \
        and (d / "index.html").is_file()


def root(d=None):
    """The walkthroughs directory, or None when no lab_root is configured.

    Dormant by absence — the applications.py lab_root pattern. A missing
    directory is not an error on any install but the owner's.

    `d` is the caller's explicit root and always wins. readinglist passes one
    so its sweep can be rooted at a fixture: a source that silently resolves
    its own path from settings reads the owner's real disk out from under
    every test that thought it had isolated it, which is precisely how that
    module's sitedocs seam broke."""
    if d is not None:
        d = Path(d)
        return d if d.is_dir() else None
    lab = (settings.raw().get("lab_root") or "").strip()
    if not lab:
        return None
    p = Path(lab).expanduser() / SUBDIR
    return p if p.is_dir() else None


def _registry(lab_dir):
    """{slug: {name, description}} from the site's own prototypes.json.

    Best-effort by design: the registry is how the site publishes, not how
    the films exist. A malformed or absent file costs metadata, never rows."""
    out = {}
    try:
        raw = json.loads((lab_dir / "prototypes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    rows = raw if isinstance(raw, list) else next(
        (v for v in raw.values() if isinstance(v, list)), [])
    for r in rows:
        if not isinstance(r, dict):
            continue
        path = str(r.get("path") or "").strip("/")
        if not path.startswith(SUBDIR + "/"):
            continue
        slug = path[len(SUBDIR) + 1:].strip("/")
        share = r.get("share") if isinstance(r.get("share"), dict) else {}
        out[slug] = {
            "name": str(r.get("name") or share.get("name") or "").strip(),
            "description": str(r.get("description")
                               or share.get("description") or "").strip(),
            "added": str(r.get("added") or "").strip(),
            "motion": bool(r.get("motion")),
        }
    return out


def _page_title(d):
    """The film's own <title>, which names its subject far better than a slug.

    Read only when the registry had no name for it — the registry's hand-written
    name is the one that went out with the share card, so it wins."""
    try:
        head = (d / "index.html").read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return ""
    m = _TITLE_RE.search(head)
    return " ".join(_TAG_RE.sub(" ", m.group(1)).split()) if m else ""


def _date_of(slug):
    m = _DATE_RE.search(slug)
    return m.group(1) if m else ""


def project_of(slug):
    """The project a film belongs to — the slug with its date and any trailing
    subject removed. `vira-reader-2026-07-27` -> vira, `qocha-2026-07-20` ->
    qocha. Used as the queue's project facet, so it must match the vocabulary
    the ideas backlog already uses (Vira, qocha, ...)."""
    head = _DATE_RE.split(slug)[0].strip("-")
    if not head:
        return ""
    first = head.split("-")[0]
    return first


def subject_of(slug):
    """What the session was ABOUT, from the slug: the words between the project
    and the date. Empty for a bare `vira-2026-07-13` — that session predates the
    convention of naming the subject, and inventing one would be worse."""
    head = _DATE_RE.split(slug)[0].strip("-")
    parts = [p for p in head.split("-") if p]
    tail = parts[1:]
    if not tail:
        # `vira-2026-07-13-test-badge` — subject sits AFTER the date.
        after = _DATE_RE.split(slug)
        tail = [p for p in after[-1].strip("-").split("-") if p] \
            if len(after) > 2 else []
    return " ".join(tail)


def _title(slug, meta, d):
    """One readable line. Registry name first, then the page's own title, then
    a slug-derived fallback that at least names project and subject."""
    if meta.get("name"):
        return meta["name"]
    page = _page_title(d / slug)
    if page:
        return page
    subj = subject_of(slug)
    proj = project_of(slug).title()
    return f"Walkthrough — {proj} {subj}".strip() if subj else f"Walkthrough — {slug}"


def films(d=None):
    """Every walkthrough on disk, newest first. [] when dormant."""
    d = root(d)
    if not d:
        return []
    reg = _registry(d.parent)
    out = []
    try:
        dirs = [x for x in d.iterdir() if _is_film_dir(x)]
    except OSError:
        return []
    for x in sorted(dirs, key=lambda p: p.name):
        slug = x.name
        meta = reg.get(slug, {})
        out.append({
            "slug": slug,
            "title": _title(slug, meta, d),
            "description": meta.get("description", ""),
            "project": project_of(slug),
            "subject": subject_of(slug),
            "date": _date_of(slug) or meta.get("added", ""),
            "url": f"/{SUBDIR}/{slug}/",
            "motion": (x / "motion.mp4").is_file(),
            "thumb": f"/{SUBDIR}/{slug}/thumb.jpg" if (x / "thumb.jpg").is_file() else "",
            "registered": slug in reg,
        })
    out.sort(key=lambda r: r["date"] or "", reverse=True)
    return out


def rows(d=None):
    """readinglist source rows. `created` is the film's own date, not the
    directory mtime — these were rsynced and captured on different days, and
    mtime would file every one of them as brand new (and then the freshness
    filter would queue thirty-three films at once).

    No `ref`: readinglist stores a 64-char string there, not a record. The
    film's thumb, motion flag and subject are DERIVED — the route layer joins
    them on the locator at read time, so recapturing a film updates the row
    with nothing to re-register."""
    return [{
        "title": f["title"],
        "kind": "walkthrough",
        "locator": f["url"],
        "locator_kind": "url",
        "created": (f["date"] + "T12:00:00") if f["date"] else None,
    } for f in films(d)]
