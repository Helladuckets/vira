"""Application evidence map — role requirements to claim-safe material.

The Applications catalog owns the role and its stable uid.  Application
packages own the current resume, cover letter, answers, and interview prep.
The self-record owns professional claims, and the Evidence Ledger owns the
approved reusable build stories.  This module joins those sources at read
time.  Application-specific gap plans stay in Applications owner state and
are never promoted to evidence or copied into the self-record.

Everything is deterministic and local.  Similarity is deliberately described
as shared evidence language, not as a model's opinion that a requirement is
met.  The owner can therefore inspect every line behind every connection.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from . import evidence, jobcompare, jobdesc, settings

MAX_ARTIFACT_NODES = 44
MAX_SELF_NODES = 72
MAX_TEXT = 520

# The ONE table naming each lane's artifact files.  build() and the narrative
# reader consume it, and a DROPPED file is accepted through it as well
# (attach_material), so "what this lane is made of" and "what this lane will
# take" cannot drift apart.  Each entry is (root_globs, version_names): the
# .docx the package keeps at its top level, and the plain-text copy inside
# V<N>/ — which is the half a drop can write.
# The resume ships in two forms: the two-page record and a one-page companion
# distilled from it, told apart by a `_1p` suffix on an otherwise identical
# name. ONE rule, here, because two readers consume it — this lane and
# resumeview — and a second copy is a second chance for them to disagree about
# which file "the resume" means.
#
# The two-pager is the primary everywhere by EXCLUSION, never by sort order.
# `2026-08-14_cv_role.docx` does happen to sort before `..._role_1p.docx`
# ("." is 46, "_" is 95), and depending on that is exactly the accident the
# routinesrc backup names were bitten by.
#
# It is load-bearing beyond tidiness because `_read_artifact` takes the first
# candidate that yields NODES, not the first that exists: an empty or
# unreadable two-pager would fall straight through to the companion, and the
# Map would then map the distillation while reporting on the record. That is a
# silent substitution of a SUBSET for the whole, which is the one failure this
# lane cannot notice on its own.
ONE_PAGE_MARK = "_1p"
ONE_PAGE_RE = re.compile(r"_1p(?=\.[^.]+$)", re.I)


def is_one_pager(path):
    """True for the one-page companion resume."""
    return bool(ONE_PAGE_RE.search(Path(path).name))


LANE_ARTIFACTS = {
    "resume": ((("*_cv_*.docx",), ("*_cv_*.md",)),),
    "cover": ((("cover-letter.docx",),
               ("cover-letter.md", "cover-letter.txt")),),
    "narrative": ((("interview-prep.docx",), ("interview-prep.md",)),
                  (("answers.docx",), ("answers.md", "answers.txt"))),
}
LANE_TITLES = {"resume": "resume", "cover": "cover letter",
               "narrative": "narratives"}
# A drop is a document, never a binary.  .docx is deliberately excluded: the
# root .docx is what the owner edits by hand and what resumeview renders, so
# replacing it from a drag gesture is a different and much larger decision.
DROP_SUFFIXES = (".md", ".markdown", ".txt")
MAX_DROP_BYTES = 400_000

WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?", re.I)
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")
BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+?)\s*$")
TABLE_RULE_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$")
VERSION_RE = re.compile(r"^V(\d+)$", re.I)
URL_RE = re.compile(r"https?://[^\s<>`|)\]]+", re.I)

STOP = frozenset("""
 a about above after again against all also am an and any are as at be because
 been before being below between both but by can could did do does doing down
 during each few for from further had has have having he her here hers herself
 him himself his how i if in into is it its itself just me more most my myself
 no nor not of off on once only or other our ours ourselves out over own same
 she should so some such than that the their theirs them themselves then there
 these they this those through to too under until up very was we were what when
 where which while who whom why will with would you your yours yourself
 role work working team teams ability able experience years strong excellent
 including across ensure responsible responsibilities required preferred
 """.split())

# Concept families make connections robust to ordinary wording changes while
# staying inspectable: every added family name is returned as a signal.
SEMANTIC = {
    "program delivery": ("program", "launch", "deliver", "ship", "rollout",
                         "execution", "end-to-end"),
    "coordination": ("coordinate", "orchestrate", "align", "stakeholder",
                     "cross-functional", "partnership"),
    "dependencies": ("dependency", "dependencies", "unblock", "sequence",
                     "workstream", "parallel"),
    "judgment": ("tradeoff", "tradeoffs", "risk", "risks", "scope",
                 "quality", "pressure", "decision"),
    "technical fluency": ("technical", "engineering", "engineer", "research",
                          "researcher", "ml", "ai", "model", "infrastructure"),
    "communication": ("communicate", "communication", "translate", "written",
                      "verbal", "influence", "documentation", "status"),
    "systems and process": ("process", "playbook", "framework", "operating",
                            "system", "systems", "scale", "repeatable"),
    "trust": ("trust", "relationship", "relationships", "engagement"),
    "ambiguity": ("ambiguous", "ambiguity", "complex", "structure", "shift",
                  "pace", "priorities"),
}

SKIP_JOB_HEADINGS = (
    "live application", "application form", "equal opportunity", "privacy",
    "accommodation",
)
JOB_CONTENT_HEADINGS = (
    "about", "overview", "responsib", "you may be a good fit", "good-fit",
    "good fit", "qualification", "requirement", "what you'll do",
    "what you will do", "who you are", "logistics", "benefit",
    "compensation", "salary",
)
SKIP_SELF_HEADINGS = (
    "private", "privacy", "public-materials constraints", "open reconciliation",
    "references", "alternate identities", "contact and identity",
    "do-not-use", "red herring",
)
# Master History's endnotes are the claim gate; its body is selection
# context. The two details below are the signal every downstream label reads.
GATE_DETAIL = "permitted wording; the claim gate"
CONTEXT_DETAIL = ("selection context; outward wording requires the governing "
                  "endnote")
SAFE_SELF_KEYS = (
    "identity", "positioning", "adjudicated_stories", "campaign_strategy",
    "employment", "education", "credentials", "resume_vehicle_decoder",
)


def packages_root() -> Path:
    """Application package home, configurable and backward-compatible."""
    override = settings.raw().get("applications_packages_root")
    if override:
        return Path(str(override)).expanduser()
    # The self-record's CLAUDE.md places application packages OUTSIDE the
    # record ("do not recreate an applications folder here"), so this is the
    # only home. It is returned even when absent so the caller creates it in
    # the right place rather than seeding a forbidden in-record fallback.
    return Path.home() / "Documents" / "CV" / "15-applications"


def _clean(value, cap=MAX_TEXT):
    text = re.sub(r"\s+", " ", str(value or "")).strip(" \t-|•")
    if len(text) > cap:
        text = text[:cap - 3].rsplit(" ", 1)[0] + "..."
    return text


def _slug(value):
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")


def _node(lane, text, heading="", source="", kind="section", order=0,
          detail=""):
    text = _clean(text)
    key = "\0".join((lane, source, str(order), text))
    return {
        "id": f"{lane}-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10],
        "lane": lane,
        "text": text,
        "heading": _clean(heading, 100),
        "source": _clean(source, 140),
        "kind": kind,
        "detail": _clean(detail, 180),
    }


def _requirement_key(heading, text, occurrence=0):
    value = "\0".join((_slug(heading), _slug(text), str(occurrence)))
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def _canonical_job_heading(role, node):
    """Restore Anthropic's public section names across package vintages."""
    source = node.get("heading") or ""
    if _slug(role.get("company")) != "anthropic":
        return
    lower = source.casefold()
    canonical = ""
    if "responsib" in lower:
        canonical = "Responsibilities"
    elif "good-fit" in lower or "good fit" in lower:
        canonical = "You may be a good fit if you"
    if canonical and canonical != source:
        node["source_heading"] = source
        node["heading"] = canonical
        node["detail"] = f"Source section: {source}"


