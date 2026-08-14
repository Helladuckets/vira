"""The resume viewport — an application document you can read, question, and
annotate, with the annotations outliving the draft they were written against.

The Applications module could dispatch a package build and could map its
requirements, but it could never SHOW you the resume.  The Map reads the same
package folder (`applicationmap._read_artifact`) and flattens it into graph
units — deliberately, because a map is not a document: it keeps only the lines
relevant to a requirement, capped at 44.  This module renders the whole thing
in order, so the owner can walk someone through it.

WHY THE ROOT .docx AND NOT THE FROZEN V<N>/ SOURCE.  The package layout puts
the current round's Word copies in the package ROOT and the markdown/PDF
record in `V<N>/`, and the root copies are the ones the owner edits by hand.
Rendering the frozen source would show him a document that is not the one he
has been working on, so `applicationmap._read_artifact`'s existing preference
(root globs first, version folder second) is inherited rather than overridden.

WHY TERMS ARE THE FIRST-CLASS ANCHOR.  A package is rebuilt at every version
bump, so a note pinned to a sentence dies exactly when the owner has done the
most work — the redraft.  A term ("orchestration", "throughput") survives a
rewording; a specific bullet does not.  So a term bubble is keyed by the term
itself and is durable by construction, while a LINE note carries the text it
was written against and degrades honestly (`stale`, with the old wording shown)
rather than silently re-anchoring to a sentence that now says something else.

WHY THE GLOSS IS GLOBAL WITH A PER-ROLE OVERRIDE.  Walking through the twelfth
resume must not start from zero, so what the owner works out about a term
becomes the default everywhere; where one employer needs a different angle the
role's own note wins.  Same overlay shape as `contactcard.py`: the durable
layer underneath, the specific correction on top, and clearing the override
falls back rather than deleting.

WHAT THIS MODULE NEVER DOES.  It does not write the self-record.  A claim about
the owner's career is governed by the Master History endnotes (the claim gate),
and Vira does not author those — `feedback()` routes a broader note to the
brief journal, whose integration pass and unapplied-instruction staging already
carry that work to a session under the gate.  Job-specific feedback lands
deterministically in the role's own owner state, which is backed up and which
`apply_prompt` already feeds back into the next build.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path

from . import applicationmap, applications, jsonstore, suggest

STORE = Path(__file__).resolve().parent.parent / "data" / "resume-notes.json"

KINDS = ("resume", "resume1p", "cover")
KIND_LABEL = {"resume": "Resume", "resume1p": "Resume (1 page)",
              "cover": "Cover letter"}

# Where each kind lives, in the same preference order _read_artifact uses:
# the editable Word copy in the package root, then the round's source.
#
# `resume` and `resume1p` share a glob and are separated by
# applicationmap.is_one_pager — the one rule both readers ask, rather than a
# second pattern here that could drift from the Map's. Annotations are already
# scoped by kind (block ids carry it), so the two resumes keep separate notes
# and neither reports the other's as stale.
ARTIFACT = {
    "resume": (("*_cv_*.docx",), ("*_cv_*.md",)),
    "resume1p": (("*_cv_*.docx",), ("*_cv_*.md",)),
    "cover": (("cover-letter.docx",), ("cover-letter.txt",)),
}
ONE_PAGE_KINDS = ("resume1p",)

MAX_TERM = 120
MAX_NOTE = 4000
MAX_BLOCKS = 400
MAX_ANCHORS = 8
MAX_QUESTION = 600
# A selection long enough to be a sentence is a claim, not a term — the same
# floor define.py uses to refuse defining a paragraph.
MAX_TERM_WORDS = 8

_WS = re.compile(r"\s+")
_TABLE_RULE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$")


class ViewError(RuntimeError):
    pass


def _passive():
    return bool(os.environ.get("VIRA_PASSIVE"))


def _now():
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def _clean(text, cap=MAX_NOTE):
    text = _WS.sub(" ", str(text or "")).strip()
    return text[:cap]


def term_key(term):
    """Terms are matched case- and punctuation-insensitively: the owner who
    selects 'Evidence Gate' and later 'evidence gate' means one thing."""
    return re.sub(r"[^a-z0-9]+", " ", str(term or "").casefold()).strip()


def _blank():
    return {"glossary": {}, "roles": {}}


def _load():
    s = jsonstore.read(STORE, _blank())
    if not isinstance(s, dict):
        s = _blank()
    s.setdefault("glossary", {})
    s.setdefault("roles", {})
    return s


def _save(fn):
    return jsonstore.mutate(STORE, fn, _blank(), indent=1, ensure_ascii=False)


def _role(state, uid):
    row = state["roles"].setdefault(uid, {})
    row.setdefault("terms", {})
    row.setdefault("lines", {})
    row.setdefault("claims", {})
    return row


# ---------------------------------------------------------------- document

def _block_id(kind, text, seen):
    """Identity is the TEXT, never the position.

    Keying on order would break every line note the moment a paragraph is
    inserted above it, which is the common edit.  Keying on text means only a
    REWORDING breaks the anchor — and a rewording genuinely is a different
    sentence, which is the one case worth reporting as stale.  Duplicate lines
    (a repeated heading, two identical bullets) take an occurrence suffix so
    two blocks can never share an id.
    """
    base = _WS.sub(" ", text).strip().casefold()
    n = seen.get(base, 0)
    seen[base] = n + 1
    digest = hashlib.sha1(f"{kind}\0{base}\0{n}".encode("utf-8")).hexdigest()
    return f"{kind}-{digest[:10]}"


def _blocks(markdown, kind):
    """Parse _docx_markdown output back into ordered, typed blocks.

    _docx_markdown emits '## ' for a Word heading style, '- ' for a numbered
    or bulleted paragraph, '| a | b |' for a real data row, and bare text for
    everything else.  That is enough structure to render a document; the graph
    layer's `_markdown_units` is deliberately not reused because it drops and
    merges lines to make readable map cards.
    """
    out, seen = [], {}
    for raw in (markdown or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if _TABLE_RULE.match(line):
            continue
        if line.startswith("## "):
            kind_, text = "h", line[3:].strip()
        elif line.startswith("- "):
            kind_, text = "li", line[2:].strip()
        elif line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            kind_, text = "row", "  ".join(c for c in cells if c)
        else:
            kind_, text = "p", line.strip()
        if not text:
            continue
        out.append({"id": _block_id(kind, text, seen), "type": kind_,
                    "text": text})
        if len(out) >= MAX_BLOCKS:
            break
    return out


def document(role, kind):
    """Render one package artifact as ordered blocks.

    Never raises for an absent package: a role whose package was never built
    reports `found: false` with the reason named, the way every dormant
    surface in this app does.
    """
    if kind not in KINDS:
        raise ViewError(f"kind must be one of {KINDS}")
    package = applicationmap.find_package(role)
    if package is None:
        return {"kind": kind, "label": KIND_LABEL[kind], "found": False,
                "reason": "No application package has been built for this "
                          "role yet.", "blocks": [], "path": "",
                "package": ""}
    root_globs, version_names = ARTIFACT[kind]
    text, path = _read_source(package, root_globs, version_names,
                              one_page=_one_page_filter(kind))
    if not path:
        return {"kind": kind, "label": KIND_LABEL[kind], "found": False,
                "reason": f"The package folder has no {KIND_LABEL[kind].lower()}.",
                "blocks": [], "path": "", "package": package.name}
    blocks = _blocks(text, kind)
    return {
        "kind": kind,
        "label": KIND_LABEL[kind],
        "found": bool(blocks),
        "reason": "" if blocks else "That file is empty.",
        "path": path.name,
        "package": package.name,
        "editable": path.parent == package,
        "blocks": blocks,
        "pdf": _pdf_name(package, kind),
    }


def _read_source(package, root_globs, version_names, one_page=None):
    """Root .docx first — the copy the owner edits — then the round's source.

    `one_page` picks which resume form to serve: True the one-page companion,
    False the two-page record, None no filtering (the cover letter, which has
    no variants).
    """
    candidates = []
    for pattern in root_globs:
        candidates.extend(sorted(package.glob(pattern)))
    version = applicationmap._latest_version(package)
    for name in version_names:
        candidates.extend(sorted(version.glob(name)))
    if one_page is not None:
        candidates = [p for p in candidates
                      if applicationmap.is_one_pager(p) is one_page]
    for path in candidates:
        if not path.is_file():
            continue
        if path.suffix.casefold() == ".docx":
            text = applicationmap._docx_markdown(path)
        else:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        if text.strip():
            return text, path
    return "", None


def _one_page_filter(kind):
    """Which resume form this kind serves; None for anything that has one."""
    if kind in ONE_PAGE_KINDS:
        return True
    return False if kind == "resume" else None


def _pdf_name(package, kind):
    """The PDF is the pixel-true render; the viewport links it rather than
    trying to reproduce Word's layout in HTML."""
    version = applicationmap._latest_version(package)
    stem = "cover-letter.pdf" if kind == "cover" else "*_cv_*.pdf"
    want = _one_page_filter(kind)
    for found in sorted(version.glob(stem)):
        if not found.is_file():
            continue
        if want is not None and applicationmap.is_one_pager(found) is not want:
            continue
        return found.name
    return ""


