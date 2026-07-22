"""Radar — who deserves attention, and who should meet whom.

Two engines, both explainable:

priority_people() — a deterministic ranking of who the owner should talk
to next, scored from live signals (each row carries its reasons): owed
replies (chat.db), going-quiet decay on active-tier contacts, stale open
loops weighted toward what the OWNER owes, birthdays inside a week,
available conversation hooks (something to actually say), and live
conversation MARKERS — a thing that just landed and this one person
cares about it. Reuses the Daily Brief's loaders; same freshness, same
cost profile (~100ms), and markers come off the cached store rather than
the wire.

groupings — the connector engine, sized by audience rather than fixed at
pairs. Profile text (summary, hooks, facts, company/title) is tokenized
per person and INVERTED: a rare token maps straight to everyone carrying
it, so a topic four people share surfaces as one grouping instead of six
disconnected pairs. A pair is just the degenerate case.

Two triggers feed the same candidate shape:

  overlap — the standing signal: rare-but-shared profile tokens across
            the ~120 most active contacts, weighted by token rarity.
  event   — what just happened, read locally: links the owner's contacts
            actually shared in chat.db over the last few weeks, matched
            against the same fingerprints. An item whose audience is
            exactly one person is not a grouping at all — it becomes a
            marker on that person's row.

Candidates are then annotated deterministically — the Contact Atlas says
whether the members already know each other, chat.db says whether a
group thread already covers them — which is what picks the move (post to
the thread you have, start a group chat, make an introduction). ONE AI
pass then curates the survivors into named topics with an opener.

Cached in data/radar-groupings.json (the grouping-scout routine refreshes
weekly; a button refreshes on demand); dismissals persist, and a legacy
data/radar-intros.json seeds the store once so old dismissals carry over.
"""
import datetime as dt
import json
import re
import sqlite3
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import brief
from . import data as crm
from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "radar-groupings.json"
LEGACY = ROOT / "data" / "radar-intros.json"

TOP_ACTIVE = 120          # most-active contacts considered for groupings
# The curator is one CLI call against a wall-clock timeout, and both the
# prompt and the replies it writes scale with this. Forty candidates ran
# past 120s and silently fell back to raw matches; twenty lands in ~85s
# and the model was never weighing forty rooms seriously anyway.
MAX_CANDIDATES = 20       # candidates offered to the AI curator
PAIR_SLOTS = 14           # of those, at most this many two-person rooms
MIN_SHARED = 2            # shared rare tokens to qualify as common ground
MAX_MEMBERS = 5           # an audience past this is a mailing list, not a room
TOPIC_DF_MAX = 12         # a token carried by more people than this is generic
PEOPLE_LIMIT = 12
EVENT_DAYS = 21           # how far back the shared-link scan reaches
EVENT_LINKS = 300         # newest links considered per refresh
MAX_MARKERS = 12          # a marker is a row on a list, not a feed
MARKER_PTS = 12           # a live thing to say is a reason to talk to someone
MARKER_SLOTS = 3          # rows held below the cut for markers that missed it

_lock = threading.Lock()
_refresh_lock = threading.Lock()

STOP = set("""a about above after again all also am an and any are as at be
because been before being below between both but by can did do does doing
down during each few for from further had has have having he her here hers
him his how i if in into is it its just like me more most my no nor not of
off on once only or other our out over own same she so some such than that
the their them then there these they this those through to too under until
up very was we were what when where which while who whom why will with you
your really thing things want wants know knows text texts message messages
call calls week month year years time times good great new old talk talks
said says asked asking sent gets got make makes made recently currently
also often usually plan plans still keep keeps loop loops open contact
http https www com net org html index page news article story amp utm
company companies business service services meeting meetings email
emails update updates thanks sounds maybe pretty since around
went goes going came comes appears seems looks lately closest never
oldest excited weekend visit reply send sends uses used using
hasn wasn isn didn doesn couldn wouldn shouldn aren weren don won ain
working looking getting making taking coming trying talking thinking
wanting needing helping saying asking telling seeing having doing
putting giving calling texting sending waiting starting finishing
""".split())

