"""Read a hand-written resume or cover letter and mark it up against a role.

The owner writes drafts by hand in Word. Everything needed to check one
already exists and had no way to reach a file he wrote himself: the claim
gate in MASTER_HISTORY's endnotes, VOICE.md's standing bans, this role's job
description, and what the employer says it hires for. This module is the
join.

WHAT COMES BACK IS A DOCX HE OPENS IN WORD, and one rule governs it:

    BLACK IS HIS. COLOURED IS VIRA'S.

Not one character of the draft is altered, reordered or dropped - his text
renders verbatim in black, and every suggestion is a separate coloured line
beneath the line it is about. That is what makes a bad suggestion free: he
reads past it. `test_the_owners_text_is_reproduced_verbatim` pins it.

TWO COLOURS, because two different things are being said:

  BLUE  - a proposed REWRITE or a keyword to work in. Something to adopt.
  CLAY  - a FLAG. Something to check, with no rewrite offered.

THE DISTINCTION IS LOAD-BEARING AND IT IS WHY THE OWNER'S "rewrites in
colour" answer is implemented as two shapes rather than one. A voice ban or
a keyword gap has an honest better wording, so it gets one. A claim with no
endnote anchor does NOT: the fix is adjudication against the record, and
inventing a rewrite there would be inventing evidence - the exact failure
the claim gate exists to prevent. So a gate finding is stated as a flag and
never dressed as a suggestion.

NO python-docx. `applicationmap._docx_markdown` reads docx with stdlib XML
and says why in its own docstring - making the writer pull a dependency the
reader deliberately avoids would be the module arguing with itself. A docx
is a zip of four XML parts and a coloured run is three attributes.

Read-only against everything but its own output: the draft is never written
back, the package is never touched, and the marked copy lands beside the
role as a new file.
"""

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
import os
from pathlib import Path

MAX_BYTES = 400_000
MAX_LINES = 400
MAX_SUGGESTIONS = 60

KINDS = ("resume", "cover")

INK = "1A1A1A"          # his words
BLUE = "14568C"         # adopt this
CLAY = "A33B1F"         # check this
MUTE = "555555"         # Vira's own chrome

# ---------------------------------------------------------------- the bans
#
# VOICE.md's standing bans, the half that is mechanical. Each row is
# (pattern, tag, what to do). Deliberately NOT a model call: these are exact,
# and a deterministic finding cannot hallucinate. The judgment half (does
# this letter argue fit instead of disposition) is the model's job below.
#
# The em-dash and curly-quote rows matter most for a hand-written draft:
# Word inserts both by AUTOCORRECT, so they arrive without him typing them.
TYPO_FIX = {"\u2014": "-", "\u2013": "-", "\u2018": "'",
            "\u2019": "'", "\u201c": '"', "\u201d": '"'}

BANS = (
    (re.compile(r"[\u2014\u2013]"), "VOICE",
     "Em or en dash - house style is a plain hyphen, or recast the sentence. "
     "Word's autocorrect inserts these; they are not yours."),
    (re.compile(r"[‘’“”]"), "VOICE",
     "Curly quote - straight quotes only, house style. Also autocorrect."),
    (re.compile(r"\bI am excited to apply\b", re.I), "VOICE",
     "Banned opener. State the conviction with an object instead of the "
     "excitement."),
    (re.compile(r"\bproven track record\b", re.I), "VOICE",
     "Banned phrase - it claims nothing. Name the record."),
    (re.compile(r"\bleverage\b", re.I), "VOICE",
     "Banned word. Say what was actually used or built."),
    (re.compile(r"\bsynerg\w*", re.I), "VOICE",
     "Banned word."),
    (re.compile(r"\bpassionate about\b", re.I), "VOICE",
     "Empty intensifier. The ban is on enthusiasm with no object, never on "
     "stated conviction - name what you love and what it is made of."),
    (re.compile(r"\bthrilled\b", re.I), "VOICE",
     "Empty enthusiasm."),
    (re.compile(r"(?:real estate|the two industries)[^.]{0,60}"
                r"(?:prepared me|more alike)", re.I), "VOICE",
     "The weak transfer claim. It asserts the domains resemble each other, "
     "which invites the obvious objection. The strong version is about the "
     "epistemic regime - years where a plausible answer is worthless if the "
     "operating result contradicts it (endnote 59)."),
)

