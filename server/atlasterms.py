"""Reader for the curated AI-terminology atlas on the site.

The atlas is a running prototype and stays on thedurham.nyc (the documents
merge: the site keeps running things, Vira keeps documents).  Its 49 terms
are hand-written and carry real, checked links, so they are the FIRST rung
of the definition ladder in `define.py` — a curated record costs nothing and
is better than anything a model composes on the spot.

Reading the site's own `data.js` in place, rather than copying the terms
into Vira, is the applications.py `lab_root` pattern: one source, and an
edit on the site reaches Vira on the next mtime change.

The file is JavaScript, not JSON: keys are bare identifiers and values are
JS string literals.  `_to_json` converts one object literal at a time with a
STRING-AWARE scanner rather than a regex.  That is not fastidiousness — the
naive `([{,])\\s*(\\w+):` substitution corrupts this exact file, because
several curated values contain a comma-then-word-then-colon inside a quoted
string ("Monitoring, evals: observability explains ...").  A regex cannot
tell that colon from a key.
"""
import json
import re
from pathlib import Path

from . import settings

ATLAS_SUBDIR = "ai-terminology-atlas"
DATA_FILE = "data.js"

# Tables lifted out of data.js. Everything the detail card needs; the page's
# own TERM_DETAILS composition is mirrored in `_detail` below.
TABLES = ("ATLAS_DATA", "FAMILY_DISTINCTIONS", "LINEAGE", "ORIGIN_SOURCES",
          "TECHNICAL", "DISTINCTIONS", "CURRENT_USAGE", "TRAJECTORY",
          "VIRA_FAMILY_EXAMPLES", "VIRA_EXAMPLES", "TERM_READING")

_CONFUSION = {
    "high": ("High: communities routinely use it for materially different "
             "mechanisms; qualify it or choose a narrower noun."),
    "medium": ("Medium: the core meaning is useful, but adjacent communities "
               "draw its boundary differently."),
    "low": ("Low: the term usually points to an implementable or inspectable "
            "mechanism."),
}

_cache = {"key": None, "value": None}


def atlas_dir():
    """Where the atlas lives, or None when no lab root is configured.

    Dormant-by-absence, like every other optional source: a machine with no
    site checkout simply starts the ladder one rung lower.
    """
    lab = (settings.raw().get("lab_root") or "").strip()
    if not lab:
        return None
    return Path(lab).expanduser() / ATLAS_SUBDIR


def data_path():
    d = atlas_dir()
    return None if d is None else d / DATA_FILE


# ---------------------------------------------------------------- JS parsing

def _scan_strings(src):
    """Yield (index, in_string) for every character, tracking JS quoting.

    One scanner serves both brace-matching and key-quoting so the two can
    never disagree about where a string starts.
    """
    quote = None
    escaped = False
    for i, ch in enumerate(src):
        if quote:
            yield i, True
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        else:
            if ch in "\"'`":
                quote = ch
                yield i, True
                continue
            yield i, False


def strip_comments(src):
    """Remove JS comments outside strings.

    Must run BEFORE brace matching, not during JSON conversion: a comment can
    itself contain braces or brackets (this module's own docstring example
    does), and those would unbalance `object_literal`. The scanner is what
    keeps the `//` in a URL safe.
    """
    out = []
    i, n = 0, len(src)
    in_string = dict(_scan_strings(src))
    while i < n:
        if not in_string.get(i) and src[i] == "/" and i + 1 < n:
            if src[i + 1] == "/":
                j = src.find("\n", i)
                i = n if j < 0 else j
                continue
            if src[i + 1] == "*":
                j = src.find("*/", i + 2)
                i = n if j < 0 else j + 2
                continue
        out.append(src[i])
        i += 1
    return "".join(out)