def _markdown_units(text, lane, source, min_words=4, bullet_min=3):
    """Markdown/txt into heading-aware, line-sized evidence nodes."""
    out, heading, paragraph = [], "", []

    def flush():
        if paragraph:
            value = _clean(" ".join(paragraph))
            if len(WORD_RE.findall(value)) >= min_words:
                out.append(_node(lane, value, heading, source, "section",
                                 len(out)))
            paragraph.clear()

    for raw in str(text or "").replace("\r", "").split("\n"):
        line = raw.strip()
        hm = HEADING_RE.match(line)
        if hm:
            flush()
            heading = _clean(hm.group(1), 100)
            continue
        if not line:
            flush()
            continue
        bm = BULLET_RE.match(line)
        if bm:
            flush()
            value = _clean(bm.group(1))
            if len(WORD_RE.findall(value)) >= bullet_min:
                out.append(_node(lane, value, heading, source, "bullet",
                                 len(out)))
            continue
        if TABLE_RULE_RE.match(line):
            continue
        if line.startswith("|") and line.endswith("|"):
            flush()
            cells = [_clean(c) for c in line.strip("|").split("|")]
            value = " — ".join(c for c in cells if c)
            if len(WORD_RE.findall(value)) >= min_words:
                out.append(_node(lane, value, heading, source, "row", len(out)))
            continue
        paragraph.append(line)
    flush()
    return out


def _docx_markdown(path):
    """Extract paragraphs and table rows from a docx with only stdlib.

    Root docx files are the package's live editing surface.  Reading their
    XML directly avoids making python-docx a runtime dependency.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml")
    except (OSError, KeyError, zipfile.BadZipFile):
        return ""
    root = ET.fromstring(xml)
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    lines = []
    body = root.find(f"{ns}body")
    if body is None:
        return ""
    def paragraph_line(paragraph):
        value = "".join(t.text or "" for t in
                        paragraph.iter(f"{ns}t")).strip()
        if not value:
            return ""
        style = paragraph.find(f"./{ns}pPr/{ns}pStyle")
        style_name = style.get(f"{ns}val", "") if style is not None else ""
        if style_name.casefold().startswith("heading"):
            return "## " + value
        num = paragraph.find(f"./{ns}pPr/{ns}numPr")
        return ("- " if num is not None else "") + value

    for child in body:
        if child.tag == f"{ns}p":
            lines.append(paragraph_line(child))
            lines.append("")
        elif child.tag == f"{ns}tbl":
            for row in child.findall(f"./{ns}tr"):
                cells = []
                for cell in row.findall(f"./{ns}tc"):
                    paragraphs = [paragraph_line(p)
                                  for p in cell.findall(f".//{ns}p")]
                    cells.append([p for p in paragraphs if p])
                # Layout tables (one cell, or several paragraphs in a cell)
                # are document structure, not a semantic data row. Preserve
                # their paragraphs separately so a whole cover letter never
                # collapses into one giant map card.
                if len(cells) == 1 or any(len(c) > 1 for c in cells):
                    for cell in cells:
                        for value in cell:
                            lines.extend((value, ""))
                else:
                    values = [c[0] if c else "" for c in cells]
                    if any(values):
                        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def _posting_fields(path):
    """(fields, text, ok) — `ok` False ONLY when the file could not be read.

    The flag is the whole point: an unreadable posting and an empty one
    produce identical fields, and the index must never treat the first as
    evidence that a package says nothing (see _package_rows).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}, "", False
    fields = {}
    for line in text.splitlines()[:30]:
        if ":" not in line or line.startswith("#"):
            continue
        key, value = line.split(":", 1)
        fields[_slug(key)] = value.strip()
    return fields, text, True


