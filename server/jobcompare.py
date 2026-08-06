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


def _units(text: str) -> list[str]:
    """Readable responsibility-sized units from lines and sentences."""
    out = []
    headings = {"about the role", "about this role", "the role",
                "role overview", "responsibilities", "requirements",
                "qualifications", "what you'll do", "what you’ll do",
                "what you will do", "you may be a good fit if you have"}
    focused = _INLINE_UNIT_START_RE.sub("\n", role_text(text))
    for line in focused.splitlines():
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", line)
        for part in parts:
            part = re.sub(r"\s+", " ", part).strip(" -•")
            words = _tokens(part)
            if part.casefold().rstrip(":") in headings:
                continue
            if 3 <= len(words) <= 90:
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
        "method": ("Shared language is a three-word phrase comparison of "
                   "the role sections. Employer boilerplate, compensation, "
                   "benefits, and legal text are excluded when headings "
                   "identify them."),
    }
