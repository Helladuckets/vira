"""Deterministic job-description comparison for the Applications module.

The comparison answers two related questions without a model call:

* How much wording is shared between each pair of role descriptions?
* Which responsibilities repeat across the set, and which distinguish one
  role from the others?

Similarity is deliberately described as *shared language*, not "percent of
the same job".  It is a Dice score over three-word phrases in the role-facing
part of each posting.  Employer boilerplate, compensation, benefits, and legal
copy are excluded where the posting exposes recognizable section headings.
"""
from __future__ import annotations

import html
import re
from difflib import SequenceMatcher
from itertools import combinations

MAX_ROLES = 6
MIN_ROLES = 2

_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?", re.I)
_BLOCK_END_RE = re.compile(
    r"</(?:p|li|ul|ol|h[1-6]|div|section|article|blockquote)\s*>", re.I)
_BREAK_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")

_START_MARKERS = (
    "about the role", "about this role", "the role", "role overview",
    "what you'll do", "what you’ll do", "what you will do",
    "responsibilities", "your responsibilities",
)
_END_MARKERS = (
    "annual compensation range", "the expected salary range",
    "salary range", "compensation and benefits", "pay transparency",
    "benefits and perks", "equal opportunity", "eeo statement",
    "we are an equal opportunity", "accommodations",
    "deadline to apply", "applications will be reviewed",
)

# Board snapshots created before structured description capture flattened every
# HTML list item onto one line.  Sentence splitting alone then turns a whole
# responsibilities or qualifications section into one oversized unit and loses
# the exact differences the comparison exists to surface.  These are common
# starts of responsibility and requirement bullets, kept case-sensitive so a
# verb in the middle of ordinary prose does not become a boundary.
_INLINE_UNIT_START_RE = re.compile(
    r"(?<=\s)(?=(?:"
    r"Responsibilities|Qualifications|Requirements|What you(?:'|’)?ll do|"
    r"What you will do|You may be a good fit if you have|"
    r"(?:Partner|Serve|Support|Create|Guide|Help|Identify|Travel|Maintain|"
    r"Ship|Build|Develop|Design|Lead|Drive|Collaborate|Manage|Own|Deliver|"
    r"Advise|Conduct|Translate|Establish|Implement|Ensure|Define|Provide|"
    r"Research|Evaluate|Improve|Scale|Contribute)\b|"
    r"\d+\s*\+?\s*(?:years?|yrs?)\b|Experience\b|Exceptional\b|Strong\b|"
    r"Comfort(?:able)?\b|Familiarity\b|Knowledge\b|Ability\b|"
    r"Proficien(?:cy|t)\b|Bachelor(?:'|’)?s?\b|Master(?:'|’)?s?\b|"
    r"Excellent\b|Excitement\b|Passion\b|A love\b|You enjoy\b|Track record\b"
    r"))")

# A compact role taxonomy makes a six-description comparison legible.  These
# are responsibilities and working modes, not title or seniority guesses.
_THEMES = (
    ("Customer advisory", r"\bcustomer|\bclient|trusted advisor|stakeholder"),
    ("Hands-on building", r"prototype|proof.of.concept|\bcod(?:e|ing)\b|hands.on|\bbuild(?:ing|s)?\b"),
    ("Architecture and integration", r"architect|integration|technology stack|systems? design|scalable"),
    ("Deployment and delivery", r"deploy|implementation|production|deliver(?:y|ing)?|rollout"),
    ("Evaluation and safety", r"\bevals?\b|evaluation|measure performance|safety|reliab"),
    ("Executive communication", r"executive|c.suite|business value|technical communication|varied audiences"),
    ("Sales partnership", r"account executive|\bsales\b|pre.sales|go.to.market"),
    ("Reusable enablement", r"reusable|blueprint|enablement|demo(?:s)?\b|scale across customers"),
    ("Product feedback", r"product and engineering|product team|insights? back|product feedback"),
    ("Domain expertise", r"industr(?:y|ies)|vertical|domain expertise|regulated|sector"),
    ("People leadership", r"\bmanage(?:r|ment)?\b|\bmentor|\bcoach|lead a team|people leader"),
    ("Travel and onsite work", r"\btravel|customer sites?|on.site|onsite"),
)