def source_path(role, kind, name):
    """Resolve a package file for serving, refusing anything outside it."""
    package = applicationmap.find_package(role)
    if package is None or not name:
        return None
    for candidate in (package / name, applicationmap._latest_version(package) / name):
        try:
            resolved = candidate.resolve()
            resolved.relative_to(package.resolve())
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    return None


# ------------------------------------------------------------- annotations

def annotations(uid, blocks=None, kind=""):
    """The margin rail for one role: term bubbles, claim bubbles, line notes.

    `blocks` is the document currently on screen; when given, a line note whose
    block is gone is returned `stale: true` carrying the wording it was written
    against.  Without it staleness is unknown rather than assumed false — a
    surface may only claim what it can see.

    `kind` scopes LINE and CLAIM bubbles to the document being read. A note
    lives on one line of one artifact, so leaving the resume's notes on screen
    while the cover letter is open reported every one of them as stale — a
    different and much worse statement than "this belongs to the other
    document".  Block ids carry their kind as a prefix, so the two cases are
    told apart deterministically rather than guessed.  TERMS deliberately stay
    on both: a word you have worked out how to defend is a fact about the whole
    application, not about one file in it.
    """
    state = _load()
    row = state["roles"].get(uid, {})
    live = {b["id"] for b in (blocks or [])}
    mine = (lambda b: b.startswith(kind + "-")) if kind else (lambda _b: True)
    terms = []
    # The rail belongs to THIS document, so only a term pinned to this role
    # appears on it; the global glossary supplies the wording, it does not
    # decide membership. A term the owner worked out on another application
    # shows up here the moment he pins it here too.
    for key, pin in sorted(row.get("terms", {}).items()):
        glob = state["glossary"].get(key) or {}
        note = pin.get("note") or ""
        terms.append({
            "key": key,
            "term": pin.get("term") or glob.get("term") or key,
            "note": note or glob.get("note", ""),
            "scope": "role" if note else ("global" if glob.get("note")
                                          else "pinned"),
            "global_note": glob.get("note", ""),
            "when": pin.get("when") or glob.get("when", ""),
        })
    claims = []
    for bid, rec in sorted(row.get("claims", {}).items(),
                           key=lambda kv: kv[1].get("when", "")):
        if not mine(bid):
            continue
        claims.append({**rec, "block_id": bid,
                       "stale": bool(blocks) and bid not in live})
    lines = []
    for bid, rec in sorted(row.get("lines", {}).items(),
                           key=lambda kv: kv[1].get("when", "")):
        if not mine(bid):
            continue
        lines.append({**rec, "block_id": bid,
                      "stale": bool(blocks) and bid not in live})
    return {"terms": terms, "claims": claims, "lines": lines}


