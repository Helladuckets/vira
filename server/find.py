"""The sorter: is a typed query a database question or a meaning
question, and which of the four corpora answers it.

Vira holds four private databases — the media index (photos, videos,
docs, links), the vault notes (qocha), the CRM (people), and message
and mail bodies — and until now had two search boxes with no agreement
about which query belonged where. Worse, one whole class of question
was unanswerable everywhere: "the most recent session where we decided
X" is a filter-and-sort, not a similarity ranking, and a top-k RAG can
only refuse it.

So the split that matters is NOT keyword vs semantic — both engines
already run FTS5 and vectors together and fuse them with RRF
(retrieval.py). It is:

  deterministic query   filter, sort, count, enumerate — SQL answers it
  similarity retrieval  rank by meaning — vectors answer it

plan() decides, in two rungs:

  rung 1  pure heuristics, no model, no network: dates, superlatives,
          totality, quoted phrases, filenames, phone and email, the
          from:/db:/kind: operators, CRM names. Safe on every keystroke.
  rung 2  the model (plan_llm), only for question-shaped input the user
          committed with Enter — and a model failure degrades to the
          rung-1 plan rather than erroring.

Whatever the rung, the filters are applied as SQL by each corpus BEFORE
ranking, so ordering and date windows are honoured rather than hoped
for. run() fans the plan out concurrently and returns the groups
SEPARATELY: an RRF score from the media index and one from the vault
are not comparable, and blending them buries exact hits.
"""
import concurrent.futures as futures
import json
import re
from datetime import date, datetime, timedelta
from datetime import time as dtime

from . import data as crm

DATABASES = ("notes", "media", "people", "messages")

# media index kinds, keyed by the words people actually type
KIND_WORDS = {
    "photo": ("photo", "photos", "picture", "pictures", "pic", "pics",
              "image", "images", "screenshot", "screenshots", "selfie"),
    "video": ("video", "videos", "clip", "clips", "movie", "footage"),
    "doc": ("doc", "docs", "document", "documents", "pdf", "pdfs", "deck",
            "decks", "spreadsheet", "resume", "contract"),
    "link": ("link", "links", "url", "urls", "article", "articles",
             "website", "site"),
    "audio": ("audio", "recording", "recordings", "voicemail", "memo",
              "memos"),
}

NOTE_WORDS = ("note", "notes", "session", "sessions", "retro", "retros",
              "wiki", "vault", "decided", "decision", "decisions", "plan",
              "plans", "brief", "briefs", "brain")
MSG_WORDS = ("text", "texted", "message", "messages", "messaged", "said",
             "say", "says", "tell", "told", "wrote", "write", "email",
             "emails", "emailed", "thread", "conversation", "chat", "reply",
             "replied", "mention", "mentioned", "talked", "talking")
PEOPLE_WORDS = ("contact", "contacts", "person", "people", "number",
                "phone", "address", "works", "company", "relationship")

MONTHS = {m: i for i, m in enumerate(
    ("january february march april may june july august september "
     "october november december").split(), start=1)}
MONTH_ABBR = {m[:3]: i for m, i in MONTHS.items()}
ALL_MONTHS = {**MONTH_ABBR, **MONTHS}
MONTH_ALT = "|".join(sorted(ALL_MONTHS, key=len, reverse=True))
# "may" is an ordinary verb and "mar"/"jan" are names, so a bare month
# token only reads as a date with a preposition or a year beside it
SAFE_MONTH = {m for m in MONTHS if m != "may"}

INTERROGATIVES = ("who", "what", "when", "where", "why", "which", "how",
                  "did", "does", "do", "is", "are", "was", "were", "can",
                  "could", "should", "would", "will", "have", "has", "had")

VERBS = (r"\b(sent|send|told|said|wrote|decided|talked|discussed|"
         r"mentioned|asked|agreed|shared|needed|built|shipped)\b")