# A recurring opener is a fingerprint that has become a template. Both of
# these are genuinely his, which is exactly how they turned into boilerplate:
# measured 2026-08-14, the hinge opens six of the twenty-seven letters on
# disk nearly verbatim.
STOCK = (
    (re.compile(r"no power to change how other people worked", re.I),
     "This hinge opens six of your last twenty-seven letters nearly "
     "verbatim. It is yours and it is good, which is how it became stock. "
     "Find this role's own hinge."),
    (re.compile(r"(?:13|thirteen)[- ]year", re.I),
     "The thirteen-years opener recurs across the corpus. Keep the fact, "
     "change the entrance."),
)

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]{2,}")
# Terms that carry no signal about a specific role. A keyword suggestion is
# only worth making when the word distinguishes THIS posting.
_STOP = frozenset("""
the and for you our are with will that this have from your not but they has
who what when where how all any can may who's its their them then than
about into over under more most other some such only own same too very
work working works role roles team teams year years experience
company companies job jobs position positions candidate candidates
applicant applicants please apply application applications
every each been being were was does doing done make makes making
help helps helping include includes including across within here
""".split())


def _norm(text):
    return " ".join(str(text or "").split())


# ------------------------------------------------------------- extraction

def _docx_lines(data):
    """Paragraph text from a docx, using stdlib XML only.

    Mirrors `applicationmap._docx_markdown`'s approach deliberately: one
    reading discipline for docx in this codebase, no dependency.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml")
    except (OSError, KeyError, zipfile.BadZipFile, ValueError):
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    body = root.find(f"{ns}body")
    if body is None:
        return []
    out = []
    for para in body.iter(f"{ns}p"):
        text = "".join(t.text or "" for t in para.iter(f"{ns}t")).strip()
        if text:
            out.append(text)
    return out


def extract(data, filename=""):
    """(lines, source_kind). Never raises on a file it cannot read."""
    name = (filename or "").lower()
    if name.endswith(".docx") or data[:2] == b"PK":
        lines = _docx_lines(data)
        if lines:
            return lines[:MAX_LINES], "docx"
        return [], "docx"
    try:
        text = data.decode("utf-8", errors="replace")
    except (AttributeError, UnicodeError):
        return [], "text"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[:MAX_LINES], "text"


def guess_kind(lines):
    """resume or cover, from the draft's own shape.

    A letter salutes and signs; a resume does neither and carries dates.
    Reported back so a wrong guess is visible and correctable rather than
    silently deciding which checks run.
    """
    blob = " ".join(lines[:40]).lower()
    if re.search(r"\bdear\b|^re:|sincerely|i would welcome", blob):
        return "cover"
    if re.search(r"\b(19|20)\d{2}\s*[-–]\s*((19|20)\d{2}|present)", blob):
        return "resume"
    return "cover" if sum(len(l) for l in lines) < 4000 else "resume"


# --------------------------------------------------------------- findings

def _finding(line_no, tag, note, colour=BLUE, rewrite=""):
    return {"line": line_no, "tag": tag, "note": _norm(note),
            "rewrite": _norm(rewrite), "colour": colour}


def _typo_fixed(line):
    """The line with every banned character mapped to its house form.

    Returns "" when nothing changed, so only a REAL correction is offered.
    """
    fixed = line
    for bad, good in TYPO_FIX.items():
        fixed = fixed.replace(bad, good)
    return fixed if fixed != line else ""


def ban_findings(lines):
    """Deterministic voice findings. No model, no network, always right.

    A TYPOGRAPHY ban carries its own rewrite - the substitution is certain,
    so withholding it would make the owner retype what Vira already knows.
    A PHRASE ban carries guidance instead: "state the conviction with an
    object" has no single correct wording, and offering one would be Vira
    writing his letter rather than checking it.
    """
    out = []
    for i, line in enumerate(lines):
        for pat, tag, note in BANS:
            m = pat.search(line)
            if m:
                out.append(_finding(i, tag, f"'{m.group(0)}' - {note}",
                                    rewrite=_typo_fixed(line)))
        for pat, note in STOCK:
            if pat.search(line):
                out.append(_finding(i, "STOCK", note, CLAY))
    return out


def _term(word):
    """A term with its trailing punctuation removed.

    `python.` can never appear in a draft, so without this the LAST word
    of every sentence in a posting reads as permanently missing - a
    systematic false positive, and the "punctuation is not a search term"
    trap the Queue's search hit in 2026-07-27.  Internal punctuation is
    kept, because node.js and ci/cd are real terms.
    """
    return word.lower().strip(".,;:!?)('\"")


def jd_terms(jd, limit=40):
    """Distinctive terms from THIS posting, most distinctive first.

    Frequency inside the posting decides emphasis; `_STOP` removes the words
    every posting shares. A keyword the whole corpus uses is not a keyword.
    """
    counts = {}
    for w in _WORD.findall(str(jd or "")):
        low = _term(w)
        if not low or low in _STOP or len(low) < 4:
            continue
        counts[low] = counts.get(low, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [w for w, _n in ranked[:limit]]


def keyword_findings(lines, jd):
    """Terms the posting leans on that the draft never says.

    Reported ONCE, as a single finding, deliberately: a per-term list reads
    as an instruction to stuff them in, and the honest use is to notice
    which of them the record can actually support.
    """
    if not jd:
        return []
    draft = " ".join(lines).lower()
    missing = [t for t in jd_terms(jd) if t not in draft]
    if not missing:
        return []
    return [_finding(
        -1, "KEYWORD",
        "This posting leans on words the draft never says: "
        + ", ".join(missing[:14])
        + (f" (+{len(missing) - 14} more)" if len(missing) > 14 else "")
        + ". Work in only the ones the record genuinely supports - a term "
        "you cannot back is worse than a term you omit.")]


MIN_RECORD = 40         # passages; below this the record cannot adjudicate

# A letterhead line carries digits and length but makes no claim.
_LETTERHEAD = re.compile(r"@|https?://|linkedin\.com|\d{3}[-.\s]\d{3}[-.\s]\d{4}"
                         r"|^re:\s", re.I)
_CLAIM_VERB = re.compile(r"\b(led|built|ran|closed|managed|drove|delivered|"
                         r"launched|owned|shipped|grew|cut|raised)\b", re.I)


def record_ready():
    """Can the career record actually adjudicate a claim right now?

    THIS GATE IS THE WHOLE HONESTY OF THE CLAIM CHECK, and it was written
    after the check reported six unsupported claims in a real letter that
    the real record covers.  Retrieval is IDF-weighted over the record's own
    passages, and idf is log(N / (1 + matches)) - so on a TINY corpus every
    token scores at or below zero and retrieval correctly returns nothing
    for every line.  Reading that silence as "the record does not cover
    this" inverts it into a confident accusation, which is exactly the
    failure mode a claim check must never have.

    So: enough passages to mean something, or the check does not run and
    the report SAYS it did not.  A fixture record, a dormant install and a
    genuinely missing MASTER_HISTORY all take that path.
    """
    try:
        from . import resumeview
        nodes = (resumeview._corpus() or ((),))[0]
        return len(nodes) >= MIN_RECORD
    except Exception:  # noqa: BLE001
        return False


def anchor_findings(lines, kind):
    """Lines making a claim the career record does not obviously carry.

    Uses resumeview's IDF-weighted coverage retrieval over MASTER_HISTORY,
    which is already the app's answer to "what in the record speaks to this
    sentence". Deliberately CLAY and deliberately NOT a rewrite: the fix is
    adjudication against the record, and a proposed wording here would be a
    fabricated claim wearing a suggestion's clothes.

    Only lines that ARE claims are asked about - a name, a contact line and
    a salutation are none of Vira's business, and flagging them taught the
    owner to ignore the colour that matters.
    """
    if not record_ready():
        return []
    try:
        from . import resumeview
    except Exception:  # noqa: BLE001 -- the rest of the review still works
        return []
    out = []
    for i, line in enumerate(lines):
        if len(line) < 60 or _LETTERHEAD.search(line):
            continue
        if not (re.search(r"\d", line) or _CLAIM_VERB.search(line)):
            continue
        try:
            anchors = resumeview._anchors_for(line)
        except Exception:  # noqa: BLE001
            return out
        if not anchors:
            out.append(_finding(
                i, "CLAIM",
                "Nothing in the career record obviously covers this line. "
                "Either it is worded further from the record than it needs "
                "to be, or it is a claim that still needs adjudicating into "
                "an endnote before it goes out. Not a wording problem - do "
                "not paraphrase it away.", CLAY))
    return out[:12]


# ----------------------------------------------------------- the docx out

_CT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
       '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
       'content-types">'
       '<Default Extension="rels" ContentType="application/vnd.openxmlformats'
       '-package.relationships+xml"/>'
       '<Default Extension="xml" ContentType="application/xml"/>'
       '<Override PartName="/word/document.xml" ContentType="application/vnd.'
       'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
       '</Types>')

_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
         '2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
         'openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
         ' Target="word/document.xml"/></Relationships>')

_DOC_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
             '2006/relationships"/>')


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _para(text, colour=INK, size=22, bold=False, italic=False, after=120):
    """One WordprocessingML paragraph. `size` is half-points."""
    rpr = f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>'
    rpr += f'<w:color w:val="{colour}"/>'
    if bold:
        rpr += "<w:b/>"
    if italic:
        rpr += "<w:i/>"
    return (f'<w:p><w:pPr><w:spacing w:after="{after}" w:line="276" '
            f'w:lineRule="auto"/></w:pPr>'
            f'<w:r><w:rPr>{rpr}</w:rPr>'
            f'<w:t xml:space="preserve">{_esc(text)}</w:t></w:r></w:p>')


def render_docx(lines, findings, header):
    """The marked-up document.

    His paragraphs in black, verbatim and in order; each finding as its own
    coloured paragraph directly beneath the line it concerns. Findings with
    line -1 are document-wide and land at the end under their own rule.
    """
    by_line = {}
    for f in findings:
        by_line.setdefault(f["line"], []).append(f)

    body = [_para(header["title"], MUTE, 20, bold=True, after=40)]
    for sub in header.get("subs", []):
        body.append(_para(sub, MUTE, 18, italic=True, after=40))
    body.append(_para(
        "Black is your draft, reproduced exactly. Coloured lines are Vira's "
        "and are not in your document. Blue is something to adopt; clay is "
        "something to check.", MUTE, 18, italic=True, after=260))

    for i, line in enumerate(lines):
        body.append(_para(line, INK, 22,
                          after=40 if by_line.get(i) else 160))
        for f in by_line.get(i, []):
            body.append(_para(f"{f['tag']} - {f['note']}", f["colour"], 19,
                              italic=True, after=40))
            if f["rewrite"]:
                body.append(_para(f"TRY - {f['rewrite']}", f["colour"], 20,
                                  after=40))
        if by_line.get(i):
            body.append(_para("", INK, 12, after=120))

    tail = by_line.get(-1, [])
    if tail:
        body.append(_para("ACROSS THE WHOLE DRAFT", MUTE, 20, bold=True,
                          after=120))
        for f in tail:
            body.append(_para(f"{f['tag']} - {f['note']}", f["colour"], 19,
                              italic=True, after=100))

    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/'
           'wordprocessingml/2006/main"><w:body>'
           + "".join(body) +
           '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
           '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
           'w:left="1440"/></w:sectPr></w:body></w:document>')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        zf.writestr("word/document.xml", doc)
    return buf.getvalue()


# --------------------------------------------------------- the model pass

MODEL_TAGS = ("REWRITE", "SHAPE", "CONVICTION", "VOICE")

_PROMPT_HEAD = """You are reviewing a draft the owner wrote BY HAND, for one
specific job. Return findings only. You are not rewriting the document.