_EXPLICIT_REQUIREMENTS = (
    ("Minimum experience", "experience",
     r"\b\d+\s*\+?\s*(?:years?|yrs?)\b"),
    ("Education or credentials", "education",
     r"\b(?:bachelor|master|ph\.?d|degree|certification|certified)\b"),
    ("Travel or onsite expectation", "travel",
     r"\b(?:travel|onsite|on-site|customer sites?)\b"),
    ("Authorization or clearance", "clearance",
     r"\b(?:security clearance|clearance|citizenship|citizen|work authorization|authorized to work)\b"),
)

_TECH_TOOL_RE = re.compile(
    r"\b(?:python|java|javascript|typescript|sql|pytorch|tensorflow|"
    r"kubernetes|docker|aws|azure|gcp|git|api|apis|llm|llms)\b", re.I)
_TOOL_REQUIREMENT_RE = re.compile(
    r"\b(?:expected|required|proficien|comfort|familiar|experience with|"
    r"ability to|knowledge of|working knowledge)\b", re.I)


def _plain(raw: str) -> str:
    """Turn both real HTML and escaped-HTML remnants into readable lines.

    Some older board snapshots decoded entities after stripping tags, leaving
    literal ``<li>`` fragments in the saved text.  Unescaping before removing
    tags handles those records as well as current plain descriptions.
    """
    text = str(raw or "")
    for _ in range(2):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    text = _BREAK_RE.sub("\n", text)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    lines = []
    for line in text.replace("\r", "\n").split("\n"):
        line = re.sub(r"\s+", " ", line).strip(" \t-•")
        if line:
            lines.append(line)
    return "\n".join(lines)


def role_text(raw: str) -> str:
    """Return the role-facing section of a posting, when headings allow it."""
    text = _plain(raw)
    folded = text.casefold()
    starts = [folded.find(marker) for marker in _START_MARKERS]
    starts = [i for i in starts if i >= 0]
    start = min(starts) if starts else 0
    ends = [folded.find(marker, start + 20) for marker in _END_MARKERS]
    ends = [i for i in ends if i >= 0]
    end = min(ends) if ends else len(text)
    focused = text[start:end].strip()
    return focused or text


def _tokens(text: str) -> list[str]:
    return [m.group(0).casefold() for m in _WORD_RE.finditer(text)]


def _shingles(text: str, size: int = 3) -> set[tuple[str, ...]]:
    words = _tokens(text)
    if len(words) < size:
        return {tuple(words)} if words else set()
    return {tuple(words[i:i + size]) for i in range(len(words) - size + 1)}


def shared_language(left: str, right: str) -> int:
    """Dice similarity over unique three-word phrases, as a whole percent."""
    a, b = _shingles(role_text(left)), _shingles(role_text(right))
    if not a and not b:
        return 100
    if not a or not b:
        return 0
    return round(200 * len(a & b) / (len(a) + len(b)))


_HEADINGS = {"about the role", "about this role", "the role",
             "role overview", "responsibilities", "requirements",
             "qualifications", "what you'll do", "what you’ll do",
             "what you will do", "you may be a good fit if you have"}


def _split_rows(text: str) -> list[tuple[str, str]]:
    """Every readable row of a posting in document order.

    Yields ``("heading", text)`` and ``("unit", text)``.  One splitter serves
    both the analysis (which drops headings and caps unit length) and the
    side-by-side view (which keeps headings as section labels and never
    truncates the posting's own words), so the two cannot disagree about
    where one statement ends and the next begins.
    """
    rows = []
    focused = _INLINE_UNIT_START_RE.sub("\n", role_text(text))
    for line in focused.splitlines():
        for part in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", line):
            part = re.sub(r"\s+", " ", part).strip(" -•")
            if not part:
                continue
            if part.casefold().rstrip(":") in _HEADINGS:
                rows.append(("heading", part))
            elif 3 <= len(_tokens(part)) <= 90:
                rows.append(("unit", part))
    return rows