# stripped from the residual before ranking. bm25 over "what did we say
# about the boat" scores every message containing "the"; over "boat" it
# scores the ones about boats. Vectors barely notice either way.
STOP = {
    "a", "about", "all", "am", "an", "and", "any", "anything", "are", "as",
    "at", "be", "been", "but", "by", "can", "could", "did", "do", "does",
    "for", "from", "get", "got", "had", "has", "have", "he", "her", "him",
    "his", "how", "i", "if", "in", "into", "is", "it", "its", "just", "me",
    "my", "of", "on", "one", "or", "our", "out", "over", "she", "should",
    "so", "some", "that", "the", "their", "them", "then", "there", "these",
    "they", "this", "those", "to", "up", "us", "was", "we", "were", "what",
    "when", "where", "which", "who", "whom", "why", "will", "with", "would",
    "you", "your",
}

# single-token first-name matching is the risky one: the CRM carries
# ordinary words as names (a "Client", a "Delivery"), so a bare token
# only resolves when it is not filler, not vocabulary this module
# already reads, and points at one person. Three letters is the floor —
# Kim, Sam, Bob and Joe are real contacts, not noise.
NAME_STOP = set(NOTE_WORDS) | set(MSG_WORDS) | set(PEOPLE_WORDS) | set(
    ALL_MONTHS) | STOP | {w for words in KIND_WORDS.values() for w in words} | {
    "ago", "did", "far", "few", "let", "lot", "new", "not", "now", "off",
    "old", "own", "put", "run", "see", "set", "too", "top", "try", "two",
    "use", "via", "way", "yes", "yet",
    "about", "after", "again", "against", "already", "also", "back",
    "been", "before", "being", "between", "both", "client", "clients",
    "company", "delivery", "down", "during", "each", "even", "ever",
    "every", "family", "first", "from", "group", "here", "home", "into",
    "just", "last", "like", "made", "make", "management", "many", "more",
    "most", "much", "must", "need", "never", "next", "office", "only",
    "other", "over", "recent", "same", "send", "sent", "some", "still",
    "such", "team", "than", "that", "them", "then", "there", "these",
    "they", "this", "those", "through", "time", "under", "very", "want",
    "week", "well", "were", "what", "when", "where", "which", "while",
    "with", "work", "year", "your",
}

FILE_RE = re.compile(
    r"\b[\w-]+\.(?:pdf|docx?|xlsx?|pptx?|pages|numbers|key|csv|txt|md|zip"
    r"|png|jpe?g|heic|gif|webp|mov|mp4|m4a|caf|amr|heif)\b", re.I)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
ID_RE = re.compile(r"\b(?:p_[0-9a-f]{6,}|IMG_\d+|[A-Z]{2,}_\d{3,})\b")
QUOTED_RE = re.compile(r"[\"“]([^\"”]{2,})[\"”]|'([^']{4,})'")
OP_RE = re.compile(r"\b(from|to|db|kind|source|since|until|is):(\S+)", re.I)


def _blank_plan(raw):
    return {
        "raw": raw,
        "text": raw,
        "databases": list(DATABASES),
        "primary": None,
        "filters": {"since": None, "until": None, "person": None,
                    "sender": None, "direction": None, "kind": None,
                    "source": None, "order": "relevance",
                    "limit_all": False, "exact": False, "phrases": []},
        "shape": "list",
        "why": "",
        "rung": 1,
    }


# ---------- rung 1: the deterministic sorter ----------

def _cut(text, span):
    """Drop a matched span, leaving the residual query readable: what is
    left is what the ranking layers actually see."""
    return text[:span[0]] + " " + text[span[1]:]


def _month_window(month, year):
    start = date(year, month, 1)
    end = date(year + (month == 12), (month % 12) + 1, 1)
    return start.isoformat(), end.isoformat()


def _month_year(token, year, today):
    mo = ALL_MONTHS[token.lower()]
    if year:
        return _month_window(mo, int(year))
    # a month with no year means the most recent one that has actually
    # happened: in July, "last March" is this year's March
    yr = today.year if mo <= today.month else today.year - 1
    return _month_window(mo, yr)