def _latest_version(package):
    versions = []
    try:
        children = list(package.iterdir())
    except OSError:
        children = []
    for child in children:
        match = VERSION_RE.match(child.name)
        if child.is_dir() and match:
            versions.append((int(match.group(1)), child))
    return max(versions, default=(0, package))[1]


# --------------------------------------------------------- package index
#
# One walk over the packages root, cached on the postings' own mtimes,
# answering both questions asked of it: "which package is THIS role's"
# (find_package, one role at a time, behind Map and Read) and
# "which roles have one at all" (written_for, the whole catalog on every
# list render).  They share the rows AND the scoring, so the Read button
# and the Written filter cannot disagree about whether a role is written.
_PKG_CACHE: dict = {"key": None, "rows": []}

# role_uid mints lowercase alphanumerics joined by hyphens, so every token
# of that shape in a posting is a candidate uid.  Extracting them once at
# index time is what makes the catalog-wide pass affordable: searching each
# posting's text per role is thousands of regex passes over 20KB each.
UID_TOKEN_RE = re.compile(
    r"(?<![a-z0-9-])[a-z0-9]+(?:-[a-z0-9]+)+(?![a-z0-9-])", re.I)

# A superseded package moved under `archive/` is still a written package —
# it answers for a role that has no live successor — but it must never
# outrank one.  Matched on a path COMPONENT, never a substring: a company
# named "archive-labs" gets a flat folder at the root and is not an
# archive.
ARCHIVE_DIRS = {"archive"}


def _is_archived(path, root):
    try:
        parts = path.relative_to(root).parts[:-2]  # drop V<N>/posting.md
    except ValueError:
        return False
    return any(p.casefold() in ARCHIVE_DIRS for p in parts)


def _package_rows():
    """Every versioned posting.md under the packages root, indexed.

    A FAILED READ IS NEVER CACHED AS A FACT.  An Apply session writes and
    moves these files while the module is being read, so a posting the walk
    just listed can be unreadable an instant later — and the mtime it
    settles on is the very key such a pass would file its blank answer
    under, so the damage STICKS until the file is edited again.  Measured
    2026-08-13: one pass mid-package-write reported every role in the
    catalog as unwritten, which is what the Written filter renders as an
    empty list, with nothing anywhere saying a read had failed.

    So: a posting that cannot be read keeps its LAST KNOWN row (a package
    being rewritten is still a written package), a posting that fails
    before it was ever read contributes NO row rather than a blank one
    that can never match, and any degraded pass returns without caching so
    the next call retries.  An absent root is not a failure — that is an
    install with no packages yet, and an empty answer is the true one.
    """
    from . import applications
    root = packages_root()
    stats, degraded = [], False
    if root.is_dir():
        for path in root.rglob("posting.md"):
            if not VERSION_RE.match(path.parent.name):
                continue
            try:
                stats.append((path, path.stat().st_mtime))
            except OSError:
                # A file that vanishes between the walk and the stat drops
                # out of the KEY too, so this pass self-heals on the next
                # call either way; not caching it is simply the cheaper
                # side of the trade.  The read below is the case that does
                # NOT self-heal, and that is what the tests pin.
                degraded = True
    # The whole (path, mtime) set, not just the newest: an edit in place, a
    # deletion and an addition all have to invalidate, and 17 tuples is
    # cheaper than one wrong answer about which package a role has.
    key = (str(root), tuple(sorted((str(p), m) for p, m in stats)))
    if _PKG_CACHE["key"] == key and not degraded:
        return _PKG_CACHE["rows"]
    known = {row["path"]: row for row in _PKG_CACHE["rows"]}
    rows = []
    for path, mtime in stats:
        fields, text, ok = _posting_fields(path)
        if not ok:
            degraded = True
            prior = known.get(str(path))
            if prior is not None:
                rows.append(prior)
            continue
        urls = [fields.get("posting-url") or fields.get("apply-url") or ""]
        urls.extend(URL_RE.findall(text))
        uids = {applications.role_uid({"url": url}) for url in urls if url}
        uids |= {token.lower() for token in UID_TOKEN_RE.findall(text)}
        lines = text.splitlines()
        rows.append({
            "path": str(path),
            "package": path.parent.parent,
            "version": int(VERSION_RE.match(path.parent.name).group(1)),
            "archived": _is_archived(path, root),
            "mtime": mtime,
            "uids": {u for u in uids if u},
            "company": _slug(fields.get("company")),
            "title": _slug(lines[0].lstrip("# ") if lines else ""),
        })
    if degraded:
        return rows
    _PKG_CACHE["key"], _PKG_CACHE["rows"] = key, rows
    return rows


def _match_score(row, uid, company, title):
    """The ONE definition of "this package belongs to this role".

    A posting that names the uid — in a field, an apply URL, or its own
    prose — is that role's beyond doubt.  Company plus title is the
    fallback for a package written before uids were recorded; both sides
    must be non-empty, or two postings missing a company field would claim
    each other's roles.
    """
    if uid and uid in row["uids"]:
        return 100
    if not (company and title) or company != row["company"]:
        return 0
    if title == row["title"]:
        return 70
    if row["title"] and (title in row["title"] or row["title"] in title):
        return 45
    return 0


