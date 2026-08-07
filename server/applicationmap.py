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

import hashlib
import json
import math
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from . import evidence, jobcompare, jobdesc, settings

MAX_ARTIFACT_NODES = 44
MAX_SELF_NODES = 56
MAX_TEXT = 520

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
    "references", "alternate identities", "do-not-use", "red herring",
)
SAFE_SELF_KEYS = (
    "identity", "positioning", "adjudicated_stories", "campaign_strategy",
    "employment", "education", "credentials", "resume_vehicle_decoder",
)


def packages_root() -> Path:
    """Application package home, configurable and backward-compatible."""
    override = settings.raw().get("applications_packages_root")
    if override:
        return Path(str(override)).expanduser()
    cloud = Path.home() / "Documents" / "CV" / "15-applications"
    if cloud.is_dir():
        return cloud
    from . import applications
    return applications.self_record() / "15-applications"


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
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}, ""
    fields = {}
    for line in text.splitlines()[:30]:
        if ":" not in line or line.startswith("#"):
            continue
        key, value = line.split(":", 1)
        fields[_slug(key)] = value.strip()
    return fields, text


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


def find_package(role):
    """Resolve a package by posting uid, then exact company/title fallback."""
    from . import applications
    root = packages_root()
    if not root.is_dir():
        return None
    wanted_uid = role.get("uid") or ""
    wanted_company = _slug(role.get("company"))
    wanted_title = _slug(role.get("title"))
    matches = []
    for posting in root.rglob("posting.md"):
        if not VERSION_RE.match(posting.parent.name):
            continue
        fields, posting_text = _posting_fields(posting)
        urls = [fields.get("posting-url") or fields.get("apply-url") or ""]
        urls.extend(URL_RE.findall(posting_text))
        candidate_uids = {applications.role_uid({"url": url})
                          for url in urls if url}
        company = _slug(fields.get("company"))
        lines = posting_text.splitlines()
        title = _slug(lines[0].lstrip("# ") if lines else "")
        explicit_uid = bool(wanted_uid and re.search(
            rf"(?<![a-z0-9-]){re.escape(wanted_uid)}(?![a-z0-9-])",
            posting_text, re.I))
        score = 100 if wanted_uid in candidate_uids or explicit_uid else 0
        if company == wanted_company and title == wanted_title:
            score = max(score, 70)
        elif company == wanted_company and (title in wanted_title
                                             or wanted_title in title):
            score = max(score, 45)
        if score:
            version = int(VERSION_RE.match(posting.parent.name).group(1))
            matches.append((score, version, posting.stat().st_mtime,
                            posting.parent.parent))
    return max(matches, default=(0, 0, 0, None))[3]


def _read_artifact(package, lane, root_globs, version_names):
    if package is None:
        return [], None
    candidates = []
    for pattern in root_globs:
        candidates.extend(sorted(package.glob(pattern)))
    version = _latest_version(package)
    for name in version_names:
        candidates.extend(sorted(version.glob(name)))
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
            _fields, text = _posting_fields(posting)
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
    facts = root / "FACTS.md"
    if facts.is_file():
        try:
            candidates = _markdown_units(facts.read_text(encoding="utf-8"),
                                         "self", "FACTS.md")
        except OSError:
            candidates = []
        out.extend(n for n in candidates if
                   not n["heading"].casefold().startswith("facts —") and
                   not any(skip in n["heading"].casefold()
                           for skip in SKIP_SELF_HEADINGS))
    distilled = root / "self.json"
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


def _story_nodes(package):
    nodes = []
    for root_globs, version_names in (
            (("interview-prep.docx",), ("interview-prep.md",)),
            (("answers.docx",), ("answers.txt",))):
        found, _path = _read_artifact(package, "narrative", root_globs,
                                      version_names)
        nodes.extend(found)
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
                     "Drafting instruction; verify against FACTS.md")
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
    resume, resume_path = _read_artifact(
        package, "resume", ("*_cv_*.docx",), ("*_cv_*.md",))
    cover, cover_path = _read_artifact(
        package, "cover", ("cover-letter.docx",), ("cover-letter.txt",))
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
         "FACTS.md first; self.json is a subordinate distillation",
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
                   "not a claim that a requirement is satisfied. FACTS.md "
                   "remains the claim authority; renderings never become sources."),
    }


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
        ("Planning notes are drafting instructions, not evidence. Verify every "
         "claim against FACTS.md before using it."),
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
            lines.append("- Action: add FACTS-grounded evidence or revise the outward package.")
        lines.append("")
    return {
        "filename": f"{_slug(role.get('company'))}-{_slug(role.get('title'))}-evidence-brief.md",
        "text": "\n".join(lines).rstrip() + "\n",
    }
