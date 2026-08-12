"""What a posting's own body says about where the work happens.

A board's location field and its remote flag are metadata an employer
fills in loosely. The description is where the policy is actually
written down, in a sentence somebody wrote on purpose:

    This role is based in San Francisco, CA. We use a hybrid work model
    of 3 days in the office per week and offer relocation assistance.

Measured on OpenAI's live Ashby board 2026-08-12: 735 listed roles, of
which 422 carry `isRemote: True` while naming real cities in their
location strings -- and 221 of those postings carry the sentence above.
`fetch_ashby` believed the flag and appended a "Remote" location that
the employer never wrote, so 179 roles in the snapshot read as eligible
for a New York owner on the strength of a word Vira had invented. The
body was right there in `jd` the whole time.

So: THE BODY OUTRANKS THE FLAG, and nothing else here outranks the
body. This module only reports what a sentence actually says --
`places` are lifted out of the sentence text, never inferred from the
board's own location strings, and a posting that says nothing confident
returns None rather than a guess. That is the same grounded-or-held
discipline `resolver.py` and `evidence.py` hold: an honest silence
beats a confident wrong reading, because a wrong reading here HIDES a
job from the owner.

The reader is deterministic and costs no model call, which is what lets
it run on every role of every sweep.

WINDOWS, NOT SENTENCES. The policy routinely spans a sentence boundary
("...San Francisco, CA. We use a hybrid work model of 3 days...") and
the corpus is full of abbreviations that defeat a sentence splitter
("anywhere in the U.S.", "Washington, D.C."). So each trigger claims a
bounded window of following text. The bound matters in the other
direction too: an unbounded window would eventually reach some
unrelated paragraph mentioning remote work and flip the reading.
"""

from __future__ import annotations

import re

# How much text after a trigger counts as the same policy statement.
# The longest real policy sentence measured on this corpus is 156 chars
# ("...based in San Francisco or NYC, with a hybrid schedule of 3 days
# per week in the office, or can be performed remotely from anywhere in
# the U.S."), so this holds the whole statement plus its follow-on
# clause without reaching the next topic.
WINDOW = 260

# The residency trigger. The adverb slot is what carries the corpus's
# real variety -- "exclusively", "primarily", "ideally", "preferred to
# be", "either fully remote or" -- so it is a bounded wildcard rather
# than a list nobody would keep current.
TRIGGER = re.compile(
    r"(?i)\b(?:this|the)\s+(?:role|position)\s+(?:is|will\s+be)\s+"
    r"(?:\w+\s+){0,4}?based\b")

# A schedule stated with no residency sentence at all still binds a role
# to whatever offices the board named: you cannot do 3 days a week in an
# office from another city.
SCHEDULE = re.compile(
    r"(?i)\bhybrid\s+work\s+model\b|\bhybrid\s+(?:work\s+)?schedule\b|"
    r"\brequires?\s+in-?person\s+presence\b|"
    r"\bin\s+the\s+office\s+(?:\d+|one|two|three|four|five)\s+days?\b")

# Read in this order: a refusal is worded with almost the same words as
# an offer -- "we aren't considering remote applications" against "we
# are open to considering remote" -- so permissiveness can only be
# judged after the refusals have had their say. Getting that order or
# these patterns wrong does not merely miss a refusal, it reads one as
# an offer, which is the worst answer available. `_clean` folds curly
# apostrophes to straight ones before this runs: the corpus writes
# "aren't" with U+2019 and a missed contraction here inverted the
# reading of every posting that refused remote in that spelling.
REFUSAL = re.compile(
    r"(?i)(?:are\s*n'?t|is\s*n'?t|are\s+not|is\s+not|not\s+currently|"
    r"no\s+longer)\s+(?:considering|accepting|open\s+to)[^.]{0,40}remote|"
    r"\bremote\s+(?:work|applications?|candidates?)[^.]{0,24}?\bnot\s+"
    r"(?:be\s+)?(?:considered|accepted|available|an\s+option)|"
    r"\bno\s+remote\b|\bnot\s+a\s+remote\s+role\b")