def object_literal(src, name):
    """The balanced `{...}` (or `[...]`) assigned to `name`, or ''."""
    src = strip_comments(src)
    m = re.search(r"(?:^|\n)\s*(?:const|let|var|window\.)\s*" + re.escape(name)
                  + r"\s*=\s*(?=[{\[])", src)
    if not m:
        m = re.search(r"(?:^|\n)\s*window\.\s*" + re.escape(name)
                      + r"\s*=\s*(?=[{\[])", src)
    if not m:
        return ""
    start = m.end()
    open_ch = src[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    for i, in_str in _scan_strings(src[start:]):
        if in_str:
            continue
        ch = src[start + i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return src[start:start + i + 1]
    return ""


_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def _to_json(src):
    """Quote bare object keys and drop trailing commas, outside strings only."""
    out = []
    i = 0
    n = len(src)
    in_string = dict(_scan_strings(src))
    # The last significant non-space character emitted, for "is this a key?"
    prev = ""
    while i < n:
        if in_string.get(i):
            out.append(src[i])
            if src[i] in "\"'`" and not (out and len(out) > 1
                                         and out[-2] == "\\"):
                prev = "\""
            i += 1
            continue
        ch = src[i]
        if ch.isspace():
            out.append(ch)
            i += 1
            continue
        # JS comments, which JSON has none of. Safe here because the scanner
        # already knows we are outside a string — the `//` in a URL is not
        # reached by this branch.
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if prev in ("{", ",", ""):
            m = _IDENT.match(src, i)
            if m:
                j = m.end()
                k = j
                while k < n and src[k].isspace():
                    k += 1
                if k < n and src[k] == ":" and not in_string.get(k):
                    out.append(json.dumps(m.group(0)))
                    i = j
                    prev = "\""
                    continue
        if ch == ",":
            j = i + 1
            while j < n and src[j].isspace():
                j += 1
            if j < n and src[j] in "}]" and not in_string.get(j):
                i += 1                       # trailing comma
                continue
        out.append(ch)
        prev = ch
        i += 1
    return "".join(out)


def parse_tables(src, names=TABLES):
    """Every named table in `src` that parses, as plain Python."""
    tables = {}
    for name in names:
        raw = object_literal(src, name)
        if not raw:
            continue
        try:
            tables[name] = json.loads(_to_json(raw))
        except ValueError:
            continue                          # a table we cannot read is a
    return tables                             # missing table, never a crash


# ------------------------------------------------------------------ the card

def _detail(term, t):
    """Mirror the page's TERM_DETAILS composition for one term."""
    name = term.get("n") or ""
    family = term.get("f") or ""
    technical = (t.get("TECHNICAL", {}).get(name)
                 or f"{term.get('definition', '')} In implementation, this "
                    f"belongs to the {family} layer of the agent system.")
    distinctions = (t.get("DISTINCTIONS", {}).get(name)
                    or t.get("FAMILY_DISTINCTIONS", {}).get(family) or "")
    lineage = (t.get("LINEAGE", {}).get(name)
               or "No reliable first coinage was verified for this edition; "
                  "the atlas tracks its modern LLM-era semantic drift.")
    current = (t.get("CURRENT_USAGE", {}).get(name)
               or "Current usage is represented by the directly linked "
                  "first-party or practitioner source.")
    vira = (t.get("VIRA_EXAMPLES", {}).get(name)
            or t.get("VIRA_FAMILY_EXAMPLES", {}).get(family) or {})
    return {
        "technical": technical,
        "distinctions": distinctions,
        "lineage": lineage,
        "origin_source": t.get("ORIGIN_SOURCES", {}).get(name, ""),
        "current": current,
        "trajectory": t.get("TRAJECTORY", {}).get(term.get("status"), ""),
        "confusion": _CONFUSION.get(term.get("confusion"), ""),
        "vira_example": vira.get("example", ""),
        "reading": t.get("TERM_READING", {}).get(name) or [],
    }


def _card(term, t):
    """One atlas term as a define.py card."""
    d = _detail(term, t)
    rows = [
        ("plain_definition", "Plain definition", term.get("definition", "")),
        ("technical_definition", "Technical definition", d["technical"]),
        ("distinctions", "Synonyms and distinctions", d["distinctions"]),
        ("lineage", "Etymology and lineage", d["lineage"]),
        ("current_usage", "Current usage", d["current"]),
        ("trajectory", "Trajectory", d["trajectory"]),
        ("confusion_risk", "Confusion risk", d["confusion"]),
        ("verdict", "Vocabulary verdict", term.get("verdict", "")),
    ]
    w = term.get("w") or {}
    if w:
        tilt = " · ".join(f"{k} {v}/10" for k, v in w.items())
        rows.append(("tilt", "Cohort tilt", tilt))
    if d["vira_example"]:
        rows.append(("vira_example", "Vira architecture example",
                     d["vira_example"]))

    links = []
    if d["origin_source"]:
        links.append({"label": "origin source", "url": d["origin_source"]})
    for r in d["reading"]:
        if isinstance(r, dict) and r.get("url"):
            links.append({"label": r.get("label") or r["url"],
                          "url": r["url"]})
    return {
        "term": term.get("n", ""),
        "rung": "atlas",
        "source": "AI Terminology Atlas",
        "sourced": True,
        "family": term.get("f", ""),
        "status": term.get("status", ""),
        "signal": term.get("signal"),
        "hype": term.get("hype"),
        "rows": [{"key": k, "label": lb, "value": v}
                 for k, lb, v in rows if v],
        "links": links,
    }


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def load():
    """{normalized term -> card}, mtime-cached. Empty when dormant."""
    path = data_path()
    if path is None or not path.exists():
        _cache["key"], _cache["value"] = None, {}
        return {}
    key = (str(path), path.stat().st_mtime_ns)
    if _cache["key"] == key:
        return _cache["value"]
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _cache["value"] or {}
    t = parse_tables(src)
    index = {}
    for term in (t.get("ATLAS_DATA") or {}).get("terms") or []:
        if not term.get("n"):
            continue
        card = _card(term, t)
        for alias in (term.get("n"), term.get("p")):
            k = _norm(alias)
            # The display name wins an alias collision: `p` is a plural or
            # search phrase, and two terms may legitimately share one.
            if k and (k not in index or alias == term.get("n")):
                index[k] = card
    _cache["key"], _cache["value"] = key, index
    return index


def lookup(term):
    """The curated card for `term`, or None."""
    return load().get(_norm(term))


def status():
    path = data_path()
    return {
        "configured": path is not None,
        "path": str(path) if path else "",
        "exists": bool(path and path.exists()),
        "terms": len(load()),
    }