def _units(text: str) -> list[str]:
    """Readable responsibility-sized units from lines and sentences."""
    out = []
    for kind, part in _split_rows(text):
        if kind != "unit":
            continue
        if len(part) > 360:
            part = part[:357].rsplit(" ", 1)[0] + "..."
        out.append(part)
    return out


def description_available(raw: str) -> bool:
    """Whether a description has enough role text for an honest comparison."""
    return len(_tokens(role_text(raw))) >= 20


def _unit_similarity(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b, autojunk=False).ratio()
    sa, sb = set(a), set(b)
    jaccard = len(sa & sb) / len(sa | sb)
    return max(seq, jaccard)


def _dedupe_units(items: list[tuple[float, str]], limit: int) -> list[str]:
    picked = []
    for _score, text in sorted(items, key=lambda item: item[0], reverse=True):
        if any(_unit_similarity(text, prior) >= 0.72 for prior in picked):
            continue
        picked.append(text)
        if len(picked) >= limit:
            break
    return picked


def _common_units(unit_sets: list[list[str]], limit: int = 5) -> list[str]:
    if not unit_sets or any(not units for units in unit_sets):
        return []
    candidates = []
    for unit in unit_sets[0]:
        matches = []
        for others in unit_sets[1:]:
            matches.append(max(_unit_similarity(unit, other)
                               for other in others))
        if matches and min(matches) >= 0.70:
            candidates.append((sum(matches) / len(matches)
                               + min(len(_tokens(unit)), 36) / 100, unit))
    return _dedupe_units(candidates, limit)


def _unique_units(index: int, unit_sets: list[list[str]],
                  limit: int = 4) -> list[str]:
    others = [unit for i, units in enumerate(unit_sets) if i != index
              for unit in units]
    if not others:
        return []
    candidates = []
    for unit in unit_sets[index]:
        best = max((_unit_similarity(unit, other) for other in others),
                   default=0.0)
        if best < 0.56:
            length = min(len(_tokens(unit)), 40) / 100
            candidates.append((1 - best + length, unit))
    return _dedupe_units(candidates, limit)


def _theme_rows(roles: list[dict], unit_sets: list[list[str]]) -> list[dict]:
    rows = []
    for order, (name, pattern) in enumerate(_THEMES):
        rx = re.compile(pattern, re.I)
        present, examples = [], {}
        for role, units in zip(roles, unit_sets):
            match = next((unit for unit in units if rx.search(unit)), None)
            if match:
                present.append(role["uid"])
                examples[role["uid"]] = match
        if not present:
            continue
        count = len(present)
        rows.append({
            "name": name,
            "count": count,
            "kind": "shared" if count == len(roles)
                    else "unique" if count == 1 else "varies",
            "present": present,
            "examples": examples,
            "_order": order,
        })
    rows.sort(key=lambda row: (-row["count"], row["_order"]))
    for row in rows:
        row.pop("_order", None)
    return rows