PERMISSIVE = re.compile(
    r"(?i)\bfully\s+remote\b|\bperformed\s+remotely\b|"
    r"\bremotely\s+from\s+anywhere\b|\bwork\s+from\s+anywhere\b|"
    r"\bremote[- ]first\b|"
    r"\b(?:welcome|welcomes|considering|consider|open\s+to)\b[^.]{0,40}?"
    r"\bremote\b|"
    r"\bremote\s+(?:candidates?|applicants?|work)\b[^.]{0,24}?"
    r"\b(?:welcome|considered|possible|available|fine)\b|"
    r"\bmay\s+(?:also\s+)?(?:be|consider)\b[^.]{0,24}?\bremote\b|"
    r"\bor\s+remote\b")

# "3 days a week" and "3 days in the office per week" are the same
# statement; the words between the count and "week" are why a tight
# pattern silently reported no schedule on the corpus's most common
# sentence.
DAYS = re.compile(
    r"(?i)\b(\d+|one|two|three|four|five|six)\s*days?\b"
    r"(?:[^.]{0,24}?\b(?:a|per)\s*week\b|\s+(?:a|per)\s*week\b)|"
    r"\bin\s+the\s+office\s+(\d+|one|two|three|four|five|six)\s*days?")
WORD_DAYS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}

# Where the place clause ends. Everything here begins a different
# thought: the schedule, the relocation offer, a caveat, a new sentence.
CLAUSE_END = re.compile(
    r"(?i)\.\s|;|\s+with\s+|\s+and\s+(?:we|requires?|offers?|you)\b|"
    r"\s+(?:however|but|although|while)\b|\s+We\s+use\b|\s+and\s+follows?\b|"
    r"\s+at\s+this\s+time\b|\s+on\s+a\s+case\b|\s*[-–—]\s+|"
    r"\s+for\s+at\s+least\b|\s+\d+\s*days?\b")

# Office nouns that qualify a place rather than naming one, and the
# determiners a policy sentence opens with.
OFFICE_NOUN = re.compile(
    r"(?i)\s*\b(?:hq|headquarters|offices?|hubs?|locations?|site)\b\s*$")
LEAD_JUNK = re.compile(
    r"(?i)^(?:in|at|out\s+of|within|from)\s+|^(?:one\s+of\s+)?(?:our|the|its)\s+"
    r"|^(?:either|any\s+of)\s+"
    # "based on-site at our Palo Alto office" -- the arrangement word
    # leads a perfectly real place, so strip it rather than rejecting
    # the whole clause.
    r"|^(?:on-?site|onsite|in-?person|remotely)\s+")

# A trailing comma segment that QUALIFIES the place before it rather
# than naming a new one, so "San Francisco, CA" and "Paris, France"
# survive whole while "San Francisco, Seattle or New York" splits into
# three. Misjudging one only changes the label shown to the owner --
# eligibility matches by substring -- so a modest list is the right
# size for this.
QUALIFIER = re.compile(
    r"(?i)^(?:[A-Z]{2}|U\.?S\.?A?|USA|United\s+States|UK|U\.K\.|"
    r"England|Ireland|France|Germany|Japan|Singapore|Australia|India|"
    r"Canada|Poland|Switzerland|Netherlands|Spain|Italy|Sweden|Brazil|"
    r"Mexico|Israel|South\s+Korea|Korea|China|Taiwan|UAE|Portugal|"
    r"Denmark|Norway|Finland|Belgium|Austria|Greece|New\s+Zealand)$")

# A place clause is a handful of proper nouns. Anything long or carrying
# a verb is prose that happened to follow the word "based". The word cap
# is what rejects "an OpenAI self-build data center campus" (six words,
# 39 characters -- comfortably inside the character cap, and nothing a
# city ever looks like); the longest real names here are four, like
# "San Francisco Bay Area" and "New York, New York".
MAX_PLACE = 44
MAX_PLACE_WORDS = 5
NOT_A_PLACE = re.compile(
    r"(?i)\b(?:you|we|your|our\s+team|experience|candidate|role|work|"
    r"team|company|please|apply|position|salary|which|that|this)\b|\d")

# Words that describe the ARRANGEMENT, not the place. "This role is
# based on-site, five days a week" names no city at all, and reading
# "on-" and "five days a week" as offices would bind the role to
# nowhere real.
NOT_A_PLACE_WORD = re.compile(
    r"(?i)^(?:on|on-?site|onsite|in-?person|remote(?:ly)?|hybrid|"
    r"anywhere|home|flexible)$|\bdays?\b|\bweek\b|"
    r"\b(?:campus|centre|center|facility|building|premises)\b")