def set_term(uid, term, note="", scope="global", kind=""):
    """Pin a term to this document, and optionally record what the owner can
    stand behind about it.  `scope` global writes the durable default; role
    writes the override that wins for this application only."""
    display = _clean(term, MAX_TERM)
    key = term_key(display)
    if not key:
        raise ViewError("a term is required")
    if len(display.split()) > MAX_TERM_WORDS:
        raise ViewError("that is a claim, not a term — use 'can I stand "
                        "behind this' for a whole sentence")
    if scope not in ("global", "role"):
        raise ViewError("scope must be global or role")
    text = _clean(note)

    def apply(state):
        row = _role(state, uid)
        # Pinning is what puts the bubble on this document's rail, so the role
        # entry is written even when the gloss itself is global.
        pin = row["terms"].setdefault(key, {"term": display})
        pin["term"] = display
        if scope == "role":
            pin["note"] = text
            pin["when"] = _now()
            if not text:
                pin.pop("note", None)
        else:
            entry = state["glossary"].setdefault(key, {})
            entry["term"] = display
            entry["note"] = text
            entry["when"] = _now()
            if not text:
                state["glossary"].pop(key, None)
    _save(apply)
    return annotations(uid, kind=kind)


def clear_term(uid, key, kind=""):
    """Unpin a term from this document. The global gloss is left alone —
    it belongs to every other resume too."""
    def apply(state):
        _role(state, uid)["terms"].pop(term_key(key) or key, None)
    _save(apply)
    return annotations(uid, kind=kind)