MIN_SUBJECT_LEN = 6       # below this a shared word is phrasing, not a topic

# a possessive or contraction is the same word wearing a suffix — without
# this "that's" walks straight past a stop list that holds "that"
_CLIP = re.compile(r"('s|'re|'ve|'ll|'d|'t|'m|-)+$")


def _words(text):
    out = set()
    for t in re.findall(r"[a-z][a-z'&-]{3,}", (text or "").lower()):
        t = _CLIP.sub("", t)
        if len(t) > 3 and t not in STOP:
            out.add(t)
    return out


# ---------- priority people ----------

def priority_people(limit=PEOPLE_LIMIT):
    c = crm._load()
    scores = {}       # pid -> {"score": float, "reasons": [str]}

    def bump(pid, pts, reason, first=False):
        if not pid:
            return
        row = scores.setdefault(pid, {"score": 0.0, "reasons": []})
        row["score"] += pts
        if not reason:
            return
        if first:
            row["reasons"].insert(0, reason)   # markers outrank the slice
        else:
            row["reasons"].append(reason)

    for w in brief._unreplied_imessages():
        hrs = w.get("hours") or 0
        pts = 50 if hrs < 72 else 35
        bump(w["person_id"], pts,
             f"waiting on your reply ({int(hrs)}h)" if hrs else
             "waiting on your reply")
    for q in brief._going_quiet():
        over = max(0, q["days"] - brief.QUIET_DAYS)
        bump(q["person_id"], min(40.0, 12 + over * 1.5),
             f"going quiet — {q['days']} days since contact")
    loop_pts = Counter()
    for lp in brief._open_loops():
        pid = lp["person_id"]
        if loop_pts[pid] >= 24:
            continue
        mine = lp.get("owed_by") == "me"
        pts = 8 if mine else 4
        stale = min(8, (lp.get("days") or 0) / 14)
        loop_pts[pid] += pts
        bump(pid, pts + stale,
             (f"you owe: {lp['what'][:70]}" if mine
              else f"open loop: {lp['what'][:70]}"))
    try:
        for b in (brief._calendar().get("birthdays") or []):
            title = b.get("title") or ""
            name = re.sub(r"(’s|'s)?\s*[Bb]irthday.*$", "", title).strip()
            hits = crm.search_people(name, limit=1) if name else []
            if hits:
                bump(hits[0]["id"], 30, f"birthday {b.get('date', 'soon')}")
    except Exception:  # noqa: BLE001 — calendar store optional
        pass
    for pid, row in scores.items():
        prof = c["profiles"].get(pid) or {}
        hooks = prof.get("hooks")
        if isinstance(hooks, list) and hooks:
            row["score"] += min(6, 2 * len(hooks))
            row["reasons"].append(f"{len(hooks)} conversation hook(s) ready")

    # markers ride the cached store: a live item this one person cares
    # about is worth a row of its own, so bump people with nothing else
    seen_marker = set()
    for m in _read_store().get("markers", []):
        pid = m.get("person_id")
        if not pid or pid in seen_marker or pid not in c["by_id"]:
            continue
        seen_marker.add(pid)
        bump(pid, MARKER_PTS, m.get("text") or "", first=True)

    out = []
    for pid, row in scores.items():
        person = c["by_id"].get(pid)
        if not person:
            continue
        out.append({
            "person_id": pid,
            "person_name": person["name"],
            "tier": person.get("profile_tier") or person.get("master_tier"),
            "score": round(row["score"], 1),
            "reasons": [r for r in row["reasons"] if r][:4],
            "marker": pid in seen_marker,
        })
    out.sort(key=lambda x: -x["score"])
    # An owed reply outranks a marker and should — but scoring a marker
    # honestly means it never survives the cut, and a thing to say that
    # nobody ever sees is not a feature. A few seats at the bottom are
    # held for them; they keep their real score, so the list stays sorted
    # and nothing is inflated to get there.
    keep = out[:limit]
    if seen_marker:
        held = [r for r in out[limit:] if r["marker"]][:MARKER_SLOTS]
        keep = sorted(keep + held, key=lambda x: -x["score"])
    return keep


# ---------- fingerprints ----------

