"""Radar — who deserves attention, and who should meet whom.

Two engines, both explainable:

priority_people() — a deterministic ranking of who the owner should talk
to next, scored from live signals (each row carries its reasons): owed
replies (chat.db), going-quiet decay on active-tier contacts, stale open
loops weighted toward what the OWNER owes, birthdays inside a week, and
available conversation hooks (something to actually say). Reuses the
Daily Brief's loaders; same freshness, same cost profile (~100ms).

introductions — the connector engine. Deterministic candidate pass:
profile text (summary, hooks, facts, company/title) is tokenized per
person; rare-but-shared tokens across the ~120 most active contacts
surface pairs with real common ground (weighted by token rarity). ONE AI
pass then curates the top pairs into actual introduction pitches with an
opener draft. Cached in data/radar-intros.json (the intro-scout routine
refreshes weekly; a button refreshes on demand); dismissals persist.
"""
import datetime as dt
import json
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import brief
from . import data as crm
from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
INTROS = ROOT / "data" / "radar-intros.json"

TOP_ACTIVE = 120          # most-active contacts considered for intros
MAX_PAIRS = 40            # candidate pairs offered to the AI curator
MIN_SHARED = 2            # shared rare tokens to qualify as a candidate
PEOPLE_LIMIT = 12

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
""".split())


# ---------- priority people ----------

def priority_people(limit=PEOPLE_LIMIT):
    c = crm._load()
    scores = {}       # pid -> {"score": float, "reasons": [str]}

    def bump(pid, pts, reason):
        if not pid:
            return
        row = scores.setdefault(pid, {"score": 0.0, "reasons": []})
        row["score"] += pts
        if reason:
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
            "reasons": row["reasons"][:4],
        })
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


# ---------- introduction candidates ----------

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
    blob = " ".join(texts).lower()
    own = {t for t in re.findall(r"[a-z']+", (person.get("name") or "").lower())}
    toks = set()
    for t in re.findall(r"[a-z][a-z'&-]{3,}", blob):
        if t in STOP or t in own:
            continue
        toks.add(t)
    return toks


def intro_candidates():
    """Deterministic pairs with real common ground: [(a_pid, b_pid, score,
    shared_tokens)] best-first."""
    c = crm._load()

    def activity(p):
        return (p.get("imsg_n") or 0) + (p.get("email_n") or 0) * 2

    ranked = sorted(
        (p for p in c["people"]
         if (p.get("profile_tier") or p.get("master_tier"))
         and not p.get("name", "").startswith("(")),
        key=activity, reverse=True)[:TOP_ACTIVE]
    fingerprints = {}
    for p in ranked:
        prof = c["profiles"].get(p["id"]) or {}
        master = (crm.get_person(p["id"]) or {}).get("master") or {}
        toks = person_tokens(p, prof, master)
        if toks:
            fingerprints[p["id"]] = toks
    df = Counter()
    for toks in fingerprints.values():
        df.update(toks)
    pids = list(fingerprints)
    pairs = []
    for i, a in enumerate(pids):
        for b in pids[i + 1:]:
            shared = [t for t in fingerprints[a] & fingerprints[b]
                      if 2 <= df[t] <= 12]
            if len(shared) < MIN_SHARED:
                continue
            score = sum(1.0 / df[t] for t in shared)
            shared.sort(key=lambda t: df[t])
            pairs.append((a, b, round(score, 3), shared[:8]))
    pairs.sort(key=lambda x: -x[2])
    return pairs[:MAX_PAIRS]


CURATE_PROMPT = """You are {owner}'s chief of staff, deciding which \
introductions between {owner}'s contacts are genuinely worth making.

Below are candidate pairs discovered by profile overlap, each with the
shared topics and a short dossier per person. Pick the BEST introductions
only — pairs where both sides plausibly gain something concrete. Skip
pairs whose overlap is coincidental wording, family members of each
other, or people who obviously already know each other (same company,
same family name, explicit mentions).

Return ONLY a JSON object:
{{"intros": [{{"a_id": "...", "b_id": "...",
  "why": "<one or two sentences: the concrete mutual value>",
  "opener": "<a short double-opt-in message {owner} could send to one of \
them proposing the intro>"}}]}}