def match_package(role, rows=None):
    """Best package for one role, or None. Deterministic, no model.

    RANKED (score, live-before-archived, newest, version).  Version is a
    WITHIN-package round counter, and ranking on it ACROSS packages was a
    real defect: measured 2026-08-14, the active
    `anthropic/staff-software-engineer-claude-code-2026-08-13` (V1, and
    the newer file by mtime) lost role a-5383610008 to its own archived
    predecessor, which had reached V3 — so the Map, the Read pane and the
    WRITTEN chip all served the superseded August 7 package.  Both
    postings name the uid, so both scored 100 and the tiebreak decided
    it.  Two active packages with V1-only predecessors resolved correctly,
    which is exactly why it read as fine.

    Archive still ANSWERS, it just never outranks: eight roles have no
    live package at all, and their archived one is the honest record that
    the application was written.  Version stays as the last tiebreak,
    where it can no longer reach across packages ahead of recency.
    """
    rows = _package_rows() if rows is None else rows
    uid = (role.get("uid") or "").casefold()
    company = _slug(role.get("company"))
    title = _slug(role.get("title"))
    best = (0, 0, 0.0, 0, None)
    for row in rows:
        score = _match_score(row, uid, company, title)
        if score:
            best = max(best, (score, 0 if row.get("archived") else 1,
                              row["mtime"], row["version"],
                              row["package"]))
    return best[4]


def find_package(role):
    """Resolve a package by posting uid, then exact company/title fallback."""
    return match_package(role)


def written_for(roles):
    """{uid: package folder name} for every role whose package is on disk.

    DERIVED on every read, never stored.  The on-disk package is the only
    signal that stays true: Vira's own `last_job` stamp records a DISPATCH,
    which is a different fact — it survives a session that failed, and it
    misses every package the owner had written from a copy-out session Vira
    never launched (measured 2026-08-13: 13 roles have a package on disk
    against 10 carrying a stamp, so a stamp-based filter would have hidden
    three of the applications it exists to surface).
    """
    rows = _package_rows()
    if not rows:
        return {}
    out = {}
    for role in roles:
        package = match_package(role, rows)
        if package is not None:
            out[role["uid"]] = package.name
    return out


def _read_artifact(package, lane, root_globs, version_names):
    if package is None:
        return [], None
    candidates = []
    for pattern in root_globs:
        candidates.extend(sorted(package.glob(pattern)))
    version = _latest_version(package)
    for name in version_names:
        candidates.extend(sorted(version.glob(name)))
    if lane == "resume":
        candidates = [p for p in candidates if not is_one_pager(p)]
    for path in candidates:
        if not path.is_file():
            continue
        if path.suffix.casefold() == ".docx":
            text = _docx_markdown(path)
        else:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
        nodes = _markdown_units(text, lane, path.name)
        if nodes:
            return nodes, path
    return [], None


def _lane_artifact(package, lane):
    """Every artifact this lane reads, through LANE_ARTIFACTS. (nodes, path)."""
    nodes, first = [], None
    for root_globs, version_names in LANE_ARTIFACTS.get(lane, ()):
        found, path = _read_artifact(package, lane, root_globs, version_names)
        nodes.extend(found)
        if path is not None and first is None:
            first = path
    return nodes, first


def _job_concepts_from_text(role, text, source):
    """Extract every substantive logical line from one JD representation."""
    # Short requirements count too ("Move fast.", "Be curious.").  The
    # stricter word floors used for artifact evidence would silently erase
    # exactly the terse JD lines this lane promises to preserve.
    units = _markdown_units(text, "job", source, min_words=1, bullet_min=1)
    focused = []
    structured = any(any(term in n["heading"].casefold()
                         for term in JOB_CONTENT_HEADINGS) for n in units)
    started = not structured
    for node in units:
        heading = node["heading"].casefold()
        if any(term in heading for term in JOB_CONTENT_HEADINGS):
            started = True
        if any(skip in heading for skip in SKIP_JOB_HEADINGS):
            continue
        if not started:
            continue
        # posting.md begins with a small metadata block.  It locates the
        # package but is not part of the job description.  Everything else
        # stays in source order: bullets, prose paragraphs, compensation, and
        # benefits.  Section headings remain attached to their lines.
        lower = node["text"].casefold()
        if ("posting url:" in lower or "apply url:" in lower or
                lower.startswith("company:") or lower.startswith("saved:")):
            continue
        focused.append(node)
    # Do not deduplicate or cap the source.  Repeated requirements are still
    # lines the employer chose to repeat, and the map's contract is lossless.
    out, occurrences = [], {}
    for node in focused:
        if not _slug(node["text"]):
            continue
        if (_slug(role.get("company")) == "anthropic" and
                ("good fit" in node["heading"].casefold() or
                 "good-fit" in node["heading"].casefold()) and
                node["text"].casefold().startswith((
                    "the annual compensation", "for sales roles",
                    "annual salary:"))):
            node["source_heading"] = node["heading"]
            node["heading"] = "Compensation"
            node["detail"] = "Pay-transparency block"
        _canonical_job_heading(role, node)
        node["kind"] = "concept"
        signature = (_slug(node["heading"]), _slug(node["text"]))
        occurrence = occurrences.get(signature, 0)
        occurrences[signature] = occurrence + 1
        node["concept_key"] = _requirement_key(
            node["heading"], node["text"], occurrence)
        out.append(node)
    return out