def person_tokens(person, prof, master):
    """Rare-topic fingerprint for one person, own-name tokens excluded.
    Shared with atlas.py, which generalizes the pairwise overlap into a
    full shared_topic edge set."""
    texts = []
    for key in ("relationship_summary", "comms_style"):
        v = prof.get(key)
        if isinstance(v, str):
            texts.append(v)
    for key in ("hooks", "personal_facts", "open_loops"):
        v = prof.get(key)
        if isinstance(v, list):
            for x in v:
                texts.append(x.get("text") or x.get("what") or ""
                             if isinstance(x, dict) else str(x))
    for key in ("company", "title", "relationship"):
        if master.get(key):
            texts.append(str(master[key]))
    own = {t for t in re.findall(r"[a-z']+", (person.get("name") or "").lower())}
    return {t for t in _words(" ".join(texts)) if t not in own}


def _activity(p):
    return (p.get("imsg_n") or 0) + (p.get("email_n") or 0) * 2


def _fingerprints(c):
    """pid -> token set, over the most active tiered contacts."""
    ranked = sorted(
        (p for p in c["people"]
         if (p.get("profile_tier") or p.get("master_tier"))
         and not p.get("name", "").startswith("(")),
        key=_activity, reverse=True)[:TOP_ACTIVE]
    out = {}
    for p in ranked:
        prof = c["profiles"].get(p["id"]) or {}
        master = (crm.get_person(p["id"]) or {}).get("master") or {}
        toks = person_tokens(p, prof, master)
        if toks:
            out[p["id"]] = toks
    return out


def _topic_index(fingerprints):
    """(token -> sorted carriers, document frequency) — the inversion that
    makes an audience the primary shape and a pair the leftover case."""
    carriers = defaultdict(list)
    for pid in sorted(fingerprints):
        for t in fingerprints[pid]:
            carriers[t].append(pid)
    df = Counter({t: len(pids) for t, pids in carriers.items()})
    return carriers, df


def _trim(c, pids):
    """The members who matter when a token is carried by a crowd."""
    if len(pids) <= MAX_MEMBERS:
        return sorted(pids)
    ranked = sorted(pids, key=lambda p: -_activity(c["by_id"].get(p) or {}))
    return sorted(ranked[:MAX_MEMBERS])


def _size_bonus(n):
    """More people caring about one thing is worth more than more words
    matching between two — but only a little more, so the ranking still
    reflects the evidence rather than a preference for big rooms."""
    return 1 + 0.3 * (n - 2)


# ---------- trigger: standing profile overlap ----------

def overlap_groupings(c, fingerprints, df, carriers):
    """Audiences with real common ground, best-first. Candidates keyed by
    member set, so different tokens naming the same room merge into one."""
    cand = {}
    for t, pids in carriers.items():
        if not 2 <= df[t] <= TOPIC_DF_MAX:
            continue
        members = _trim(c, pids)
        if len(members) < 2:
            continue
        cand.setdefault(tuple(members), set()).add(t)
    out = []
    for members, seed_tokens in cand.items():
        shared = [t for t in set.intersection(
            *(fingerprints[p] for p in members))
            if 2 <= df[t] <= TOPIC_DF_MAX]
        shared = sorted(set(shared) | seed_tokens, key=lambda t: df[t])
        strong = _substantive(shared)
        # two people whose profiles both say "went" and "reply" share
        # nothing; a room needs at least one actual subject in common
        if len(shared) < MIN_SHARED or not strong:
            continue
        shared = strong + [t for t in shared if t not in strong]
        score = sum(1.0 / df[t] for t in shared) * _size_bonus(len(members))
        out.append({
            "members": list(members),
            "topics": shared[:8],
            "score": round(score, 3),
            "trigger": {"type": "overlap"},
        })
    out.sort(key=lambda x: -x["score"])
    return out


# ---------- trigger: what just landed (local, chat.db) ----------