def set_line_note(uid, block_id, note, quote="", kind=""):
    """A note against one line, carrying the wording it was written against so
    a later redraft can report it as stale instead of silently moving it."""
    block_id = _clean(block_id, 64)
    if not block_id:
        raise ViewError("block id is required")
    text = _clean(note)

    def apply(state):
        row = _role(state, uid)
        if text:
            row["lines"][block_id] = {"note": text, "quote": _clean(quote, 600),
                                      "when": _now()}
        else:
            row["lines"].pop(block_id, None)
    _save(apply)
    return annotations(uid, kind=kind)


def set_claim(uid, block_id, question, answer, citations=(), quote="", kind=""):
    """Bank a 'can I stand behind this?' answer as a durable margin bubble."""
    block_id = _clean(block_id, 64)
    if not block_id:
        raise ViewError("block id is required")

    def apply(state):
        _role(state, uid)["claims"][block_id] = {
            "question": _clean(question, MAX_QUESTION),
            "answer": _clean(answer),
            "citations": [_clean(c, 400) for c in list(citations)[:MAX_ANCHORS]],
            "quote": _clean(quote, 600),
            "when": _now(),
        }
    _save(apply)
    return annotations(uid, kind=kind)


def clear_claim(uid, block_id, kind=""):
    def apply(state):
        _role(state, uid)["claims"].pop(_clean(block_id, 64), None)
    _save(apply)
    return annotations(uid, kind=kind)


# ------------------------------------------------------- grounded questions

ASK_PROMPT = """You are answering one question about a job-application \
document for the person whose career it describes. He is preparing to defend \
every line of it in an interview.

ANSWER ONLY FROM THE NUMBERED ANCHORS BELOW. They are drawn from his own \
career record; anchors marked GATE carry the approved outward wording and its \
limits, and they outrank narrative context on any conflict. If the anchors do \
not support the line, say so plainly and say what would be needed — never \
reason from general knowledge about what someone with this title probably did.

Return STRICT JSON, no prose around it:
{"answer": "<two to five sentences, plain and specific>",
 "anchors": [<indexes of the anchors you actually used>],
 "supported": true|false}

supported is false when the anchors do not establish the claim.

THE LINE IN QUESTION:
%(line)s

THE QUESTION:
%(question)s

DOCUMENT CONTEXT (surrounding lines, for reference only):
%(context)s

ANCHORS FROM THE CAREER RECORD:
%(anchors)s
"""


# A match must share at least this many distinct tokens: coverage is a RATIO,
# so a short line sharing one lucky word reads as well-covered ("arrange a
# convenient time next week" matched the record on 'week' alone).
MIN_SHARED = 2
# Both floors are expressed in MULTIPLES OF THE CORPUS'S OWN MEDIAN IDF, never
# in absolute nats. Measured: the real record (510 passages) has a median idf
# of 5.14 while a short one (4 passages) has 0.69, so a fixed floor of 6.0 is
# 1.2 typical tokens on one and 8.7 on the other — it would silently answer
# "nothing supports this" for every line of a career record that is merely
# short, which is the confident-wrong-way-round failure.
STRICT_MULT = 2.0   # is this line a claim the record speaks to at all?
RELAXED_MULT = 1.0  # once it is, which gate wording governs it?
GATE_SLOTS = 4
BODY_SLOTS = 5