3 to 8 intros, best first. Candidates:

{candidates}
"""


def _intros_read():
    try:
        s = json.loads(INTROS.read_text())
    except (OSError, json.JSONDecodeError):
        s = {}
    s.setdefault("generated", None)
    s.setdefault("intros", [])
    s.setdefault("dismissed", [])
    return s


def _intros_write(s):
    INTROS.parent.mkdir(parents=True, exist_ok=True)
    tmp = INTROS.with_name(INTROS.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
    tmp.replace(INTROS)


def refresh_intros():
    """Regenerate the curated introduction list (deterministic candidates +
    one AI curation pass). Serialized; safe to fire from a thread."""
    from . import settings, suggest
    with _refresh_lock:
        pairs = intro_candidates()
        c = crm._load()
        if not pairs:
            with _lock, locked(INTROS):
                s = _intros_read()
                s["generated"] = datetime.now(timezone.utc).isoformat(
                    timespec="seconds")
                s["intros"] = []
                _intros_write(s)
            return []

        def dossier(pid):
            person = c["by_id"].get(pid) or {}
            prof = c["profiles"].get(pid) or {}
            master = (crm.get_person(pid) or {}).get("master") or {}
            bits = [person.get("name", pid)]
            for k in ("company", "title", "relationship"):
                if master.get(k):
                    bits.append(f"{k}: {master[k]}")
            if isinstance(prof.get("relationship_summary"), str):
                bits.append(prof["relationship_summary"][:300])
            return " | ".join(bits)

        blocks = []
        for a, b, score, shared in pairs:
            blocks.append(
                f"- a_id: {a}  b_id: {b}  shared topics: "
                f"{', '.join(shared)}\n  A: {dossier(a)[:400]}\n"
                f"  B: {dossier(b)[:400]}")
        owner = settings.get("owner_name") or "the owner"
        prompt = CURATE_PROMPT.format(owner=owner,
                                      candidates="\n".join(blocks)[:40_000])
        intros = []
        try:
            text = suggest.complete(prompt)
            parsed = suggest._extract_json(text)
            valid = {p["id"] for p in c["people"]}
            for it in (parsed.get("intros") or [])[:10]:
                a, b = it.get("a_id"), it.get("b_id")
                if a in valid and b in valid and a != b:
                    intros.append({
                        "a_id": a, "a_name": c["by_id"][a]["name"],
                        "b_id": b, "b_name": c["by_id"][b]["name"],
                        "why": (it.get("why") or "")[:500],
                        "opener": (it.get("opener") or "")[:500],
                        "key": f"intro:{min(a, b)}:{max(a, b)}",
                    })
        except Exception:  # noqa: BLE001 — fall back to the raw candidates
            for a, b, score, shared in pairs[:8]:
                pa, pb = c["by_id"].get(a), c["by_id"].get(b)
                if not pa or not pb:
                    continue
                intros.append({
                    "a_id": a, "a_name": pa["name"],
                    "b_id": b, "b_name": pb["name"],
                    "why": "shared ground: " + ", ".join(shared[:5]),
                    "opener": "",
                    "key": f"intro:{min(a, b)}:{max(a, b)}",
                })
        with _lock, locked(INTROS):
            s = _intros_read()
            s["generated"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds")
            s["intros"] = intros
            _intros_write(s)
        return intros


def list_intros():
    with _lock, locked(INTROS):
        s = _intros_read()
    dismissed = set(s["dismissed"])
    return {"generated": s["generated"],
            "intros": [i for i in s["intros"]
                       if i.get("key") not in dismissed]}


def dismiss_intro(key, restore=False):
    with _lock, locked(INTROS):
        s = _intros_read()
        d = set(s["dismissed"])
        (d.discard if restore else d.add)(key)
        s["dismissed"] = sorted(d)
        _intros_write(s)


def compose(limit=PEOPLE_LIMIT):
    """The Radar window payload."""
    return {"people": priority_people(limit), **list_intros(),
            "as_of": dt.datetime.now().isoformat(timespec="seconds")}