def _job_concepts(role, package):
    """Use the richest local JD source; package snapshots never hide lines."""
    candidates = []
    if package is not None:
        posting = _latest_version(package) / "posting.md"
        if posting.is_file():
            _fields, text, _ok = _posting_fields(posting)
            nodes = _job_concepts_from_text(role, text, "saved posting.md")
            if nodes:
                candidates.append((len(nodes), 1, nodes))
    raw = role.get("jd") or role.get("blurb") or ""
    if raw:
        text = jobdesc.to_markdown(raw)
        nodes = _job_concepts_from_text(role, text, "catalog job description")
        if nodes:
            candidates.append((len(nodes), 0, nodes))
    if candidates:
        # More preserved lines wins.  On an exact tie the saved package wins
        # because it is the captured application-time snapshot.
        return max(candidates, key=lambda item: (item[0], item[1]))[2]
    raw = role.get("jd") or role.get("blurb") or ""
    fallback = []
    for index, unit in enumerate(jobcompare._units(raw)):
        node = _node("job", unit, "Role concepts", "saved job description",
                     "concept", index)
        node["concept_key"] = _requirement_key(node["heading"], node["text"])
        fallback.append(node)
    return fallback


def _flatten_json(value, prefix=""):
    out = []
    if isinstance(value, dict):
        for key, child in value.items():
            out.extend(_flatten_json(child, f"{prefix} / {key}".strip(" /")))
    elif isinstance(value, list):
        for child in value:
            out.extend(_flatten_json(child, prefix))
    elif isinstance(value, (str, int, float)) and _clean(value):
        out.append((prefix, _clean(value)))
    return out