WHAT A COVER LETTER IS FOR (this is the standard the draft is judged by):
a letter makes ONE claim about how he thinks or works that a resume
structurally cannot carry, and shows that this company is where that way of
working is the point. The two halves are welded. A letter that walks the
posting's requirement list in prose has done the resume's job twice and the
letter's job not at all. If a paragraph is in the letter because the posting
asked for it, say so - that is the most valuable finding you can return.

A RESUME is judged differently: verb-led bullets, dense specifics, no
adjective inflation, the career's shape preserved, claims carrying concepts
and what he built rather than proof chains.

RULES ON YOUR OWN OUTPUT, and they are enforced after you answer:
- A rewrite may ONLY restate what his line already says. You may not add a
  number, a date, a figure, a company, a title or an outcome that is not
  already in the line you are rewriting. Inventing one is the single worst
  thing you can do here.
- No em dashes, no en dashes, no curly quotes, no emoji, anywhere.
- If a line's problem is that it claims something the record may not carry,
  do NOT offer a rewrite. Say what the claim is and stop.
- Prefer few, sharp findings over many. Silence on a good line is correct.

Return STRICT JSON, no prose, no fence:
{"findings":[{"line":<0-based index>,"tag":"REWRITE|SHAPE|CONVICTION|VOICE",
"note":"<one sentence, what is wrong>","rewrite":"<the better wording, or
empty string>"}]}
"""


def _digits(text):
    return set(re.findall(r"\d+", str(text or "")))


def _clean_model(raw, lines):
    """Validate every proposal against the draft before it can be shown.

    Three refusals, in cost order. An out-of-range line index is a proposal
    about a document that does not exist. A banned character in a suggestion
    is the reviewer breaking the rule it is enforcing. And a rewrite carrying
    a NUMBER the owner's line does not have is a fabricated quantity - the
    named failure mode in VOICE.md, committed at drafting time, which the
    claim gate alone does not catch. Dropped, never repaired.
    """
    try:
        import json
        text = str(raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text).strip()
        data = json.loads(text)
    except Exception:  # noqa: BLE001 -- a bad pass costs the model half only
        return [], {"parse": 1}
    out, dropped = [], {"range": 0, "tag": 0, "typography": 0, "invented": 0}
    rows = data if isinstance(data, list) else (data.get("findings") or [])
    for row in rows[:MAX_SUGGESTIONS]:
        try:
            i = int(row.get("line", -99))
        except (TypeError, ValueError):
            dropped["range"] += 1
            continue
        if not 0 <= i < len(lines):
            dropped["range"] += 1
            continue
        tag = str(row.get("tag") or "").strip().upper()
        if tag not in MODEL_TAGS:
            dropped["tag"] += 1
            continue
        note = _norm(row.get("note"))
        rewrite = _norm(row.get("rewrite"))
        if not note:
            continue
        if re.search(r"[—–‘’“”]", note + rewrite):
            dropped["typography"] += 1
            continue
        if rewrite and not _digits(rewrite) <= _digits(lines[i]):
            dropped["invented"] += 1
            continue
        out.append(_finding(i, tag, note, BLUE if rewrite else CLAY, rewrite))
    return out, dropped


def model_findings(lines, role, kind, hiring=""):
    """One pass. Returns ([], {"unavailable": 1}) when no backend answers."""
    try:
        from . import suggest
    except Exception:  # noqa: BLE001
        return [], {"unavailable": 1}
    numbered = "\n".join(f"{i}: {ln}" for i, ln in enumerate(lines))
    parts = [_PROMPT_HEAD,
             f"\nTHE DRAFT IS A {kind.upper()}.",
             f"\nROLE: {role.get('title', '')} at {role.get('company', '')}"]
    jd = _norm(role.get("jd") or role.get("reason") or "")[:6000]
    if jd:
        parts.append(f"\nTHE POSTING:\n{jd}")
    if hiring:
        parts.append(f"\nWHAT THIS EMPLOYER SAYS IT HIRES FOR:\n{hiring}")
    parts.append(f"\nTHE DRAFT, one line per paragraph:\n{numbered}")
    try:
        raw = suggest.complete("\n".join(parts))
    except Exception:  # noqa: BLE001 -- deterministic findings still ship
        return [], {"unavailable": 1}
    return _clean_model(raw, lines)


# ------------------------------------------------------------- the review

def review(role, data, filename="", kind=""):
    """Read a draft, check it, and return the marked-up docx.

    Deterministic findings always run. The model pass is additive: a backend
    that is down costs the judgment half and nothing else, and the report
    says which halves ran rather than presenting a thinner review as a
    complete one.
    """
    if not data:
        raise ValueError("the file is empty")
    if len(data) > MAX_BYTES:
        raise ValueError(f"the file is larger than {MAX_BYTES // 1000}KB")
    lines, source = extract(data, filename)
    if not lines:
        raise ValueError(
            "no text could be read out of that file - a .docx, .md or .txt "
            "draft is what this reads")
    kind = kind if kind in KINDS else guess_kind(lines)

    hiring = ""
    try:
        from . import companywiki
        info = companywiki.resolve(role.get("company"))
        rows = (info.get("hiring") or {}).get("letter_claims") or []
        hiring = "; ".join(r["title"] for r in rows[:14])
    except Exception:  # noqa: BLE001
        hiring = ""

    findings = ban_findings(lines)
    findings += keyword_findings(lines, role.get("jd") or role.get("reason"))
    # anchor_findings OWNS the refusal; this reads the same fact only to
    # report it.  Gating here as well would be a second implementation of
    # one guarantee - and it was: it made removing the real gate invisible
    # to the suite, which is how a vacuous guard survives a mutation check.
    record = record_ready()
    findings += anchor_findings(lines, kind)
    model, dropped = model_findings(lines, role, kind, hiring)
    findings += model
    findings.sort(key=lambda f: (f["line"] if f["line"] >= 0 else 10 ** 6))

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = {
        "title": f"VIRA CHECK - {role.get('title') or 'this role'} at "
                 f"{role.get('company') or 'this employer'}",
        "subs": [
            f"Read as a {kind}; {len(lines)} paragraphs; {stamp}.",
            f"{len(findings)} findings."
            + ("" if model else "  The judgment pass did not run, so these "
                                "are the mechanical checks only.")
            + ("" if record else "  The career record could not be read, so "
                                 "nothing here checks your claims against "
                                 "it."),
        ],
    }
    return {
        "kind": kind,
        "source": source,
        "lines": len(lines),
        "findings": findings,
        "model_ran": bool(model),
        "record_read": record,
        "dropped": {k: v for k, v in (dropped or {}).items() if v},
        "docx": render_docx(lines, findings, header),
    }


# --------------------------------------------------------------- saving it

def marked_name(filename):
    """The marked copy's name, derived from what was dropped.

    Named for the ORIGINAL rather than the date: the owner drops a draft,
    reads the marked copy beside it, edits the original, drops it again. A
    datestamped name would leave him a pile to tell apart, and the second
    check of one draft is a replacement rather than a new document.
    """
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", (filename or "draft")).strip("-.")
    stem = re.sub(r"\.(docx|md|txt|markdown)$", "", stem, flags=re.I) or "draft"
    stem = re.sub(r"-{2,}", "-", stem).strip("-")
    return f"{stem[:80]}-checked.docx"


def save_beside_package(role, blob, filename):
    """Write the marked copy into the role's package folder.

    Returns the path, or None when the role has no package - a hand-written
    draft for a role never dispatched is exactly the case this feature is
    for, and refusing to review it because it has no folder would be
    backwards. The DOWNLOAD is the deliverable; this is the convenience.
    """
    if os.environ.get("VIRA_PASSIVE"):
        raise PermissionError(
            "this is a test copy of Vira - the marked draft downloads, but it "
            "is not written into your real package folder from here")
    from . import applicationmap
    root = applicationmap.find_package(role)      # a Path, or None
    if root is None or not Path(root).is_dir():
        return None
    out = Path(root) / marked_name(filename)
    out.write_bytes(blob)
    return str(out)