def recent_links(days=EVENT_DAYS, limit=EVENT_LINKS):
    """Links shared in the owner's conversations lately, newest first:
    [{url, title, domain, from_pid, from_name, when, seen_pids}].

    Read live from chat.db — the same URL-balloon pass the media index
    makes, without depending on the index being built. Returns [] on any
    platform or instance that has no chat.db to read (Windows, the
    fixture demo, a locked store), which drops the whole event trigger
    and leaves groupings on profile overlap alone.

    seen_pids is everyone already in the thread the link appeared in.
    Suggesting an article to the person you sent it to is the one way
    this feature can embarrass its owner, so the read carries the
    audience that has already seen it."""
    from . import imessage, media, settings
    if settings.fixture_mode() or not settings.IS_MAC:
        return []
    c = crm._load()
    cutoff = int((datetime.now(timezone.utc)
                  - timedelta(days=days)).timestamp()
                 - imessage.APPLE_EPOCH) * 1_000_000_000
    try:
        con = imessage._connect()
    except (sqlite3.Error, OSError):
        return []
    try:
        rows = con.execute(
            """SELECT m.date, m.is_from_me, m.text, m.attributedBody,
                      m.payload_data, h.id, cmj.chat_id
               FROM message m
               JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
               LEFT JOIN handle h ON h.ROWID = m.handle_id
               WHERE m.date > ?
                 AND (m.balloon_bundle_id =
                        'com.apple.messages.URLBalloonProvider'
                      OR m.text LIKE '%http%')
               ORDER BY m.date DESC LIMIT ?""",
            (cutoff, limit * 6)).fetchall()
        by_url = {}
        for date_ns, from_me, text, blob, payload, handle, chat_id in rows:
            url = title = None
            if payload:
                url, title = media._link_from_payload(payload)
            if not url:
                m = media.URL_RE.search(imessage.msg_text(text, blob) or "")
                url = m.group(0) if m else None
            if not url:
                continue
            key = url.split("?")[0].rstrip("/").lower()
            item = by_url.get(key)
            if item:
                item["chats"].add(chat_id)   # same link, several chat legs
                continue
            if len(by_url) >= limit:
                continue
            pid = None if from_me else (crm.resolve_handle(handle)
                                        if handle else None)
            person = c["by_id"].get(pid) if pid else None
            when = imessage.apple_dt(date_ns)
            by_url[key] = {
                "url": url,
                "title": (title or "").strip()[:140],
                "domain": media._domain(url),
                "from_pid": pid,
                "from_name": "you" if from_me else (
                    person["name"] if person else None),
                "when": when.isoformat(timespec="seconds") if when else None,
                "chats": {chat_id},
            }
        members = _chat_members(con, {cid for it in by_url.values()
                                      for cid in it["chats"]})
    except (sqlite3.Error, OSError):
        return []
    finally:
        con.close()

    out = []
    for item in by_url.values():
        chats = item.pop("chats")
        item["seen_pids"] = sorted(
            {p for cid in chats for p in members.get(cid, ())})
        out.append(item)
    return out


def _chat_members(con, chat_ids):
    """chat_id -> the CRM ids in it. Everyone in the thread has already
    seen whatever was posted there."""
    out = defaultdict(set)
    ids = list(chat_ids)
    for i in range(0, len(ids), 400):        # sqlite's variable ceiling
        batch = ids[i:i + 400]
        qmarks = ",".join("?" * len(batch))
        for cid, addr in con.execute(
                f"""SELECT chj.chat_id, h.id FROM chat_handle_join chj
                    JOIN handle h ON h.ROWID = chj.handle_id
                    WHERE chj.chat_id IN ({qmarks})""", batch).fetchall():
            pid = crm.resolve_handle(addr)
            if pid:
                out[cid].add(pid)
    return out


def _item_tokens(item):
    """The topic fingerprint of a shared link: its title, its host, and the
    readable words in its path."""
    stem = re.sub(r"\.(com|net|org|io|co|news|xyz|app)$", "",
                  item.get("domain") or "")
    path = re.sub(r"[^a-z]+", " ", (item.get("url") or "").lower())
    return _words(" ".join([(item.get("title") or ""),
                            stem.replace(".", " "), path]))