def _money(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number.is_integer():
        number = int(number)
    return "$" + f"{number:,.0f}"


def _different_values(values: dict[str, list[str]]) -> bool:
    """Whether a structured field actually differs across selected roles."""
    normalized = []
    for rows in values.values():
        normalized.append(tuple(re.sub(r"\s+", " ", row).strip().casefold()
                                for row in rows))
    return len(set(normalized)) > 1


def _specific_rows(roles: list[dict], unit_sets: list[list[str]]) -> list[dict]:
    """Explicit, high-signal differences that must never lose a rank contest.

    The prose-difference section is intentionally a short read. Numeric
    requirements such as "5+ years" versus "3+ years" cannot safely compete
    for those four slots, so this table extracts them structurally and shows
    every matching statement verbatim. Metadata fields ride the same table.
    """
    rows = []

    def add(label, kind, values):
        values = {role["uid"]: list(values.get(role["uid"]) or [])
                  for role in roles}
        if any(values.values()) and _different_values(values):
            rows.append({"label": label, "kind": kind, "values": values})

    add("Locations", "location", {
        role["uid"]: [" / ".join(str(x) for x in role.get("locations") or [])]
        if role.get("locations") else [] for role in roles})

    compensation = {}
    for role in roles:
        lo, hi = _money(role.get("salaryMin")), _money(role.get("salaryMax"))
        compensation[role["uid"]] = ([f"{lo}–{hi}" if lo and hi and lo != hi
                                      else lo or hi] if lo or hi else [])
    add("Compensation range", "compensation", compensation)

    for label, kind, pattern in _EXPLICIT_REQUIREMENTS:
        rx = re.compile(pattern, re.I)
        add(label, kind, {
            role["uid"]: [unit for unit in units if rx.search(unit)]
            for role, units in zip(roles, unit_sets)})

    add("Named technical tools", "tools", {
        role["uid"]: [unit for unit in units
                      if _TECH_TOOL_RE.search(unit)
                      and _TOOL_REQUIREMENT_RE.search(unit)]
        for role, units in zip(roles, unit_sets)})

    # Catch explicit numbers that are not already the years-of-experience
    # statements above (team size, percentages, portfolio counts, and so on).
    years_re = re.compile(_EXPLICIT_REQUIREMENTS[0][2], re.I)
    quantified_re = re.compile(r"\b\d+(?:\.\d+)?\s*(?:\+|%|percent|people|"
                               r"customers?|accounts?|projects?|days?|weeks?|"
                               r"months?)\b", re.I)
    add("Other quantified requirements", "quantified", {
        role["uid"]: [unit for unit in units
                      if quantified_re.search(unit) and not years_re.search(unit)]
        for role, units in zip(roles, unit_sets)})
    return rows


# ==================== The marked-up side by side ====================
#
# Two postings rendered whole, in their own order, with every statement
# classified against its counterpart in the other: identical wording, the
# same statement said differently, or a line only one posting makes.  A
# matched pair is diffed WORD BY WORD, so a line that differs by two words
# reads as shared with those two words marked rather than as a difference.
#
# Every number below is a similarity between two statements, not a claim
# about the jobs.  The reader can see the words that earned it.

LINK_FLOOR = 0.40      # under this, a statement has no counterpart at all
SAME_FLOOR = 0.88      # at or above, the pair is the same statement
MAX_ALIGN_UNITS = 200  # a runaway posting is capped and the drop reported

_SPAN_RE = re.compile(r"\S+")
_EDGE_RE = re.compile(r"^[^\w]+|[^\w]+$")


def document_rows(raw: str) -> list[dict]:
    """The role section as readable rows, headings kept, nothing truncated."""
    return [{"kind": kind, "text": text} for kind, text in _split_rows(raw)]


def _fold(word: str) -> str:
    """A word compared without its surrounding punctuation or case."""
    return _EDGE_RE.sub("", word).casefold()


def _diff_parts(text: str, other: str) -> list[dict]:
    """Split ``text`` into runs of shared and differing words against ``other``.

    Parts concatenate back to the original string exactly — whitespace and
    punctuation ride along untouched, so the posting's own wording is never
    rebuilt from tokens.  Punctuation-only tokens inherit their neighbour
    rather than becoming differences of their own.
    """
    spans = [(m.start(), m.end(), _fold(m.group(0)))
             for m in _SPAN_RE.finditer(text)]
    if not spans:
        return [{"t": text, "d": False}] if text else []
    other_words = [_fold(m.group(0)) for m in _SPAN_RE.finditer(other)]
    matcher = SequenceMatcher(None, [span[2] for span in spans], other_words,
                              autojunk=False)
    shared = set()
    for i, _j, size in matcher.get_matching_blocks():
        shared.update(range(i, i + size))

    parts, cursor, current = [], 0, None
    for index, (start, _end, folded) in enumerate(spans):
        differs = index not in shared
        if not folded and current is not None:
            differs = current
        if current is None:
            current = differs
        elif differs != current:
            parts.append({"t": text[cursor:start], "d": current})
            cursor, current = start, differs
    parts.append({"t": text[cursor:], "d": bool(current)})
    return parts


def _fingerprint(unit: str) -> tuple:
    tokens = _tokens(unit)
    counts = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    return counts, len(tokens), set(tokens)


def _upper_bound(left: tuple, right: tuple) -> float:
    """The most `_unit_similarity` could possibly return for this pair.

    SequenceMatcher's ratio cannot exceed twice the multiset intersection
    over the combined length, and Jaccard cannot exceed the set overlap, so
    a pair failing this bound can be skipped without the quadratic compare.
    """
    (ca, la, sa), (cb, lb, sb) = left, right
    if not la or not lb:
        return 0.0
    overlap = sum(min(ca[t], cb[t]) for t in ca.keys() & cb.keys())
    return max(2 * overlap / (la + lb), len(sa & sb) / len(sa | sb))


def _match(left_units: list[str], right_units: list[str]) -> dict:
    """Pair each statement with at most one counterpart, strongest first.

    One-to-one on purpose: a statement that reads as the counterpart of a
    line already claimed by a closer match is reported as unpaired but
    carries `near`, so the view can say "closest wording is already paired"
    instead of implying the posting never makes that point.
    """
    left_fp = [_fingerprint(unit) for unit in left_units]
    right_fp = [_fingerprint(unit) for unit in right_units]
    scored = []
    for i, fa in enumerate(left_fp):
        for j, fb in enumerate(right_fp):
            if _upper_bound(fa, fb) < LINK_FLOOR:
                continue
            score = _unit_similarity(left_units[i], right_units[j])
            if score >= LINK_FLOOR:
                scored.append((score, i, j))
    scored.sort(key=lambda row: (-row[0], row[1], row[2]))

    best_left, best_right = {}, {}
    for score, i, j in scored:
        best_left.setdefault(i, j)
        best_right.setdefault(j, i)

    taken_left, taken_right, pairs = set(), set(), []
    for score, i, j in scored:
        if i in taken_left or j in taken_right:
            continue
        taken_left.add(i)
        taken_right.add(j)
        pairs.append((score, i, j))
    return {"pairs": pairs, "near_left": best_left, "near_right": best_right}


def _side_rows(raw: str, prefix: str) -> tuple[list[dict], list[int], int]:
    """Document rows plus the index of each unit row, capped and counted."""
    rows, unit_at, dropped = [], [], 0
    for row in document_rows(raw):
        if row["kind"] != "unit":
            rows.append(dict(row))
            continue
        if len(unit_at) >= MAX_ALIGN_UNITS:
            dropped += 1
            continue
        row = dict(row)
        row["id"] = f"{prefix}{len(unit_at)}"
        unit_at.append(len(rows))
        rows.append(row)
    return rows, unit_at, dropped


def align(left: dict, right: dict) -> dict:
    """Both postings, whole and in order, with every statement classified."""
    left_rows, left_at, left_dropped = _side_rows(left.get("jd") or "", "L")
    right_rows, right_at, right_dropped = _side_rows(right.get("jd") or "", "R")
    left_units = [left_rows[i]["text"] for i in left_at]
    right_units = [right_rows[i]["text"] for i in right_at]

    matched = _match(left_units, right_units)
    for row in left_rows + right_rows:
        if row["kind"] == "unit":
            row.update(state="only", link="", score=0, near="", parts=[])

    counts = {"same": 0, "similar": 0, "only_left": 0, "only_right": 0}
    links = []
    for score, i, j in matched["pairs"]:
        a, b = left_rows[left_at[i]], right_rows[right_at[j]]
        state = "same" if score >= SAME_FLOOR else "similar"
        percent = round(score * 100)
        a.update(state=state, link=b["id"], score=percent,
                 parts=_diff_parts(a["text"], b["text"]))
        b.update(state=state, link=a["id"], score=percent,
                 parts=_diff_parts(b["text"], a["text"]))
        links.append({"left": a["id"], "right": b["id"],
                      "kind": state, "score": percent})
        counts[state] += 1

    for rows, at, near, side in ((left_rows, left_at, matched["near_left"], "left"),
                                 (right_rows, right_at, matched["near_right"], "right")):
        other = right_rows if side == "left" else left_rows
        other_at = right_at if side == "left" else left_at
        for index, row_index in enumerate(at):
            row = rows[row_index]
            if row["state"] != "only":
                continue
            counts[f"only_{side}"] += 1
            row["parts"] = [{"t": row["text"], "d": True}]
            if index in near:
                row["near"] = other[other_at[near[index]]]["id"]

    paired = counts["same"] + counts["similar"]
    total = paired * 2 + counts["only_left"] + counts["only_right"]
    return {
        "left": {"uid": left["uid"], "rows": left_rows,
                 "dropped": left_dropped},
        "right": {"uid": right["uid"], "rows": right_rows,
                  "dropped": right_dropped},
        "links": links,
        "counts": counts,
        "matched_pct": round(200 * paired / total) if total else 0,
        "method": ("Statements are paired with their closest counterpart in "
                   "the other posting, one to one. A pair is marked the same "
                   "statement above " + str(round(SAME_FLOOR * 100)) +
                   "% word similarity; the words that differ are marked "
                   "inside it either way."),
    }


def compare(roles: list[dict]) -> dict:
    """Compare two to six full role records carrying ``uid`` and ``jd``."""
    if not MIN_ROLES <= len(roles) <= MAX_ROLES:
        raise ValueError(f"choose between {MIN_ROLES} and {MAX_ROLES} roles")
    uids = [str(role.get("uid") or "") for role in roles]
    if any(not uid for uid in uids) or len(set(uids)) != len(uids):
        raise ValueError("roles must be distinct and carry a uid")
    missing = [role.get("title") or role["uid"] for role in roles
               if not description_available(role.get("jd") or "")]
    if missing:
        raise ValueError("description unavailable for: " + ", ".join(missing))

    pairs = []
    for left, right in combinations(roles, 2):
        score = shared_language(left["jd"], right["jd"])
        pairs.append({
            "left": left["uid"], "right": right["uid"],
            "shared_pct": score, "different_pct": 100 - score,
        })
    overall = round(sum(pair["shared_pct"] for pair in pairs) / len(pairs))
    unit_sets = [_units(role["jd"]) for role in roles]
    role_rows = [{
        "uid": role["uid"],
        "title": role.get("title") or "Untitled role",
        "company": role.get("company") or "",
        "locations": list(role.get("locations") or []),
        "word_count": len(_tokens(role_text(role["jd"]))),
    } for role in roles]
    return {
        "roles": role_rows,
        "overall": {"shared_pct": overall,
                    "different_pct": 100 - overall},
        "pairs": pairs,
        "specifics": _specific_rows(roles, unit_sets),
        "themes": _theme_rows(roles, unit_sets),
        "common": _common_units(unit_sets),
        "unique": {role["uid"]: _unique_units(i, unit_sets)
                   for i, role in enumerate(roles)},
        # The side-by-side reads two postings against each other line for
        # line, so it exists only for a pair.  Three or more roles keep the
        # summary above, which is what a set comparison can honestly say.
        "alignment": align(roles[0], roles[1]) if len(roles) == 2 else None,
        "method": ("Shared language is a three-word phrase comparison of "
                   "the role sections. Employer boilerplate, compensation, "
                   "benefits, and legal text are excluded when headings "
                   "identify them."),
    }