def _dates(text, today=None):
    """(since, until, residual). ISO strings out; every corpus converts
    to its own clock. Dates are read BEFORE superlatives so "last March"
    lands as a month, not as "latest"."""
    today = today or date.today()

    m = re.search(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", text)
    if m:
        d = date(int(m[1]), int(m[2]), int(m[3]))
        return d.isoformat(), (d + timedelta(days=1)).isoformat(), \
            _cut(text, m.span())

    m = re.search(r"(?<!\d)(\d{4})-(\d{2})(?!\d)", text)
    if m:
        s, u = _month_window(int(m[2]), int(m[1]))
        return s, u, _cut(text, m.span())

    # "since June", "after March 2025", "before 2024"
    m = re.search(r"\b(since|after|before|until)\s+(" + MONTH_ALT +
                  r"|\d{4})\b(?:\s+(\d{4}))?", text, re.I)
    if m:
        word = m[2].lower()
        if word in ALL_MONTHS:
            s, u = _month_year(word, m[3], today)
            anchor = s
        else:
            anchor = date(int(word), 1, 1).isoformat()
        residual = _cut(text, m.span())
        if m[1].lower() in ("since", "after"):
            return anchor, None, residual
        return None, anchor, residual

    m = re.search(r"\b(?:past|last)\s+(\d+)\s+(day|week|month|year)s?\b",
                  text, re.I)
    if m:
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[m[2].lower()]
        start = today - timedelta(days=days * int(m[1]))
        return start.isoformat(), None, _cut(text, m.span())

    m = re.search(r"\b(today|yesterday)\b", text, re.I)
    if m:
        d = today if m[1].lower() == "today" else today - timedelta(days=1)
        return d.isoformat(), (d + timedelta(days=1)).isoformat(), \
            _cut(text, m.span())

    m = re.search(r"\b(this|last)\s+(week|month|year)\b", text, re.I)
    if m:
        which, unit = m[1].lower(), m[2].lower()
        residual = _cut(text, m.span())
        if unit == "week":
            start = today - timedelta(days=today.weekday()
                                      + (7 if which == "last" else 0))
            return start.isoformat(), \
                (start + timedelta(days=7)).isoformat(), residual
        if unit == "month":
            mo, yr = today.month, today.year
            if which == "last":
                mo, yr = (12, yr - 1) if mo == 1 else (mo - 1, yr)
            s, u = _month_window(mo, yr)
            return s, u, residual
        yr = today.year - (which == "last")
        return date(yr, 1, 1).isoformat(), date(yr + 1, 1, 1).isoformat(), \
            residual

    # "in March", "last March", "March 2025", bare "October"
    m = re.search(r"\b(last\s+|in\s+|during\s+)?(" + MONTH_ALT + r")\b"
                  r"(?:\s+(\d{4}))?", text, re.I)
    if m and (m[1] or m[3] or m[2].lower() in SAFE_MONTH):
        s, u = _month_year(m[2], m[3], today)
        return s, u, _cut(text, m.span())

    # A bare year — but a phone number ends in four digits that look
    # exactly like one, in any punctuation style, so a query carrying a
    # phone number has no bare-year reading at all.
    m = None if PHONE_RE.search(text) else \
        re.search(r"(?<![\d-])(?:19|20)\d{2}(?![\d-])", text)
    if m:
        yr = int(m[0])
        return date(yr, 1, 1).isoformat(), date(yr + 1, 1, 1).isoformat(), \
            _cut(text, m.span())

    return None, None, text


def _name_maps():
    """{full name: pid} and {first name: [pids]}, rebuilt only when the
    CRM cache reloads underneath us."""
    c = crm._load()
    stamp = c.get("loaded_at")
    cached = _name_maps.cache
    if cached and cached[0] == stamp:
        return cached[1], cached[2]
    full, first = {}, {}
    for pid, p in c["by_id"].items():
        name = (p.get("name") or "").strip()
        if not name or name.endswith("(unidentified)"):
            continue
        full[name.lower()] = pid
        act = p.get("activity", {}) or {}
        first.setdefault(name.split()[0].lower(), []).append(
            (act.get("imsg_n", 0) + act.get("email_n", 0), pid))
    for head, rows in first.items():
        rows.sort(reverse=True)
    _name_maps.cache = (stamp, full, first)
    return full, first


_name_maps.cache = None


def _names(text):
    """(pid or None, residual). Full names match anywhere. A bare first
    name is riskier — the CRM is full of names that are also nouns — so
    it only resolves when the token is long, not ordinary vocabulary,
    and points at ONE person: either the only holder of that name, or
    the one who dominates it by traffic (four Alexes, but 727 messages
    against 38 says which one you meant). A tie leaves it unresolved
    rather than guessing, and the plan chip shows whoever won."""
    try:
        full, first = _name_maps()
    except Exception:      # noqa: BLE001 — an absent CRM is not an error
        return None, text
    low = text.lower()
    for name, pid in sorted(full.items(), key=lambda kv: -len(kv[0])):
        if len(name) > 4 and name in low:
            i = low.index(name)
            return pid, _cut(text, (i, i + len(name)))
    for m in re.finditer(r"[A-Za-z']{3,}", text):
        t = m[0].lower()
        if t in NAME_STOP:
            continue
        rows = first.get(t)
        if not rows:
            continue
        if len(rows) == 1 or rows[0][0] >= max(3 * rows[1][0], 1):
            return rows[0][1], _cut(text, m.span())
    return None, text


def _operators(text, f):
    """from:/to:/db:/kind:/source:/since:/until:/is: — the explicit escape
    hatch, and the only signal that narrows the database set outright."""
    explicit_db = None
    # right to left: cutting a span invalidates every span after it
    for m in reversed(list(OP_RE.finditer(text))):
        key, val = m[1].lower(), m[2].strip().strip(",")
        if key == "from":
            f["sender"] = val
        elif key == "to":
            f["direction"] = "sent"
        elif key == "db" and val.lower() in DATABASES:
            explicit_db = val.lower()
        elif key == "kind" and val.lower() in KIND_WORDS:
            f["kind"] = [val.lower()]
        elif key == "source":
            f["source"] = val.lower()
        elif key in ("since", "until"):
            f[key] = val
        elif key == "is" and val.lower() in ("sent", "received"):
            f["direction"] = val.lower()
        text = _cut(text, m.span())
    return text, explicit_db


def _exactness(text, f):
    """Quoted phrases, filenames, phone numbers, emails and ids all mean
    the same thing: the user knows the literal string. The FTS layer gets
    the weight and the vector floor rises, so fuzzy neighbours stop
    crowding out the hit the user already named."""
    phrases = [g for m in QUOTED_RE.finditer(text) for g in m.groups() if g]
    if phrases:
        f["phrases"] = phrases
        f["exact"] = True
    if any(rx.search(text) for rx in (FILE_RE, EMAIL_RE, ID_RE, PHONE_RE)):
        f["exact"] = True
    return text


def _order(text, f):
    m = re.search(r"\b(most recent|latest|newest|recently)\b", text, re.I)
    if m:
        f["order"] = "recent"
        return _cut(text, m.span())
    m = re.search(r"\b(oldest|earliest|the first)\b", text, re.I)
    if m:
        f["order"] = "oldest"
        return _cut(text, m.span())
    return text


def _totality(text, f):
    if re.search(r"\b(how many|count of|number of|all|every|everything)\b",
                 text, re.I):
        f["limit_all"] = True
    return text


def _kind(text, f):
    if f["kind"]:
        return
    low = " " + text.lower() + " "
    for kind, words in KIND_WORDS.items():
        if any(" " + w + " " in low for w in words):
            f["kind"] = [kind]
            return


def _rank_databases(text, raw, f, explicit_db, shape):
    """Signals REORDER the corpora, they do not exclude them — only an
    explicit db: narrows the set. Every search still reaches all four,
    which is the whole point of the merge.

    With no signal at all the tiebreak is the shape: a bare phrase is
    most often someone reaching for a thing they were sent, and a
    question is best served by the corpus that answers with citations.
    """
    if explicit_db:
        return [explicit_db]
    default = ("notes", "media", "people", "messages") if shape == "answer" \
        else ("media", "notes", "messages", "people")
    low = " " + text.lower() + " "
    score = dict.fromkeys(DATABASES, 0)
    for w in NOTE_WORDS:
        if " " + w + " " in low:
            score["notes"] += 2
    for w in MSG_WORDS:
        if " " + w + " " in low:
            score["messages"] += 3
    for w in PEOPLE_WORDS:
        if " " + w + " " in low:
            score["people"] += 2
    if f["kind"]:
        score["media"] += 3
    if f["person"]:
        # a bare name is a question ABOUT someone; a name plus content
        # words means the content is the point and they are the filter
        score["people"] += 3 if not text.strip() else 1
    if FILE_RE.search(raw):
        score["media"] += 3
        score["messages"] += 1
    if f["direction"] or f["sender"]:
        score["messages"] += 1
        score["media"] += 1
    if PHONE_RE.search(raw) or EMAIL_RE.search(raw):
        score["people"] += 3
    return sorted(default, key=lambda d: (-score[d], default.index(d)))


def _shape(raw):
    """A question gets an answer; a phrase gets a list. Only the answer
    shape is allowed to spend a model call, and only on Enter."""
    words = raw.strip().split()
    if not words:
        return "list"
    if raw.strip().endswith("?"):
        return "answer"
    if words[0].lower().strip(",") in INTERROGATIVES:
        return "answer"
    if len(words) >= 8 and re.search(VERBS, raw, re.I):
        return "answer"
    return "list"


def _why(p):
    """The chip the UI shows. The sorter's decision has to be visible and
    dismissible — a router that guesses silently is worse than no
    router."""
    f = p["filters"]
    bits = []
    if f["order"] != "relevance":
        bits.append("newest first" if f["order"] == "recent"
                    else "oldest first")
    if f["since"] and f["until"]:
        bits.append(f"{f['since']} to {f['until']}")
    elif f["since"]:
        bits.append("since " + f["since"])
    elif f["until"]:
        bits.append("before " + f["until"])
    if f["person"]:
        who = crm._load()["by_id"].get(f["person"])
        bits.append(who["name"] if who else f["person"])
    if f["kind"]:
        bits.append("/".join(f["kind"]))
    if f["direction"]:
        bits.append(f["direction"])
    if f["exact"]:
        bits.append("exact match")
    if f["limit_all"]:
        bits.append("all matches")
    if len(p["databases"]) == 1:
        bits.append(p["databases"][0])
    elif p["primary"]:
        bits.append(p["primary"] + " first")
    return " · ".join(bits)


def plan(q, today=None):
    """Rung 1. Pure heuristics: no model, no network, safe to call on
    every keystroke."""
    raw = (q or "").strip()
    p = _blank_plan(raw)
    if not raw:
        return p
    f = p["filters"]
    text = _exactness(raw, f)
    text, explicit_db = _operators(text, f)
    if not (f["since"] or f["until"]):
        f["since"], f["until"], text = _dates(text, today=today)
    text = _order(text, f)
    text = _totality(text, f)
    _kind(text, f)
    if not f["sender"]:
        f["person"], text = _names(text)
    if re.search(r"\b(i sent|did i send|i shared|from me)\b", raw, re.I):
        f["direction"] = "sent"
    elif re.search(r"\b(sent me|send me|shared with me)\b", raw, re.I):
        f["direction"] = "received"
    p["shape"] = _shape(raw)
    p["databases"] = _rank_databases(text, raw, f, explicit_db, p["shape"])
    p["primary"] = p["databases"][0]
    p["text"] = _terms(text)
    p["why"] = _why(p)
    return p


def _terms(text):
    """The residual, minus filler. Everything the sorter consumed is
    already gone; this drops what would only add noise to bm25. If the
    query was ALL filler ("what is this about"), the words come back —
    a search with no terms is worse than a vague one."""
    text = re.sub(r"\s+", " ", text).strip(" ,.")
    kept = [w for w in text.split() if w.lower().strip(",.?!'\"") not in STOP]
    return " ".join(kept) if kept else text


# ---------- rung 2: the model, for questions only ----------

PLAN_PROMPT = """You translate a question about a personal knowledge system into a JSON retrieval plan. Four databases are available:

- notes: the owner's second-brain vault (wiki pages, session retros, briefs, decisions)
- media: photos, videos, documents, voice memos and links shared over iMessage and email
- people: the CRM (contacts, relationships, companies, facts, open loops)
- messages: the text of iMessage conversations and email bodies

People the owner knows (id: name):
{people}

Today is {today}.
Question: {question}

Reply with ONLY a JSON object, no prose:
{{
 "databases": ["<one or more of notes, media, people, messages, best first>"],
 "person": "<person id whose material to search, or null>",
 "sender": "<person id if the question says THEY sent it; 'me' if the owner did; else null>",
 "direction": "<'received'|'sent'|null>",
 "kind": "<'photo'|'video'|'doc'|'audio'|'link'|null>",
 "face_person": "<person id of someone who should APPEAR IN a picture, or null>",
 "since": "<YYYY-MM-DD or null>",
 "until": "<YYYY-MM-DD or null>",
 "order": "<'recent' when the question asks for the latest or most recent, 'oldest' for the earliest, else 'relevance'>",
 "query": "<the content words to match on, or null>",
 "wants": "<one line: what would count as the answer>"
}}
Rules: dates belong in since/until, never in query. Omit people's names from query when person, sender or face_person already covers them. face_person is only for people visible in an image, never the sender. Prefer fewer databases when the question clearly names one."""


def plan_llm(q, today=None):
    """Rung 2. Only for question-shaped input the user committed to. A
    dead backend is not an error here: the rung-1 plan still stands."""
    base = plan(q, today=today)
    try:
        from .search import _people_for_prompt
        from .suggest import complete
        raw = complete(PLAN_PROMPT.format(
            people=_people_for_prompt(), question=q,
            today=(today or date.today()).isoformat()))
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            return base
        got = json.loads(m.group(0))
    except Exception:      # noqa: BLE001 — degrade, never fail the search
        return base

    p = dict(base, rung=2)
    f = dict(base["filters"])
    dbs = [d for d in (got.get("databases") or []) if d in DATABASES]
    if dbs:
        p["databases"] = dbs
        p["primary"] = dbs[0]
    for key in ("person", "sender", "direction", "since", "until",
                "face_person"):
        if got.get(key):
            f[key] = got[key]
    if got.get("kind") in KIND_WORDS:
        f["kind"] = [got["kind"]]
    if got.get("order") in ("recent", "oldest", "relevance"):
        f["order"] = got["order"]
    if got.get("query"):
        p["text"] = got["query"]
    p["filters"] = f
    p["wants"] = got.get("wants") or ""
    p["why"] = _why(p)
    return p


# ---------- the corpus adapters ----------
# Each takes the plan and returns {rows, count, ...}. Filters are applied
# by the corpus in SQL before ranking; nothing is re-sorted here.

def _iso_from_epoch(ts):
    return datetime.fromtimestamp(ts).isoformat() if ts else None


def _epoch(iso):
    if not iso:
        return None
    try:
        return datetime.combine(date.fromisoformat(str(iso)[:10]),
                                dtime.min).timestamp()
    except ValueError:
        return None


def a_notes(p, limit):
    """One row per NOTE, not per chunk: a long note whose every section
    mentions the query would otherwise fill the group with itself. The
    best-ranked chunk wins and its heading becomes the row's context;
    the ask still sees the full hit list, chunks and all."""
    from . import vault
    f = p["filters"]
    hits = vault.search_filtered(p["text"], limit=limit * 3, since=f["since"],
                                 until=f["until"], order=f["order"])
    rows, seen = [], set()
    for h in hits:
        if h["path"] in seen:
            continue
        seen.add(h["path"])
        rows.append({"path": h["path"], "title": h["title"] or h["path"],
                     "heading": h.get("heading") or "",
                     "snippet": (h.get("text") or "")[:320],
                     "when": _iso_from_epoch(h.get("mtime")),
                     "score": h.get("score")})
        if len(rows) >= limit:
            break
    return {"rows": rows, "count": len(rows), "hits": hits[:limit]}


def a_media(p, limit):
    from . import search as msearch
    f = p["filters"]
    rows = msearch.search(
        q=p["text"] or None, pid=f["person"], sender_pid=f["sender"],
        kind=f["kind"], direction=f["direction"],
        face_pid=f.get("face_person"),
        since=_apple_ns(f["since"]), until=_apple_ns(f["until"]),
        limit=limit, exact=f["exact"])
    return {"rows": rows, "count": len(rows)}


def a_people(p, limit):
    from . import crmindex
    f = p["filters"]
    rows = crmindex.search(p["text"], limit=limit, exact=f["exact"],
                           person=f["person"], order=f["order"])
    return {"rows": rows, "count": len(rows)}


def a_messages(p, limit):
    try:
        from . import textindex
    except ImportError:                      # minimal install
        return {"rows": [], "count": 0, "note": "not installed"}
    if not textindex.available():
        return {"rows": [], "count": 0, "note": "not indexed yet"}
    f = p["filters"]
    rows = textindex.search(
        p["text"], limit=limit, person=f["person"], sender=f["sender"],
        direction=f["direction"], source=f["source"], since=f["since"],
        until=f["until"], order=f["order"], exact=f["exact"])
    return {"rows": rows, "count": len(rows)}


ADAPTERS = {"notes": a_notes, "media": a_media, "people": a_people,
            "messages": a_messages}


def _apple_ns(iso):
    """ISO date -> Apple epoch nanoseconds (the media index's clock)."""
    ts = _epoch(iso)
    if ts is None:
        return None
    from .imessage import apple_ns
    return apple_ns(datetime.fromtimestamp(ts))


# ---------- the fan-out ----------

def run(p, limit=20):
    """Query every database in the plan concurrently. Groups stay
    separate — see the module docstring on why nothing is fused. One
    dead corpus reports its error and never kills the whole search."""
    groups = {}
    dbs = [d for d in p["databases"] if d in ADAPTERS]
    with futures.ThreadPoolExecutor(max_workers=max(1, len(dbs))) as pool:
        jobs = {db: pool.submit(ADAPTERS[db], p, limit) for db in dbs}
        for db, job in jobs.items():
            try:
                groups[db] = job.result(timeout=30)
            except Exception as e:      # noqa: BLE001
                groups[db] = {"rows": [], "count": 0, "error": str(e)[:200]}
    return {"plan": p, "groups": groups,
            "counts": {db: g.get("count", 0) for db, g in groups.items()}}


def find(q, limit=20, today=None):
    """The list path: rung 1 only, so typing never costs a model call."""
    return run(plan(q, today=today), limit=limit)


def ask(question, limit=8, today=None):
    """The answer path: rung 2, then the owning corpus's own ask.

    The two ask contracts stay distinct on purpose (the 2026-07-21 audit
    call: converge the engine, not the contracts) — notes answers are
    grounded and cited, media answers relax constraints and narrate the
    near-misses. Both now answer over the FILTERED hit set, so the prose
    can no longer contradict the list beside it.
    """
    p = plan_llm(question, today=today)
    out = run(p, limit=limit)
    out["answer"] = None
    out["citations"] = []
    primary = p["primary"]
    try:
        if primary == "notes":
            from . import vault
            hits = out["groups"].get("notes", {}).get("hits") or None
            got = vault.ask(question, hits=hits)
            out["answer"] = got.get("answer")
            out["citations"] = got.get("citations") or []
        elif primary == "media":
            from . import search as msearch
            got = msearch.ask(question, plan=_media_plan(p))
            out["answer"] = got.get("answer")
            out["relaxed"] = got.get("relaxed") or []
            if got.get("results"):
                out["groups"]["media"] = {"rows": got["results"],
                                          "count": len(got["results"])}
    except Exception as e:      # noqa: BLE001 — the lists still stand
        out["answer_error"] = str(e)[:200]
    return out


def _media_plan(p):
    """Our plan in search.ask()'s own vocabulary, so the media ask reuses
    the parse we already paid for instead of making a second call."""
    f = p["filters"]
    return {"person": f["person"], "sender": f["sender"],
            "direction": f["direction"],
            "kind": (f["kind"] or [None])[0],
            "face_person": f.get("face_person"),
            "since": f["since"], "until": f["until"],
            "query": p["text"] or None, "wants": p.get("wants", "")}