def _substantive(shared, title_toks=None):
    """The shared tokens a person would recognize as a subject, rarest
    first. Rarity alone does not make a topic: across only ~120 profiles
    an ordinary word like "give" looks rare, and two of those in common
    is not a shared interest — it is a coincidence of phrasing. For a
    link, a real match also names something in the headline rather than
    something scraped out of a URL."""
    return [t for t in shared
            if len(t) >= MIN_SUBJECT_LEN
            and (title_toks is None or t in title_toks)]


def event_groupings(c, fingerprints, df, items):
    """(groupings, markers) from what people actually sent lately. Nobody
    who was already in the thread is in the audience."""
    groupings, markers = [], []
    for item in items:
        toks = _item_tokens(item)
        if not toks:
            continue
        title_toks = _words(item.get("title"))
        # never hand someone back the thing they were already sent
        already = set(item.get("seen_pids") or ())
        already.add(item.get("from_pid"))
        audience, grounds = {}, {}
        for pid, fp in fingerprints.items():
            if pid in already:
                continue
            shared = sorted((t for t in toks & fp
                             if 2 <= df[t] <= TOPIC_DF_MAX),
                            key=lambda t: df[t])
            strong = _substantive(shared, title_toks)
            if not strong:
                continue
            # a headline is a handful of words, so it cannot clear the
            # same bar as a whole profile: one genuinely rare subject
            # counts as much as two ordinary tokens alongside it
            if len(shared) >= MIN_SHARED or df[strong[0]] <= 3:
                audience[pid] = shared
                grounds[pid] = strong[0]
        if not audience:
            continue
        label = item["title"] or item["domain"] or item["url"]
        if len(audience) == 1:
            pid, shared = next(iter(audience.items()))
            who = item.get("from_name")
            ground = grounds[pid]
            markers.append({
                "person_id": pid,
                "text": (f"{who} shared “{label[:70]}” — {ground} is their "
                         "ground too" if who and who != "you"
                         else f"worth passing “{label[:70]}” along — "
                              f"{ground} is their ground"),
                "topics": shared[:5],
                "rarity": df[ground],
                "url": item["url"],
                "domain": item["domain"],
                "when": item["when"],
                "from_name": who,
            })
            continue
        members = _trim(c, list(audience))
        shared = sorted(
            {t for p in members for t in audience[p]},
            key=lambda t: df[t])
        # the subjects lead: what names the room goes in front of the
        # ordinary words that merely happened to overlap
        strong = _substantive(shared, title_toks)
        shared = strong + [t for t in shared if t not in strong]
        score = (sum(1.0 / df[t] for t in shared[:8])
                 * _size_bonus(len(members)) * 1.4)   # live beats standing
        groupings.append({
            "members": members,
            "topics": shared[:8],
            "score": round(score, 3),
            "trigger": {"type": "event", "title": item["title"],
                        "url": item["url"], "domain": item["domain"],
                        "when": item["when"],
                        "from_pid": item["from_pid"],
                        "from_name": item["from_name"]},
        })
    groupings.sort(key=lambda x: -x["score"])
    # one marker per person — the same story reaches you on several links
    # (a youtu.be and a youtube.com of one video), and a row can only say
    # one thing. Rarest ground wins, newest breaks the tie.
    best = {}
    newest = sorted(markers, key=lambda m: m["when"] or "", reverse=True)
    for m in sorted(newest, key=lambda m: m["rarity"]):   # stable: ties keep
        best.setdefault(m["person_id"], m)                # the newer item
    return groupings, list(best.values())[:MAX_MARKERS]


# ---------- deterministic annotation: do they already know each other? ----------

def _atlas_pairs():
    """Every pair the Contact Atlas already links. The graph fuses photo
    co-occurrence, group co-chat, employer and family — so "these two
    obviously know each other" is answered from evidence instead of being
    guessed at in the prompt. No graph built yet: empty, and the move
    falls back to the audience size."""
    try:
        from . import atlas
        graph = atlas._read() or {}
    except Exception:  # noqa: BLE001 — the atlas is an optional view
        return set()
    return {(min(e["a"], e["b"]), max(e["a"], e["b"]))
            for e in (graph.get("edges") or [])
            if e.get("a") and e.get("b")}