def _clean(text):
    """Markdown out, whitespace flattened. The corpus is full of
    `**This role is based in...**` and `## Workplace & Location`."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"[*_`#>]+", " ", text)
    # Curly punctuation to straight, so the contraction patterns above
    # match the spelling employers actually publish.
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip()


def _days(window):
    m = DAYS.search(window)
    if not m:
        return None
    raw = m.group(1) or m.group(2) or ""
    raw = raw.strip().lower()
    if raw.isdigit():
        n = int(raw)
    else:
        n = WORD_DAYS.get(raw)
    return n if n and 1 <= n <= 7 else None


def _split_places(clause):
    """A place clause -> the individual places it names."""
    # Parenthesised content is either the real list ("our European
    # offices (Paris, France and London, UK)") or a schedule ("our New
    # York City office (5 days per week)"). Digits tell them apart.
    paren = re.search(r"\(([^)]{2,80})\)", clause)
    if paren and not re.search(r"\d", paren.group(1)):
        clause = paren.group(1)
    else:
        clause = re.sub(r"\([^)]*\)", " ", clause)

    parts = [p for p in re.split(r"\s+or\s+|\s+and\s+|/|,", clause) if p.strip()]
    out = []
    for part in parts:
        p = part.strip().strip(".,;:–—- ")
        if not p:
            continue
        if QUALIFIER.match(p) and out:
            out[-1] = f"{out[-1]}, {p}"     # attach a state or country
            continue
        # "in our San Francisco HQ" stacks a preposition on a
        # determiner, so one pass leaves "our San Francisco" behind.
        while True:
            stripped = LEAD_JUNK.sub("", p).strip()
            if stripped == p:
                break
            p = stripped
        # "our European offices" names a region, not a place; drop the
        # noun and keep whatever remains only if it is a real name.
        # Trailing hyphen matters: stripping "site" off "on-site" leaves
        # "on-", which no reject pattern would recognise as the fragment
        # it is.
        p = OFFICE_NOUN.sub("", p).strip().strip(".,;:-– ")
        if not p or len(p) > MAX_PLACE or NOT_A_PLACE.search(p):
            continue
        if len(p.split()) > MAX_PLACE_WORDS or NOT_A_PLACE_WORD.search(p):
            continue
        if not re.search(r"[A-Za-z]", p):
            continue
        if p.lower() in ("european", "us", "u.s.", "american", "global",
                         "remote", "one", "either", "regional"):
            continue
        if p not in out:
            out.append(p)
    return out


def _places(window):
    """The offices a residency window names, in the order it names
    them. Empty when the sentence binds a schedule but no city."""
    m = re.search(r"(?i)\bbased\b\s*", window)
    if not m:
        return []
    tail = window[m.end():]
    cut = CLAUSE_END.search(tail)
    clause = tail[:cut.start()] if cut else tail[:120]
    return _split_places(clause)


def read(jd):
    """The workplace policy a description states about itself.

    Returns None when the body says nothing confident -- which is the
    common case and must stay cheap to act on. Otherwise:

        mode      "remote" | "hybrid" | "onsite"
        days      in-office days per week, when the body states a number
        places    the offices the body names (may be empty: a schedule
                  can bind a role without naming a city)
        remote_ok whether the body leaves a remote path open
        binds     True when this reading should override a board flag --
                  i.e. the body rules remote out. The one field callers
                  need to decide anything.
        quote     the sentence, so a surface can show its own evidence
    """
    text = _clean(jd)
    if not text:
        return None

    m = TRIGGER.search(text)
    kind = "residency"
    if not m:
        m = SCHEDULE.search(text)
        kind = "schedule"
    if not m:
        return None

    window = text[m.start():m.start() + WINDOW]
    refused = bool(REFUSAL.search(window))
    permissive = (not refused) and bool(PERMISSIVE.search(window))
    days = _days(window)
    places = _places(window) if kind == "residency" else []

    if permissive:
        mode, remote_ok = "remote", True
    elif days or re.search(r"(?i)hybrid", window):
        mode, remote_ok = "hybrid", False
    else:
        mode, remote_ok = "onsite", False

    # A residency sentence that names nowhere, states no schedule and
    # refuses nothing is not evidence of anything -- "this role is based
    # on the West Coast team" and similar prose land here.
    if kind == "residency" and not places and not days and not refused \
            and mode != "remote":
        return None

    quote = window.strip()
    cut = re.search(r"(?<=[.!?])\s+(?=[A-Z])", quote[80:])
    if cut:
        quote = quote[:80 + cut.start()]

    return {
        "mode": mode,
        "days": days,
        "places": places,
        "remote_ok": remote_ok,
        "binds": not remote_ok,
        "quote": quote[:240],
    }


def label(wp):
    """A short human line for a row: 'San Francisco, CA - hybrid, 3
    days/week'. Empty when there is nothing worth saying."""
    if not wp:
        return ""
    where = " / ".join(wp.get("places") or [])
    if wp.get("mode") == "remote":
        return f"remote ok{' - ' + where if where else ''}"
    bits = []
    if where:
        bits.append(where)
    if wp.get("days"):
        bits.append(f"{'hybrid' if wp['mode'] == 'hybrid' else 'onsite'}, "
                    f"{wp['days']} days/week")
    elif wp.get("mode") == "hybrid":
        bits.append("hybrid")
    elif where:
        bits.append("office-based")
    return " - ".join(bits)