_corpus_cache = {"key": None, "nodes": [], "tokens": [], "idf": {}}


def _corpus():
    """The owner's record, tokenized once with an inverse-document-frequency
    weight per token, cached on the record's own mtime."""
    history = applications.self_record() / "canon" / "MASTER_HISTORY.md"
    try:
        stat = history.stat()
        key = (str(history), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None
    if key is not None and _corpus_cache["key"] == key:
        return _corpus_cache["nodes"], _corpus_cache["tokens"], _corpus_cache["idf"]
    nodes = applicationmap._self_nodes()
    tokens = [applicationmap._tokens(n["text"]) for n in nodes]
    df = {}
    for bag in tokens:
        for word in bag:
            df[word] = df.get(word, 0) + 1
    total = max(1, len(nodes))
    idf = {w: math.log(total / (1 + c)) for w, c in df.items()}
    _corpus_cache.update({"key": key, "nodes": nodes, "tokens": tokens,
                          "idf": idf})
    return nodes, tokens, idf


def _anchors_for(text):
    """Deterministic retrieval — no model call, and every offer is inspectable.

    THE OBVIOUS METRIC IS INVERTED HERE, which is why this does not reuse
    `applicationmap.similarity`.  That score divides shared tokens by
    sqrt(len(a) * len(b)), so it PENALIZES a long, thorough passage — and the
    long thorough passage is exactly the one that governs a claim.  Measured
    on the real record against a real resume bullet: the endnote that actually
    covers the claim scored 6 while unrelated career prose scored 28-30,
    matching on 'communication' and 'gate'.  A floor or a weight tweak would
    not have fixed a ranking that was upside down.

    So the score is IDF-WEIGHTED COVERAGE: of everything that makes this line
    distinctive, how much does this passage account for?  Rare tokens ('vira',
    'retrieval', 'cited') carry the weight, generic career language carries
    almost none, and a thorough passage is rewarded rather than punished.
    Same rare-token principle as radar's person tokens and the atlas's
    shared_topic edge.

    GATE PASSAGES GET RESERVED SLOTS rather than competing on that axis. The
    endnotes decide what the owner may actually SAY, so a strong body match
    with no gate wording alongside it is precisely the shape this feature
    exists to prevent — and measured, the governing endnote sat at rank 26
    while its narrative section led. Ranking alone would have hidden it.
    """
    nodes, tokens, idf = _corpus()
    query = applicationmap._tokens(text)
    weight = sum(idf.get(w, 0.0) for w in query)
    if not query or weight <= 0 or not idf:
        return []
    scale = sorted(idf.values())[len(idf) // 2]
    strict, relaxed = STRICT_MULT * scale, RELAXED_MULT * scale
    scored = []
    for node, bag in zip(nodes, tokens):
        shared = query & bag
        if len(shared) < MIN_SHARED:
            continue
        mass = sum(idf.get(w, 0.0) for w in shared)
        if mass < relaxed:
            continue
        scored.append((mass, sorted(shared, key=lambda w: -idf.get(w, 0.0)),
                       node))
    scored.sort(key=lambda r: r[0], reverse=True)
    # TWO STAGES, because one threshold cannot do both jobs. Measured on the
    # real record: an unrelated line's best match carries 9.1 of shared mass
    # while the GATE endnotes that genuinely govern a real claim carry only
    # 6.2-6.9 — they are wording rules, not descriptions, so they match
    # weakly by design and the two ranges OVERLAP.
    # So the strict floor answers one question only: is this line a claim the
    # record speaks to at all? Nothing reaches the gate stage until it is, and
    # noise therefore never benefits from the relaxed floor.
    if not any(mass >= strict for mass, _sig, _n in scored):
        return []
    gate = [r for r in scored
            if r[2].get("detail") == applicationmap.GATE_DETAIL][:GATE_SLOTS]
    body = [r for r in scored
            if r[2].get("detail") != applicationmap.GATE_DETAIL
            and r[0] >= strict][:BODY_SLOTS]
    out, seen = [], set()
    for score, signals, node in gate + body:
        if node["id"] in seen:
            continue
        seen.add(node["id"])
        out.append({
            "text": node["text"],
            "heading": node.get("heading", ""),
            "source": node.get("source", ""),
            "gate": node.get("detail") == applicationmap.GATE_DETAIL,
            "score": round(score / weight, 3),
            "signals": signals[:6],
        })
    return out[:MAX_ANCHORS]


def ask(role, kind, question, block_id="", context_lines=3):
    """One grounded pass over the owner's own record.

    Retrieval is deterministic and the answer is validated against it: an
    anchor index the model invents is dropped rather than rendered, the
    grounded-or-held discipline `evidence.py` and `resolver.py` already use.
    """
    question = _clean(question, MAX_QUESTION)
    if not question:
        raise ViewError("a question is required")
    doc = document(role, kind)
    blocks = doc["blocks"]
    idx = next((i for i, b in enumerate(blocks) if b["id"] == block_id), -1)
    line = blocks[idx]["text"] if idx >= 0 else ""
    lo = max(0, idx - context_lines) if idx >= 0 else 0
    hi = idx + context_lines + 1 if idx >= 0 else min(len(blocks), 12)
    context = "\n".join(b["text"] for b in blocks[lo:hi])
    anchors = _anchors_for(line or question)
    if not anchors:
        return {"answer": "Nothing in the career record uses language close "
                          "to this line, so there is no anchor to stand on "
                          "yet. Check the Master History section that should "
                          "cover it before using this wording.",
                "supported": False, "citations": [], "anchors": [],
                "line": line}
    listed = "\n\n".join(
        f"[{i}]{' GATE' if a['gate'] else ''} {a['heading']}\n{a['text']}"
        for i, a in enumerate(anchors))
    prompt = ASK_PROMPT % {"line": line or "(no line selected)",
                           "question": question, "context": context,
                           "anchors": listed}
    raw = suggest.complete(prompt)
    data = _parse(raw)
    used = []
    for i in data.get("anchors") or []:
        if isinstance(i, int) and 0 <= i < len(anchors):
            used.append(anchors[i])
    return {
        "answer": _clean(data.get("answer") or ""),
        "supported": bool(data.get("supported")),
        "citations": [f"{a['source']} — {a['heading']}" if a["heading"]
                      else a["source"] for a in used],
        "anchors": used,
        "line": line,
    }


def _parse(raw):
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        out = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return out if isinstance(out, dict) else {}


# ---------------------------------------------------------------- feedback

FEEDBACK_SCOPES = ("role", "broader")


def feedback(uid, scope, text, context=""):
    """Route one piece of owner feedback to the place that can act on it.

    ROLE feedback is a fact about this application and lands deterministically
    in the role's own owner state, which `apply_prompt` already reads back into
    the next package build.

    BROADER feedback is a claim about the career, and the career record is
    governed by the Master History endnotes.  Vira does not write those, so it
    goes verbatim to the brief journal, whose integration pass files what it
    can and stages the rest as queued work under the claim gate.  Telling the
    owner it had been applied to the record would be the one dishonest option.
    """
    text = _clean(text)
    if not text:
        raise ViewError("nothing to save")
    if scope not in FEEDBACK_SCOPES:
        raise ViewError(f"scope must be one of {FEEDBACK_SCOPES}")
    if scope == "role":
        applications.update_state(uid, comment=text)
        return {"routed": "application",
                "detail": "Saved to this role. It rides into the next package "
                          "build."}
    if _passive():
        raise ViewError("This is a test instance — a broader note would write "
                        "the real journal, so it is refused here.")
    from . import journal
    entry = journal.add(text, context=_clean(context, 300) or
                        "Resume viewport — broader than this application")
    return {"routed": "journal", "entry": entry.get("id", ""),
            "detail": "Filed to the journal. Vira is reading it now; anything "
                      "it cannot apply itself becomes queued work."}