def _group_finder():
    """members -> the group thread that already covers them, or None.
    One chat.db read per person, cached across the refresh."""
    from . import imessage, settings
    if settings.fixture_mode():
        return lambda members: None
    cache = {}

    def find(members):
        head = members[0]
        if head not in cache:
            try:
                cache[head] = imessage.groups_for_person(head)
            except Exception:  # noqa: BLE001 — chat.db optional
                cache[head] = []
        want = set(members)
        for g in cache[head]:
            pids = {p.get("person_id") for p in g.get("participants") or []}
            if want <= pids:
                return {"name": g.get("name"), "chat_ids": g.get("chat_ids"),
                        "size": len(pids)}
        return None
    return find


def _default_move(members, connected_pairs, existing):
    if existing:
        return "post_to_group"
    pairs = [(min(a, b), max(a, b))
             for i, a in enumerate(members) for b in members[i + 1:]]
    known = sum(1 for p in pairs if p in connected_pairs)
    if known == len(pairs):
        return "group_chat"
    if len(members) == 2:
        return "introduction"
    return "group_chat"


def _annotate(cands, connected_pairs, find_group):
    for cd in cands:
        existing = find_group(cd["members"])
        cd["existing_group"] = existing
        cd["move"] = _default_move(cd["members"], connected_pairs, existing)
    return cands


# ---------- the AI curation pass ----------

CURATE_PROMPT = """You are {owner}'s chief of staff, deciding which of \
{owner}'s contacts are worth putting in a room together right now.

Below are candidate GROUPINGS — two to five people who share real ground, \
each with the shared topics, a short dossier per person, and the trigger \
that surfaced them. Some were surfaced by a live item someone actually \
shared; those are the strongest, because there is a reason to reach out \
TODAY. Each candidate also says whether the members already know each \
other and whether a group thread with them already exists.

Pick only the groupings that are genuinely worth acting on — where every \
member plausibly gains something concrete. Drop a member who does not fit \
(keep at least two). Skip overlap that is coincidental wording, family \
members of each other, and rooms nobody would want.

Prefer the ROOM over the pair. When a candidate of three or more holds up \
— the ground is real for every one of them — pick it rather than the \
two-person versions of the same thing; a room of people who all care is \
worth more to {owner} than the same connection made twice. Shrink a room \
only when a member does not actually belong in it.

Set "move" to one of:
  post_to_group — a group thread with these people already exists; bring \
it up there
  group_chat    — worth starting a new group chat
  introduction  — exactly two people who should meet

Return ONLY a JSON object:
{{"groupings": [{{"members": ["pid", "pid", ...],
  "topic": "<the shared ground, 2-5 words, in plain language>",
  "move": "post_to_group|group_chat|introduction",
  "why": "<one or two sentences: the concrete mutual value, and why now>",
  "opener": "<a short message {owner} could send to open it — a \
double-opt-in ask for an introduction, or the actual first line for a \
group>"}}]}}

3 to 6 groupings, best first. Candidates:

{candidates}
"""


def _key(members):
    return "grp:" + ":".join(sorted(members))


# ---------- store ----------

def _blank():
    return {"generated": None, "groupings": [], "markers": [],
            "dismissed": []}


def _migrate_legacy():
    """Seed once from the pre-groupings store so dismissals survive the
    reframe: every intro becomes a two-member grouping, and each
    intro:a:b dismissal is remapped to its grp: key."""
    try:
        old = json.loads(LEGACY.read_text())
    except (OSError, json.JSONDecodeError):
        return _blank()
    s = _blank()
    s["generated"] = old.get("generated")
    for it in old.get("intros") or []:
        a, b = it.get("a_id"), it.get("b_id")
        if not a or not b:
            continue
        s["groupings"].append({
            "key": _key([a, b]),
            "members": [{"person_id": a, "name": it.get("a_name") or a},
                        {"person_id": b, "name": it.get("b_name") or b}],
            "topic": "", "move": "introduction",
            "why": it.get("why") or "", "opener": it.get("opener") or "",
            "trigger": {"type": "overlap"}, "existing_group": None,
        })
    for k in old.get("dismissed") or []:
        parts = str(k).split(":")
        s["dismissed"].append(
            _key(parts[1:]) if parts[0] == "intro" and len(parts) == 3 else k)
    s["dismissed"] = sorted(set(s["dismissed"]))
    return s