def _self_nodes():
    from . import applications
    root = applications.self_record()
    out = []
    # Master History is the single canonical record. Its endnotes are the
    # claim gate: each carries the approved outward wording and the limits
    # for the sentence it governs, so they are emitted first and equally
    # relevant gate language wins a tie against narrative body text. Anything
    # selected from the body still needs the governing endnote checked before
    # it can move into an outward artifact. (Before 2026-08-11 the gate was a
    # separate FACTS.md; the fold made the two one file.)
    history = root / "canon" / "MASTER_HISTORY.md"
    if history.is_file():
        try:
            text = history.read_text(encoding="utf-8")
        except OSError:
            text = ""
        split = text.find("\n# Endnotes")
        body, endnotes = (text, "") if split < 0 else (text[:split],
                                                       text[split:])
        for chunk, detail in ((endnotes, GATE_DETAIL),
                              (body, CONTEXT_DETAIL)):
            candidates = _markdown_units(chunk, "self", "MASTER_HISTORY.md")
            for node in candidates:
                node["detail"] = detail
            out.extend(n for n in candidates if
                       not n["heading"].casefold().startswith("master history")
                       and not any(skip in n["heading"].casefold()
                                   for skip in SKIP_SELF_HEADINGS))
    distilled = root / "canon" / "self.json"
    try:
        data = json.loads(distilled.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    for key in SAFE_SELF_KEYS:
        for heading, text in _flatten_json(data.get(key), key):
            if len(WORD_RE.findall(text)) >= 3:
                out.append(_node("self", text, heading, "self.json", "fact",
                                 len(out), "machine-readable distillation"))
    return out


def _approved_story_nodes():
    """Approved Evidence Ledger stories, without generated package prose."""
    nodes = []
    try:
        cases = [c for c in evidence.list_cases()
                 if c.get("status") == "approved"]
    except Exception:  # noqa: BLE001 -- map stays usable without the ledger
        cases = []
    for case in cases:
        text = " ".join(filter(None, (
            case.get("problem"), case.get("direction"), case.get("outcome"))))
        nodes.append(_node("narrative", text, case.get("title") or "Case study",
                           "Evidence Ledger", "approved story", len(nodes),
                           ", ".join(case.get("skills") or [])))
    return nodes


def _story_nodes(package):
    nodes, _path = _lane_artifact(package, "narrative")
    nodes.extend(_approved_story_nodes())
    return nodes


def prompt_plan(role):
    """Compact pre-draft plan shared by Map and the Apply dispatch.

    The plan deliberately excludes the current resume, cover letter, and
    generated interview prose: those are outputs to revise, never claim
    sources. Candidate anchors come only from the full career/self record and
    approved Evidence Ledger stories. Similarity proposes where to inspect;
    it never declares a requirement satisfied.
    """
    package = find_package(role)
    concepts = _job_concepts(role, package)
    anchors = _self_nodes() + _approved_story_nodes()
    from . import applications
    notes = applications.map_notes(role.get("uid") or "")
    notes_by_key = {}
    for note in notes:
        notes_by_key.setdefault(note["concept_key"], []).append({
            "lane": note["lane"], "text": note["text"]})

    requirements = []
    for concept in concepts:
        ranked = []
        for anchor in anchors:
            score, signals = similarity(concept["text"], anchor["text"])
            if score >= 10:
                ranked.append((score, anchor, signals))
        candidates = []
        has_positive_candidate = False
        for score, anchor, signals in sorted(
                ranked, key=lambda item: item[0], reverse=True)[:3]:
            negative = bool(re.search(
                r"\b(?:did not|does not|do not|never|no experience|not worked|"
                r"cannot|can't|without)\b", anchor["text"], re.I))
            if score >= 18 and not negative:
                has_positive_candidate = True
            candidates.append({
                "source": anchor["source"],
                "heading": anchor["heading"],
                "text": _clean(anchor["text"], 280),
                "signal": score,
                "shared_language": signals,
                "interpretation": ("boundary or negative evidence; do not use "
                                   "as support" if negative else
                                   "candidate bridge to inspect"),
                "authority": ("outward-ready only in this adjudicated form"
                              if anchor.get("detail") == GATE_DETAIL else
                              "selection context; verify the permitted "
                              "wording in the governing endnote"),
            })
        requirements.append({
            "key": concept["concept_key"],
            "section": concept["heading"] or "Job description",
            "requirement": concept["text"],
            "candidate_anchors": candidates,
            "owner_notes": notes_by_key.get(concept["concept_key"], []),
            "starting_status": ("candidate evidence to adjudicate"
                                if has_positive_candidate else "gap"),
        })
    return {
        "method": ("Candidate anchors are shared-language discovery hints, "
                   "not proof of fit. Final classification must be DIRECT, "
                   "TRANSFERABLE ANALOGUE, NEEDS ADJUDICATION, or GAP."),
        "requirements": requirements,
    }


def _stem(token):
    for suffix in ("ments", "ation", "ings", "ies", "ing", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[:-len(suffix)] + ("y" if suffix == "ies" else "")
    return token


def _tokens(text):
    return {_stem(m.group(0).casefold()) for m in WORD_RE.finditer(text)
            if m.group(0).casefold() not in STOP and len(m.group(0)) > 2}


def _families(tokens):
    return {name for name, words in SEMANTIC.items()
            if any(_stem(word) in tokens for word in words)}


def similarity(left, right):
    """Return an inspectable 0-100 shared-evidence-language score."""
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0, []
    common = a & b
    lexical = len(common) / math.sqrt(len(a) * len(b))
    fa, fb = _families(a), _families(b)
    semantic = len(fa & fb) / max(1, len(fa | fb))
    score = min(99, round(100 * (0.72 * lexical + 0.28 * semantic)))
    signals = sorted(common)[:5] + sorted(fa & fb)[:3]
    return score, signals


def _relevant(nodes, concepts, limit):
    ranked = []
    for order, node in enumerate(nodes):
        best = max((similarity(c["text"], node["text"])[0]
                    for c in concepts), default=0)
        ranked.append((best, -order, node))
    picked = [node for score, _order, node in sorted(ranked, reverse=True,
                                                     key=lambda x: (x[0], x[1]))
              if score >= 8][:limit]
    # Keep original document order inside each lane; relevance decides only
    # which lines make the readable map.
    wanted = {n["id"] for n in picked}
    return [n for n in nodes if n["id"] in wanted]


def _edges(concepts, lanes):
    edges = []
    for concept in concepts:
        for lane, nodes in lanes.items():
            scored = []
            for node in nodes:
                if node.get("planning"):
                    continue
                score, signals = similarity(concept["text"], node["text"])
                if score >= 8:
                    scored.append((score, node, signals))
            for score, node, signals in sorted(scored, key=lambda x: x[0],
                                                reverse=True)[:3]:
                edges.append({
                    "from": concept["id"], "to": node["id"], "lane": lane,
                    "score": score,
                    "strength": "strong" if score >= 38 else
                                "direct" if score >= 24 else "adjacent",
                    "signals": signals,
                })
    return edges


def _planning_nodes(role, concepts):
    """Return owner planning notes plus their explicit concept edges."""
    from . import applications
    by_key = {c["concept_key"]: c for c in concepts}
    lanes = {lane: [] for lane in applications.MAP_NOTE_LANES}
    edges = []
    for note in applications.map_notes(role.get("uid") or ""):
        concept = by_key.get(note["concept_key"])
        if concept is None:
            continue  # stale note survives state, but cannot attach falsely
        lane = note["lane"]
        node = _node(lane, note["text"], concept["heading"],
                     "Application map note", "planning note",
                     len(lanes[lane]),
                     "Drafting instruction; verify against the Master "
                     "History claim gate")
        node["id"] = f"{lane}-plan-{note['concept_key']}"
        node["planning"] = True
        node["concept_key"] = note["concept_key"]
        lanes[lane].append(node)
        edges.append({
            "from": concept["id"], "to": node["id"], "lane": lane,
            "score": 100, "strength": "manual", "signals": ["owner note"],
            "planning": True,
        })
    return lanes, edges


def build(role):
    """Compose the map from source material plus explicit planning notes."""
    package = find_package(role)
    concepts = _job_concepts(role, package)
    resume, resume_path = _lane_artifact(package, "resume")
    cover, cover_path = _lane_artifact(package, "cover")
    narratives = _story_nodes(package)
    self_nodes = _self_nodes()

    lanes = {
        "resume": _relevant(resume, concepts, MAX_ARTIFACT_NODES),
        "cover": _relevant(cover, concepts, MAX_ARTIFACT_NODES),
        "narrative": _relevant(narratives, concepts, MAX_ARTIFACT_NODES),
        "self": _relevant(self_nodes, concepts, MAX_SELF_NODES),
    }
    planning_lanes, planning_edges = _planning_nodes(role, concepts)
    for lane, nodes in planning_lanes.items():
        lanes[lane].extend(nodes)
    edges = _edges(concepts, lanes) + planning_edges
    by_concept = {c["id"]: [] for c in concepts}
    for edge in edges:
        by_concept[edge["from"]].append(edge)
    for concept in concepts:
        linked = by_concept[concept["id"]]
        evidence_edges = [e for e in linked if not e.get("planning")]
        outward = [e for e in evidence_edges if e["lane"] != "self"]
        grounded = [e for e in evidence_edges if e["lane"] == "self"]
        concept["coverage"] = {
            "outward": max((e["score"] for e in outward), default=0),
            "grounded": max((e["score"] for e in grounded), default=0),
            "planned": sum(1 for e in linked if e.get("planning")),
        }
    for nodes in lanes.values():
        for node in nodes:
            linked = [e for e in edges if e["to"] == node["id"]]
            node["connections"] = len(linked)
            node["max_score"] = max((e["score"] for e in linked), default=0)

    covered = sum(1 for c in concepts if c["coverage"]["outward"] >= 18)
    grounded = sum(1 for c in concepts if c["coverage"]["grounded"] >= 18)
    planned = sum(1 for c in concepts if c["coverage"]["planned"])
    root = packages_root()
    folder = ""
    if package is not None:
        try:
            folder = str(package.relative_to(root))
        except ValueError:
            folder = package.name
    columns = [
        {"id": "job", "title": "Job description", "subtitle":
         ("Every substantive source line, in original section order" +
          (f" · {concepts[0]['source']}" if concepts else "")),
         "nodes": concepts},
        {"id": "resume", "title": "Resume", "subtitle":
         resume_path.name if resume_path else "No resolved resume", "nodes": lanes["resume"]},
        {"id": "cover", "title": "Cover letter", "subtitle":
         cover_path.name if cover_path else "No resolved cover letter", "nodes": lanes["cover"]},
        {"id": "narrative", "title": "Narratives", "subtitle":
         "Interview prep, answers, and approved Evidence Ledger stories",
         "nodes": lanes["narrative"]},
        {"id": "self", "title": "The self", "subtitle":
         "Master History body is selection context, its endnotes are the "
         "claim gate; self.json subordinate",
         "nodes": lanes["self"]},
    ]
    return {
        "role": {k: role.get(k) for k in ("uid", "company", "title", "url",
                                           "apply_url", "fit", "tier")},
        "package": {"available": package is not None, "folder": folder,
                    "version": _latest_version(package).name if package else ""},
        "columns": columns,
        "edges": edges,
        "coverage": {"concepts": len(concepts), "covered": covered,
                     "grounded": grounded, "planned": planned,
                     "gaps": [c["id"] for c in concepts
                              if c["coverage"]["outward"] < 18]},
        "method": ("Connections are deterministic shared-language signals, "
                   "not a claim that a requirement is satisfied. The "
                   "Master History endnotes remain the claim authority; "
                   "renderings never become sources."),
    }


# ------------------------------------------------- taking a dropped document
#
# A lane reads "No connected material found" when the package's own artifact
# is absent or unreadable.  Dropping a markdown file on that lane is the
# owner saying "this belongs here", and the FILENAME decides what happens
# next — never a model:
#
#   * a name the lane already looks for is a RECONNECT.  The file was meant
#     to be in the package and was not found, so it is written where the map
#     reads from and the lane fills on the next build.  Deterministic,
#     immediate, no session.
#   * any other name is material Vira cannot place on its own.  NOTHING is
#     written until the owner confirms; then the file is staged in the
#     package's own inbox and a session is dispatched to fold it into the
#     canonical artifact.  A drag gesture must not silently start an agent
#     rewriting the owner's resume.


def _refuse_if_passive():
    if os.environ.get("VIRA_PASSIVE"):
        raise PermissionError(
            "passive instance: application packages are the owner's real "
            "files, outside this clone")


def _version_names(lane):
    """Every canonical V<N>/ filename this lane reads, in read order."""
    return [name for _root, names in LANE_ARTIFACTS.get(lane, ())
            for name in names]


def canonical_drop_name(lane, filename):
    """The canonical name a dropped file already carries, or "".

    Matched against the same table `_read_artifact` reads, so "this is the
    file that should have been picked up" means exactly "this is a file the
    lane looks for" — one definition, no second list to drift.
    """
    name = Path(str(filename or "")).name
    for pattern in _version_names(lane):
        if fnmatch.fnmatch(name.casefold(), pattern.casefold()):
            return name
    return ""


def _writable_version(package):
    """The V<N>/ a drop writes into, created when the package has none."""
    version = _latest_version(package)
    if version == package:
        version = package / "V1"
    version.mkdir(parents=True, exist_ok=True)
    return version


def _safe_drop(lane, filename, text):
    """Validate a drop and return its bare filename. Raises ValueError."""
    if lane not in LANE_ARTIFACTS:
        raise ValueError(f"{lane} does not take dropped material")
    name = Path(str(filename or "")).name.strip()
    if not name or name.startswith("."):
        raise ValueError("that file has no usable name")
    if Path(name).suffix.casefold() not in DROP_SUFFIXES:
        raise ValueError("Vira takes Markdown or plain text here "
                         "(.md, .markdown, .txt)")
    body = str(text or "")
    if not body.strip():
        raise ValueError("that file is empty")
    if len(body.encode("utf-8")) > MAX_DROP_BYTES:
        raise ValueError("that file is larger than this drop accepts "
                         f"({MAX_DROP_BYTES // 1000}KB)")
    return name


def connect_prompt(role, lane, staged, package):
    """The dispatch that folds an unplaceable document into the package."""
    from . import applications
    version = _writable_version(package)
    targets = " or ".join(_version_names(lane)) or "its own artifact"
    return "\n".join([
        f"connect-{applications.session_slug(role)}-{lane}",
        "",
        f"The owner dropped a document onto the {LANE_TITLES[lane]} lane of "
        f"this role's evidence map. Vira could not place it: the filename is "
        f"not one this lane reads, so it is staged UNREAD at",
        f"  {staged}",
        "",
        f"Fold its material into this package's own {LANE_TITLES[lane]} "
        f"artifact in {version} — the file the evidence map reads is "
        f"{targets}. Create that file if it does not exist; revise it in "
        "place if it does. Then delete the staged copy, since it is an inbox "
        "and not a source.",
        "",
        "The dropped document is the owner's material, not a claim licence: "
        "every sentence that reaches the artifact still has to clear the "
        "gate below. If the document says something the record does not "
        "support, leave it out and say so plainly at the end.",
        "",
        *applications.ground_rules(),
        "",
        "PACKAGE:",
        f"  {package}",
        "ROLE:",
        json.dumps({k: role.get(k) for k in ("uid", "company", "title", "url")},
                   indent=1, ensure_ascii=False),
    ])


def attach_material(role, lane, filename, text, confirm=False):
    """Take a dropped document into this role's package.

    Returns a dict the route serves. `applied` means the package changed;
    `prompt` means the caller should dispatch a session (the route owns the
    launch, exactly as the Apply route does).
    """
    _refuse_if_passive()
    name = _safe_drop(lane, filename, text)
    package = find_package(role)
    if package is None:
        raise ValueError(
            "no application package for this role yet — write the "
            "application first, then drop material onto it")
    body = str(text)
    canonical = canonical_drop_name(lane, name)
    if canonical:
        version = _writable_version(package)
        target = version / canonical
        backup = ""
        if target.exists() and target.read_bytes() != body.encode("utf-8"):
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            keep = target.with_suffix(target.suffix + f".{stamp}.bak")
            keep.write_bytes(target.read_bytes())
            backup = keep.name
        target.write_text(body, encoding="utf-8")
        return {"applied": True, "action": "reconnected", "lane": lane,
                "path": str(target), "name": canonical, "backup": backup,
                "message": (f"{canonical} filed into {version.name} — the "
                            f"{LANE_TITLES[lane]} lane reads it now")}
    if not confirm:
        targets = " or ".join(_version_names(lane))
        return {"applied": False, "action": "needs_session", "lane": lane,
                "name": name,
                "message": (f"{name} is not a name this lane reads "
                            f"({targets}). Vira can open a session to fold "
                            f"it into the {LANE_TITLES[lane]} — nothing is "
                            "written until you say so.")}
    inbox = _writable_version(package) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    staged = inbox / name
    staged.write_text(body, encoding="utf-8")
    return {"applied": False, "action": "session", "lane": lane,
            "name": name, "path": str(staged),
            "prompt": connect_prompt(role, lane, staged, package),
            "message": f"{name} staged — opening a session to connect it"}


def save_note(role, concept_key, lane, text):
    """Validate a planning note against this role's current requirements."""
    from . import applications
    concepts = _job_concepts(role, find_package(role))
    if concept_key not in {c["concept_key"] for c in concepts}:
        raise ValueError("requirement is no longer present in this posting")
    return applications.update_map_note(role["uid"], concept_key, lane, text)


def export_markdown(role):
    """A complete, portable requirement-by-requirement action brief."""
    data = build(role)
    nodes = {n["id"]: n for column in data["columns"]
             for n in column["nodes"]}
    edges = data["edges"]
    lines = [
        f"# {role.get('title') or 'Role'} — application evidence brief",
        "",
        f"Company: {role.get('company') or ''}",
        f"Posting: {role.get('url') or role.get('apply_url') or ''}",
        "",
        "## Coverage",
        "",
        (f"- {data['coverage']['covered']} of {data['coverage']['concepts']} "
         "job-description lines have outward material."),
        f"- {data['coverage']['grounded']} are grounded in the canonical self.",
        f"- {data['coverage']['planned']} gaps have owner planning notes.",
        "",
        ("Planning notes are drafting instructions, not evidence. Verify "
         "every claim against the Master History endnote that governs it "
         "before using it."),
    ]
    job_nodes = data["columns"][0]["nodes"]
    current_heading = None
    for index, concept in enumerate(job_nodes, 1):
        heading = concept.get("heading") or "Job description"
        if heading != current_heading:
            lines.extend(("", f"## {heading}", ""))
            current_heading = heading
        linked = sorted((e for e in edges if e["from"] == concept["id"]),
                        key=lambda e: (bool(e.get("planning")), -e["score"]))
        covered = concept["coverage"]["outward"] >= 18
        lines.append(f"### {'[x]' if covered else '[ ]'} {index}. {concept['text']}")
        lines.append("")
        lines.append(f"- Status: {'Covered' if covered else 'Gap'}")
        for lane in ("resume", "cover", "narrative", "self"):
            lane_edges = [e for e in linked if e["lane"] == lane and
                          not e.get("planning")]
            if lane_edges:
                best = lane_edges[0]
                other = nodes.get(best["to"], {})
                lines.append(
                    f"- {lane.title()}: {other.get('text', '')} "
                    f"(signal {best['score']})")
        notes = [e for e in linked if e.get("planning")]
        for edge in notes:
            note = nodes.get(edge["to"], {})
            lines.append(f"- Planning note — {edge['lane']}: {note.get('text', '')}")
        if not covered:
            lines.append("- Action: add gate-grounded evidence or revise "
                         "the outward package.")
        lines.append("")
    return {
        "filename": f"{_slug(role.get('company'))}-{_slug(role.get('title'))}-evidence-brief.md",
        "text": "\n".join(lines).rstrip() + "\n",
    }