# Deliberately a local copy of jobboards.REMOTE_RE: jobboards imports
# this module, so reaching back for it would be a cycle.
_REMOTE_LOC = re.compile(r"(?i)\bremote\b")

# Tokens that never distinguish one city from another, so matching on
# them alone would call New York and New Orleans the same place.
_WEAK = {"new", "the", "of", "our", "us", "usa", "united", "states",
         "city", "area", "metro", "greater", "downtown", "north",
         "south", "east", "west", "saint", "st"}


def _tokens(place):
    words = re.sub(r"[^a-z0-9]+", " ", str(place).lower()).split()
    return {w for w in words if len(w) > 1 and w not in _WEAK}


def _same_place(a, b):
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta and tb and ta & tb)


def allows(wp, places_rx, locations=None):
    """Whether a bound policy can be worked from the owner's places.

    True when the body does not bind, when it names no office (the
    board's own location strings stay the authority then), or when one
    of the offices it names matches the rule. `places_rx` is the
    compiled rule from jobboards.location_rule().

    WHEN A POSTING EXPLICITLY NAMES A CITY THE OWNER WORKS IN, that
    claim stands unless the body corroborates a DIFFERENT published
    city. Three measured cases decide the shape, and no simpler rule
    gets all three right:

    - Location "US - Remote", body "based in San Francisco, CA, hybrid
      3 days a week". Nothing published is a city at all, so the role's
      eligibility rested entirely on a remote tag the body contradicts.
      REFUSE -- this is the case the module exists for.
    - Locations "San Francisco / New York City / Seattle", body
      "exclusively based in our San Francisco HQ". A published city
      matches the owner, but the body corroborates one of the others and
      says it is the only real one. REFUSE -- that is what narrowing is.
    - Location "NYC", body "based in our SoHo office" (Hebbia's "AI
      Strategist, Corporate Law"). SoHo matches no New York rule and
      corroborates nothing published -- because it is the SAME office at
      a finer granularity, not a different one. ALLOW; vetoing here
      would hide a real New York job.

    The asymmetry is deliberate: showing a San Francisco job the owner
    will skip costs a glance, and hiding a New York job costs the job.
    """
    if not wp or not wp.get("binds"):
        return True
    named = wp.get("places") or []
    locations = locations or []
    if places_rx is None:
        return True

    if not named:
        # A schedule with no office named ("we use a hybrid work model of
        # 3 days in the office per week") still binds the role to
        # whatever the posting itself lists -- you cannot be in an office
        # three days a week from another city. The remote tag is exactly
        # what that contradicts, so the on-site locations are what count.
        onsite = [l for l in locations if not _REMOTE_LOC.search(str(l))]
        if not onsite:
            return True          # nothing to bind to; do not guess
        return any(places_rx.search(str(l)) for l in onsite)

    if any(places_rx.search(p) for p in named):
        return True
    if not any(places_rx.search(str(loc)) for loc in locations):
        return False        # eligibility rested on a remote tag alone
    return not any(_same_place(p, loc) for p in named for loc in locations)