def _read():
    try:
        s = json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        s = _migrate_legacy()
    base = _blank()
    for k, v in base.items():
        s.setdefault(k, v)
    return s


def _write(s):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_name(STORE.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
    tmp.replace(STORE)


def _read_store():
    """Lock-free read for the hot path (priority_people's markers). Writers
    land the file with an atomic rename, so a reader never sees a torn
    store and never has to create the lock sidecar to look."""
    return _read()


# ---------- refresh ----------

def candidates():
    """Both triggers, merged and capped: (candidates, markers)."""
    c = crm._load()
    fingerprints = _fingerprints(c)
    if not fingerprints:
        return [], []
    carriers, df = _topic_index(fingerprints)
    try:
        items = recent_links()
    except Exception:  # noqa: BLE001 — the event trigger is best-effort
        items = []
    ev, markers = event_groupings(c, fingerprints, df, items)
    ov = overlap_groupings(c, fingerprints, df, carriers)
    # one room, one candidate: when both triggers name the same people the
    # live one wins the framing (there is a reason to write TODAY) and the
    # standing overlap contributes its topics and its score
    rooms = {}
    for cd in ev + ov:
        k = frozenset(cd["members"])
        prev = rooms.get(k)
        if not prev:
            rooms[k] = cd
            continue
        keep = prev if prev["trigger"]["type"] == "event" else cd
        drop = cd if keep is prev else prev
        keep["score"] = max(prev["score"], cd["score"])
        keep["topics"] = (keep["topics"]
                          + [t for t in drop["topics"]
                             if t not in keep["topics"]])[:8]
        rooms[k] = keep

    merged, keys, pairs = [], [], 0
    for cd in sorted(rooms.values(), key=lambda x: -x["score"]):
        k = frozenset(cd["members"])
        # a smaller room saying nothing new is noise: drop it when a
        # stronger candidate already holds these people AND these topics
        if any(k < prev and set(cd["topics"]) <= set(m["topics"])
               for prev, m in zip(keys, merged)):
            continue
        # Two profiles overlap on more words than three do, so scoring
        # alone hands the whole pool to pairs and the rooms this engine
        # exists to find never reach the curator. The tail of the pool is
        # theirs; pairs take the rest on merit.
        if len(k) == 2:
            if pairs >= PAIR_SLOTS:
                continue
            pairs += 1
        keys.append(k)
        merged.append(cd)
        if len(merged) >= MAX_CANDIDATES:
            break
    return merged, markers


def refresh_groupings():
    """Regenerate the curated grouping list (deterministic candidates +
    one AI curation pass). Serialized; safe to fire from a thread."""
    from . import settings, suggest
    with _refresh_lock:
        c = crm._load()
        cands, markers = candidates()
        if not cands:
            with _lock, locked(STORE):
                s = _read()
                s["generated"] = datetime.now(timezone.utc).isoformat(
                    timespec="seconds")
                s["groupings"] = []
                s["markers"] = markers
                _write(s)
            return []

        _annotate(cands, _atlas_pairs(), _group_finder())

        def dossier(pid):
            person = c["by_id"].get(pid) or {}
            prof = c["profiles"].get(pid) or {}
            master = (crm.get_person(pid) or {}).get("master") or {}
            bits = [person.get("name", pid)]
            for k in ("company", "title", "relationship"):
                if master.get(k):
                    bits.append(f"{k}: {master[k]}")
            if isinstance(prof.get("relationship_summary"), str):
                bits.append(prof["relationship_summary"][:200])
            return " | ".join(bits)

        blocks = []
        for i, cd in enumerate(cands):
            tg = cd["trigger"]
            if tg["type"] == "event":
                who = tg.get("from_name") or "someone"
                trigger = (f"live item: {who} shared "
                           f"“{tg.get('title') or tg.get('url')}” "
                           f"({tg.get('domain')}) on {tg.get('when')}")
            else:
                trigger = "standing profile overlap"
            eg = cd.get("existing_group")
            room = (f"a group thread already covers them"
                    f"{' (' + eg['name'] + ')' if eg and eg.get('name') else ''}"
                    if eg else "no existing group thread")
            blocks.append(
                f"- candidate {i}: members {', '.join(cd['members'])}\n"
                f"  shared topics: {', '.join(cd['topics'])}\n"
                f"  trigger: {trigger}\n"
                f"  connection: {room}; suggested move: {cd['move']}\n"
                # dossiers dominate this prompt — 40 candidates of up to
                # five people each. Kept tight so the whole pass stays
                # inside the model timeout rather than falling back.
                + "\n".join(f"  - {dossier(p)[:260]}" for p in cd["members"]))
        owner = settings.get("owner_name") or "the owner"
        prompt = CURATE_PROMPT.format(owner=owner,
                                      candidates="\n".join(blocks)[:30_000])
        by_members = {frozenset(cd["members"]): cd for cd in cands}
        groupings, curated = [], True
        try:
            parsed = suggest._extract_json(suggest.complete(prompt))
            for g in (parsed.get("groupings") or [])[:10]:
                ids = [p for p in (g.get("members") or []) if p in c["by_id"]]
                ids = sorted(set(ids))
                if len(ids) < 2:
                    continue
                src = next((cd for k, cd in by_members.items()
                            if set(ids) <= k), None)
                if not src:
                    continue
                # the model names the topic and writes the opener; it does
                # not get to contradict chat.db about whether a thread
                # exists, which is the one fact the move turns on
                move = g.get("move")
                if src.get("existing_group"):
                    move = "post_to_group"
                elif move not in ("group_chat", "introduction"):
                    move = "introduction" if len(ids) == 2 else "group_chat"
                groupings.append(_row(
                    c, ids, src,
                    topic=(g.get("topic") or "")[:60],
                    move=move,
                    why=(g.get("why") or "")[:500],
                    opener=(g.get("opener") or "")[:500]))
        except Exception:  # noqa: BLE001 — fall back to the raw candidates
            # The deterministic half stands on its own, but it must not
            # pass itself off as curated: the card says so, so a thin
            # result reads as "rescan" rather than as a dim engine.
            curated = False
            for cd in cands[:8]:
                tg = cd["trigger"]
                why = ("shared ground: " + ", ".join(cd["topics"][:5])
                       if tg["type"] == "overlap" else
                       f"{tg.get('from_name') or 'someone'} shared "
                       f"“{tg.get('title') or tg.get('domain')}” — all of "
                       "them have this ground")
                groupings.append(_row(c, cd["members"], cd,
                                      topic=cd["topics"][0],
                                      move=cd["move"], why=why, opener="",
                                      curated=False))
        seen = set()
        groupings = [g for g in groupings
                     if not (g["key"] in seen or seen.add(g["key"]))]
        with _lock, locked(STORE):
            s = _read()
            s["generated"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds")
            s["groupings"] = groupings
            s["markers"] = markers
            s["curated"] = curated
            _write(s)
        return groupings


def _row(c, ids, src, *, topic, move, why, opener, curated=True):
    return {
        "key": _key(ids),
        "members": [{"person_id": p, "name": c["by_id"][p]["name"]}
                    for p in ids],
        "topic": topic or src["topics"][0],
        "move": move,
        "why": why,
        "opener": opener,
        "curated": curated,
        "trigger": src["trigger"],
        "existing_group": src.get("existing_group"),
    }


def list_groupings():
    s = _read_store()
    dismissed = set(s["dismissed"])
    return {"generated": s["generated"],
            "groupings": [g for g in s["groupings"]
                          if g.get("key") not in dismissed]}


def dismiss(key, restore=False):
    with _lock, locked(STORE):
        s = _read()
        d = set(s["dismissed"])
        (d.discard if restore else d.add)(key)
        s["dismissed"] = sorted(d)
        _write(s)


def compose(limit=PEOPLE_LIMIT):
    """The Radar window payload."""
    return {"people": priority_people(limit), **list_groupings(),
            "as_of": dt.datetime.now().isoformat(timespec="seconds")}
