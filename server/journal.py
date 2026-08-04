"""The brief journal: knowledge the owner types INTO the Daily Brief.

The brief was read-only — everything on it derived from stores the owner
could not touch without clicking into each person. This module is the
write path: the owner recounts what he knows ("dinner with Chris finally
happened", "Sarah's baby is due in September", "I paid Mark back") and it
is (1) saved verbatim to data/brief-journal.json immediately — durable no
matter what — then (2) integrated by one background AI pass that maps the
note onto the CRM: closing the open loops it resolves, appending facts to
the right profiles (stamped source:"vira" so profile refreshes preserve
them — see crm/scripts/synthesize_profiles.py VIRA_EDITABLE), and
recording new commitments as loops. Recent entries also ride into the
brief's compose payload, so the next narrative generation knows what the
owner said. Every applied action is recorded on the entry in plain
English — Vira never silently edits the CRM.

Model-guessed person mappings are verified deterministically against
ground truth (CRM registry, profiles, enrichment verdicts, the person's
recent chat.db messages) before anything is written or handed downstream
— see _pid_checker. A guess nothing supports is corrected or visibly
held, never emitted as fact.

Cross-process discipline matches the other JSON stores (fresh reads,
fcntl-locked mutations, atomic writes); integration runs on a daemon
thread so the POST returns instantly.
"""
import datetime as dt
import json
import os
import re
import threading
import uuid
from pathlib import Path

from . import data as crm
from .filelock import locked

STORE = Path(__file__).resolve().parent.parent / "data" / "brief-journal.json"
MAX_ENTRIES = 400
ROSTER_PEOPLE = 40      # recent people offered for name resolution
PROMPT_LOOPS_CAP = 60   # open loops listed in the prompt


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


def _load():
    try:
        s = json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        s = {}
    if not isinstance(s, dict):
        s = {}
    s.setdefault("entries", [])
    return s


def _save(s):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_name(STORE.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
    tmp.replace(STORE)


def recent(limit=12):
    """Newest first. Pending entries always included so the UI can poll."""
    entries = _load()["entries"]
    out = list(reversed(entries[-limit:]))
    pending = [e for e in entries[:-limit] if e.get("status") == "pending"]
    return pending + out


def add(text, person_id=None, integrate=True, context=None):
    text = (text or "").strip()
    if not text:
        raise ValueError("empty note")
    person_name = None
    if person_id:
        p = crm._load()["by_id"].get(person_id)
        if not p:
            raise KeyError(person_id)
        person_name = p["name"]
    entry = {"id": "note_" + uuid.uuid4().hex[:10], "text": text[:4000],
             "person_id": person_id, "person_name": person_name,
             "context": (context or "").strip()[:300] or None,
             "created": _now(), "status": "pending", "result": None}
    with locked(STORE):
        s = _load()
        s["entries"].append(entry)
        if len(s["entries"]) > MAX_ENTRIES:
            s["entries"] = s["entries"][-MAX_ENTRIES:]
        _save(s)
    if integrate:
        threading.Thread(target=_integrate, args=(entry["id"],),
                         daemon=True, name="journal-integrate").start()
    return entry


def _update_entry(eid, **changes):
    with locked(STORE):
        s = _load()
        for e in s["entries"]:
            if e["id"] == eid:
                e.update(changes)
                break
        _save(s)


# ---------- the integration pass ----------

INTEGRATE_PROMPT = """You are Vira, {owner}'s chief of staff. {owner} just \
typed a note into his daily brief — knowledge from his own head that you \
must map onto his CRM. Today is {date}.

{owner}'s note:
"{text}"
{scope}
People roster (name -> person_id) for resolving mentions:
{roster}

Currently-open loops (pending items between {owner} and people):
{loops}

Return STRICT JSON only, no prose around it:
{{
 "loop_actions": [{{"person_id": "...", "match_what": "<copy a listed loop's \
what EXACTLY>", "action": "close"}} or {{"person_id": "...", "match_what": \
"...", "action": "edit", "new_what": "..."}}],
 "new_loops": [{{"person_id": "...", "what": "...", "owed_by": "me" or "them"}}],
 "facts": [{{"person_id": "...", "fact": "<durable fact worth remembering>"}}],
 "unapplied": [{{"instruction": "<one precise, self-contained imperative \
instruction>", "area": "<what it touches: contacts, calendar, config, app, \
data, other>"}}],
 "summary": "<one plain sentence: what you extracted and did>"
}}

Rules:
- close a loop when the note says it happened, was resolved, or no longer
  matters; edit when the note updates its state but it stays open.
- match_what must be byte-for-byte one of the listed loops' texts.
- use ONLY person_ids present in the roster or the loops list. If a mention
  cannot be resolved, skip it and say so in summary.
- facts are durable knowledge about a person (life events, preferences,
  plans), phrased in third person. Not tasks, not the note itself.
- unapplied is for anything the note asks that the actions above CANNOT
  express — merging or splitting contacts, correcting a calendar/overlap
  judgment, changing Vira's configuration or behavior, fixing data outside
  loops and facts. Encode each as one instruction an agent with full access
  could execute later, carrying every specific the note gives (names, dates,
  which event, which contact). Never silently drop such a request.
- "(unidentified)" roster entries are placeholder contacts awaiting a name.
  Never assume one of them is the company or sender the note mentions —
  picking one is a guess that lands knowledge on the wrong real person.
- when the note describes a message from a company or automated sender (a
  bank, a service, a notification), do not map it onto a roster person
  unless that person's entry plainly IS that company; describe it in
  unapplied without a person_id instead.
- never invent anything the note does not say. If nothing is actionable,
  return empty arrays and summarize the note as saved.
"""


def _all_open_loops():
    """Every open loop with its person id — the integration candidate set
    (the brief itself caps at 15; the model should see the full picture)."""
    c = crm._load()
    out = []
    for pid, prof in c["profiles"].items():
        loops = prof.get("open_loops")
        if not isinstance(loops, list):
            continue
        person = c["by_id"].get(pid)
        name = person["name"] if person else prof.get("name", pid)
        for lp in loops:
            if isinstance(lp, dict) and lp.get("status") != "closed":
                out.append({"person_id": pid, "person_name": name,
                            "what": lp.get("what", ""),
                            "owed_by": lp.get("owed_by", ""),
                            "since": lp.get("since", "")})
    return out


def _roster(scoped_pid=None):
    people = {p["id"]: p["name"]
              for p in crm.search_people(limit=ROSTER_PEOPLE)}
    if scoped_pid:
        p = crm._load()["by_id"].get(scoped_pid)
        if p:
            people[scoped_pid] = p["name"]
    return people


def _integrate(eid):
    entry = next((e for e in _load()["entries"] if e["id"] == eid), None)
    if not entry:
        return
    try:
        plan = _plan(entry)
        actions = _apply(plan, entry)
        unapplied = _clean_unapplied(plan, entry)
        _stage_unapplied(entry, unapplied)
        _update_entry(eid,
                      status="integrated" if actions else "noted",
                      result={"summary": plan.get("summary") or
                              ("saved to journal" if not actions else ""),
                              "actions": actions,
                              "unapplied": unapplied})
    except Exception as e:  # noqa: BLE001 — the note itself is already safe
        _update_entry(eid, status="failed",
                      result={"summary": f"integration failed: {str(e)[:200]}"
                                         " — note kept in journal",
                              "actions": []})


def _plan(entry):
    from . import settings, suggest
    roster = _roster(entry.get("person_id"))
    loops = _all_open_loops()
    scoped = entry.get("person_id")
    if scoped:  # the scoped person's loops always make the prompt
        loops.sort(key=lambda l: l["person_id"] != scoped)
    loops = loops[:PROMPT_LOOPS_CAP]
    scope = ""
    if scoped:
        scope = (f'\nThis note was written about {entry.get("person_name")} '
                 f'(person_id {scoped}).\n')
    if entry.get("context"):
        scope += (f'\nWhere the note was written (what the owner was looking '
                  f'at): {entry["context"]}\n')
    prompt = INTEGRATE_PROMPT.format(
        owner=settings.get("owner_name") or "the owner",
        date=dt.date.today().isoformat(),
        text=entry["text"],
        scope=scope,
        roster="\n".join(f"- {n} -> {i}" for i, n in roster.items()) or "(none)",
        loops="\n".join(
            f'- {l["person_name"]} ({l["person_id"]}): "{l["what"]}" '
            f'[owed_by {l["owed_by"] or "?"}, since {l["since"] or "?"}]'
            for l in loops) or "(none)")
    return suggest._extract_json(suggest.complete(prompt))


def _person_name(pid):
    p = crm._load()["by_id"].get(pid)
    return p["name"] if p else pid


# ---------- person-mapping verification (deterministic) ----------
# Added after the 2026-07-16 incident: a note about an automated U.S. Bank
# message was integrated with the person_id of a real friend's placeholder
# contact — the model guessed, and everything downstream (profile writes,
# the unapplied-instruction export) trusted the guess. When a note names
# an entity, every model-emitted person_id must now be backed by ground
# truth — the CRM registry, the person's profile, their enrichment
# verdict, or their recent chat.db messages — or it is corrected/held.

_ENTITY_STOP = frozenset("""
    i a an and are at but for from he her here his if in is it its me my of
    on or our she so that the their them then there these they this those
    to today tomorrow was we were when yesterday you your
    monday tuesday wednesday thursday friday saturday sunday
    january february march april may june july august september october
    november december vira crm
    """.split())

_TOKEN_RE = re.compile(r"[A-Za-z][\w.&'’-]*")
_PID_RE = re.compile(r"\bp_[a-z0-9]{12}\b")
_ABBR_END = re.compile(r"(?:^|[^A-Za-z])(?:[A-Za-z]\.){1,3}$")


def _sentence_boundary(prev):
    """Does a new sentence start after `prev`? A trailing period counts
    unless it belongs to a dotted abbreviation ("from U.S." does not end
    the sentence; "the U.S. Bank." does)."""
    prev = prev.rstrip(" \t")
    if not prev:
        return True
    if prev[-1] in '!?:;\n"“(':
        return True
    return prev[-1] == "." and not _ABBR_END.search(prev)


def _norm(s):
    return re.sub(r"\s+", " ",
                  re.sub(r"[.'’“”\"()]", "", (s or "").lower())).strip()


def _found_norm(needle, hay):
    """Whole-word containment on normalized text — "us bank" matches
    "U.S. Bank loan docs" but "chris" never matches "christmas"."""
    if not needle or not hay:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])",
                     hay) is not None


def _entities(text):
    """Named entities the note asserts: runs of capitalized tokens, with
    stopwords (and the owner's own name) trimmed off the ends. Returns
    (entity, weak_variant) pairs. A single title-case word opening a
    sentence is ordinary sentence case, not a name, and is dropped; a
    multi-token run opening a sentence keeps a variant without its forced-
    caps first word ("Met Casey" -> variant "Casey") for generous matching.
    Intrinsically-capitalized tokens (U.S., PayPal) are never weak."""
    from . import settings
    stop = set(_ENTITY_STOP)
    stop.update(t for t in _norm(settings.get("owner_name")).split() if t)
    ents, run = [], []

    def flush():
        toks = list(run)
        run.clear()
        while toks and _norm(toks[0][0]) in stop:
            toks.pop(0)
        while toks and _norm(toks[-1][0]) in stop:
            toks.pop()
        if not toks:
            return
        first_tok, first_initial = toks[0]
        weak_first = bool(first_initial
                          and re.fullmatch(r"[A-Z][a-z]+", first_tok))
        if len(toks) == 1:
            if weak_first or len(_norm(first_tok)) < 3:
                return
            ents.append((first_tok, None))
            return
        full = " ".join(t for t, _ in toks)
        variant = " ".join(t for t, _ in toks[1:]) if weak_first else None
        ents.append((full, variant))

    text = text or ""
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0).rstrip(".,;:!?")
        if not tok:
            continue
        if tok[:1].isupper():
            initial = _sentence_boundary(text[:m.start()])
            if initial and run:  # a run never spans two sentences
                flush()
            run.append((re.sub(r"['’]s$", "", tok), initial))
            continue
        flush()
    flush()
    seen, out = set(), []
    for full, variant in ents:
        k = _norm(full)
        if k and k not in seen:
            seen.add(k)
            out.append((full, variant))
    return out


def _recent_texts(pid, limit=40):
    """The person's recent direct-thread messages from chat.db (decoded
    from text/attributedBody) — ground truth for verification. Best-effort:
    no chat.db here just means this evidence source is empty."""
    try:
        from . import imessage
        return [m["text"] for m in imessage.thread_for_person(pid, limit=limit)
                if m.get("text")]
    except Exception:  # noqa: BLE001 — evidence source, never a crash
        return []


def _person_haystack(pid):
    """Everything deterministically on record about a person, normalized:
    CRM name, profile loops and facts, the enrichment verdict text for
    their handles, and their recent chat.db messages. Vira-authored
    content (source:"vira" facts, hand-added loops with no quote/channel)
    is excluded — the journal's own past writes must never vouch for its
    next one, or one wrong mapping would self-justify forever."""
    from . import triage
    c = crm._load()
    p = c["by_id"].get(pid)
    if not p:
        return ""
    parts = [p.get("name") or ""]
    prof = c["profiles"].get(pid) or {}
    for lp in prof.get("open_loops") or []:
        if isinstance(lp, dict) and (lp.get("quote") or lp.get("channel")):
            parts += [lp.get("what") or "", lp.get("quote") or ""]
    for f in prof.get("personal_facts") or []:
        if isinstance(f, dict) and f.get("source") != "vira":
            parts.append(f.get("fact") or "")
    h = p.get("handles", {})
    handles = list(h.get("imessage") or []) + list(h.get("emails") or []) + \
        ["+1" + ph for ph in h.get("phones10") or []]
    for handle in handles:
        v = triage.verdict_for(handle)
        if v:
            parts += [str(v.get(k) or "") for k in
                      ("confirmed_name", "relationship", "evidence")]
    parts += _recent_texts(pid)
    return _norm("\n".join(parts))


def _entity_pids(entities):
    """Where the note's entities actually live: CRM people whose registry
    name IS the entity (normalized equality), plus enrichment verdicts
    naming it whose handle resolves to a person. A safe remap target only
    when this comes back with exactly one person."""
    from . import triage
    c = crm._load()
    pids = set()
    for ent in entities:
        ne = _norm(ent)
        if len(ne) < 4:
            continue
        for p in c["people"]:
            if _norm(p.get("name")) == ne:
                pids.add(p["id"])
        for v in triage._verdicts():
            if not isinstance(v, dict):
                continue
            hay = _norm(" ".join(str(v.get(k) or "") for k in
                                 ("confirmed_name", "relationship", "evidence")))
            if _found_norm(ne, hay):
                rp = crm.resolve_handle(v.get("handle") or "")
                if rp:
                    pids.add(rp)
    return pids


def _pid_checker(entry):
    """Build the mapping check for one note. check(pid) -> (verdict, pid):
    "ok" — the note names no entity, the owner scoped the note to this
    person, or an entity appears in the person's haystack; "corrected" —
    nothing ties the person to the note but exactly one other person IS
    the named entity (use the returned pid); "unverified" — no support
    and no safe remap: hold the write, flag the instruction."""
    ents = _entities((entry or {}).get("text") or "")
    scoped = (entry or {}).get("person_id")
    if not ents:
        return lambda pid: ("ok", pid)
    keys = {_norm(full) for full, _ in ents} | \
           {_norm(v) for _, v in ents if v}
    hay_hits, remap = {}, {}

    def check(pid):
        if not pid or pid == scoped:
            return "ok", pid
        if pid not in hay_hits:
            hay = _person_haystack(pid)
            hay_hits[pid] = any(_found_norm(k, hay) for k in keys)
        if hay_hits[pid]:
            return "ok", pid
        if "pids" not in remap:
            remap["pids"] = _entity_pids([full for full, _ in ents])
        others = remap["pids"] - {pid}
        if len(others) == 1:
            return "corrected", next(iter(others))
        return "unverified", pid

    return check


def _held(kind, pid, text):
    return (f'Held {kind} for {_person_name(pid)}: "{(text or "")[:120]}" — '
            'could not verify this person against the note (nothing in '
            'their name, profile, enrichment, or recent messages matches)')


def _remap_note(verdict, guessed):
    if verdict != "corrected":
        return ""
    return (f" (person corrected: the model guessed "
            f"{_person_name(guessed)}, whom nothing ties to the note)")


def _check_instruction(instr, check):
    """Verify every person_id token inside an unapplied instruction —
    the export hands these to a full-access session, which must never
    receive a confident wrong pid. Corrections are substituted in place,
    problems annotated in the text; returns (instr, worst_outcome) where
    the outcome is "none" (no pids), "ok", "corrected" or "unverified"."""
    pids = list(dict.fromkeys(_PID_RE.findall(instr)))
    if not pids:
        return instr, "none"
    status, notes = "ok", []
    for pid in pids:
        verdict, good = check(pid)
        if verdict == "corrected":
            instr = re.sub(r"\b" + re.escape(pid) + r"\b", good, instr)
            notes.append(f"person_id corrected: {pid} ({_person_name(pid)}) "
                         f"-> {good} ({_person_name(good)})")
            if status != "unverified":
                status = "corrected"
        elif verdict == "unverified":
            notes.append(f"person_id {pid} ({_person_name(pid)}) is "
                         "UNVERIFIED for this note — cross-check the "
                         "person's chat.db thread and enrichment verdict "
                         "before acting on it")
            status = "unverified"
    if notes:
        instr += " [" + "; ".join(notes) + "]"
    return instr, status


def _apply(plan, entry=None):
    """Apply the model's plan deterministically; every mutation becomes a
    plain-English action line, every miss a visible skip — never silent.
    Guessed person mappings are verified first (see _pid_checker):
    corrected when the note plainly names someone else, held when nothing
    ties the person to the note."""
    actions = []
    check = _pid_checker(entry)
    for la in plan.get("loop_actions") or []:
        try:
            act = la.get("action")
            if act not in ("close", "edit"):
                continue
            verdict, pid = check(la.get("person_id"))
            if verdict == "unverified":
                actions.append(_held("a loop action", la.get("person_id"),
                                     la.get("match_what")))
                continue
            crm.update_loop(pid, la.get("match_what"), act, la.get("new_what"))
            verb = "Closed" if act == "close" else "Updated"
            actions.append(f'{verb} loop with {_person_name(pid)}: '
                           f'"{(la.get("new_what") if act == "edit" else la.get("match_what"))[:120]}"'
                           + _remap_note(verdict, la.get("person_id")))
        except (KeyError, LookupError, ValueError,
                crm.ProfileCorruptError) as e:
            actions.append(f"Skipped a loop action ({e})")
    for nl in plan.get("new_loops") or []:
        try:
            verdict, pid = check(nl.get("person_id"))
            if verdict == "unverified":
                actions.append(_held("a new loop", nl.get("person_id"),
                                     nl.get("what")))
                continue
            saved = crm.add_loop(pid, nl.get("what"), nl.get("owed_by", "me"))
            actions.append(f'New loop with {_person_name(pid)}: '
                           f'"{saved["what"][:120]}" '
                           f'({"you owe" if saved["owed_by"] == "me" else "theirs"})'
                           + _remap_note(verdict, nl.get("person_id")))
        except (KeyError, ValueError, crm.ProfileCorruptError) as e:
            actions.append(f"Skipped a new loop ({e})")
    for f in plan.get("facts") or []:
        try:
            verdict, pid = check(f.get("person_id"))
            if verdict == "unverified":
                actions.append(_held("a fact", f.get("person_id"),
                                     f.get("fact")))
                continue
            saved = crm.add_fact(pid, f.get("fact"))
            actions.append(f'Fact saved to {_person_name(pid)}: '
                           f'"{saved["fact"][:120]}"'
                           + _remap_note(verdict, f.get("person_id")))
        except (KeyError, ValueError, crm.ProfileCorruptError) as e:
            actions.append(f"Skipped a fact ({e})")
    return actions


def _clean_unapplied(plan, entry=None):
    """Validate the model's unapplied list down to what the UI/export can
    trust: non-empty instruction strings, capped, with a short area tag —
    and every embedded person_id verified against the note (pid_check
    records the outcome; entries stored before this existed lack it and
    are re-checked at export time)."""
    check = _pid_checker(entry)
    out = []
    for u in plan.get("unapplied") or []:
        if not isinstance(u, dict):
            continue
        instr = str(u.get("instruction") or "").strip()
        if not instr:
            continue
        instr, pid_check = _check_instruction(instr[:600], check)
        out.append({"instruction": instr,
                    "area": str(u.get("area") or "other").strip()[:40],
                    "pid_check": pid_check})
    return out[:10]


# ---------- staging: an instruction becomes queued work ----------
# Until 2026-08-04 an unapplied instruction's ONLY exit was the clipboard:
# the owner copied a prompt and pasted it into a session he opened himself.
# That shape was his own 2026-07-16 spec ("encoded as a prompt that I can
# copy as an export feature, similar to my annotate"), written when a
# dispatched session was not yet trusted to finish. It has since been, and
# the asymmetry it left behind was arbitrary: the integration pass's other
# three channels — close a loop, edit a loop, write a fact — ALREADY write
# the CRM with no gate at all, so the channel that could not be expressed
# as a loop or a fact was the one carrying the HARDER gate.
#
# So an instruction is queued work now. It is staged as an idea, which is
# where every other piece of work in this app already lives — inheriting
# the Queue's approval bar, its tags, its similarity and its dispatch
# machinery rather than growing a second, weaker copy of all four.
#
# THE SPLIT IS BLAST RADIUS, NOT "deterministic vs needs code". The Vira
# repo is the better-protected target: branch-first placement, a worktree,
# a diff, a revert, the write guard, a test suite. The CRM has no git at
# all — its protection is people.json's backup and (since the same day, in
# data._backup_profile) the profile snapshot that made this list safe to
# widen. Areas outside both — a calendar judgment, an "other" — still want
# the owner's eye, so they stage as `proposed` and wait behind the
# approval bar instead of dispatching.
AUTO_AREAS = {"app", "config", "contacts", "data"}

# The cwd is ALWAYS the Vira checkout, for every area. It is the only tree
# with a safety net, it ships scripts/branch.sh (so worktree.ensure places
# the session on its own branch), it carries the agent contract, and the
# CRM is reachable from it by path. Running with cwd=the CRM would get the
# session neither placement nor a contract.
REPO = Path(__file__).resolve().parent.parent


def _task_rules(cwd):
    """The rules every hand-off carries, dispatched or exported. ONE
    composer, so the two can never drift on what a session is told."""
    return [
        f"Work in the owner's Vira repository at {cwd} — cd there first if "
        "you are not already inside it. The CRM stores this instruction may "
        "concern are reachable from there by path; read the repo's agent "
        "contract (AGENTS.md, and CLAUDE.md where present) and follow it.",
        # true whether REPO is the live checkout or (on a branch instance)
        # the worktree this Vira serves from — never call it "the checkout
        # the owner runs from", which is false in the second case
        "Branch first: the tree named above is the one this Vira is serving "
        'from, so do not build in it directly. Run "scripts/branch.sh start '
        '<slug>" and work in the worktree it creates. If you are already in '
        "a worktree of this repo, stay in it.",
        "Before writing anything into the CRM, verify it against the stores "
        "rather than trusting the instruction's wording — it was written by "
        "a model reading the owner's note, and the note below is the ground "
        "truth. Contact-registry and profile writes are backed up "
        "automatically; do not add a second backup step.",
        "Do not merge and do not push. The owner decides that after "
        "reviewing.",
        "The owner's Vira server is running on this machine. Do not "
        "restart, stop, or kill it. If your change needs a restart to take "
        "effect, say so in your report and leave it to them.",
        "If you hit a decision this instruction does not settle, ask the "
        "owner and stop rather than guessing.",
    ]


def _note_block(entry, u):
    """The instruction with the note it came from. The NOTE is what the
    owner actually said; the instruction is one model's reading of it, so a
    session gets both and is told which is which."""
    lines = [f'The owner told Vira, on {entry.get("created", "")[:10]}:',
             f'"""{entry.get("text", "")}"""']
    if entry.get("person_name"):
        lines.append(f'(the note is about {entry["person_name"]})')
    if entry.get("context"):
        lines.append(f'(written from: {entry["context"]})')
    lines.append("")
    lines.append(f'Vira could not apply this part of it automatically '
                 f'(area: {u.get("area") or "other"}):')
    lines.append(f'  {_verified_instruction(entry, u)}')
    return "\n".join(lines)


def _verified_instruction(entry, u):
    """The instruction text, pid-verified. Entries stored before
    _pid_checker existed carry no `pid_check`, so they are re-checked on
    the way out rather than trusted."""
    instr = u.get("instruction", "")
    if "pid_check" not in u:
        instr, _ = _check_instruction(instr, _pid_checker(entry))
    return instr


def instruction_prompt(entry, u, cwd=None):
    """The self-contained prompt for ONE instruction — what a dispatched
    session runs, and what "copy as prompt" hands to a session elsewhere."""
    cwd = cwd or REPO
    parts = [_note_block(entry, u), "", "Carry it out end to end:"]
    parts += [f"- {r}" for r in _task_rules(cwd)]
    parts += ["",
              "End with a concise report: what you changed and why, how you "
              "verified it, and anything you deliberately did not do."]
    return "\n".join(parts)


def _stage_one(entry, u):
    """Stage one instruction as a Queue idea, dispatching it when its area
    is inside the blast radius Vira is willing to act in unattended.

    Never raises: an instruction that cannot be staged or dispatched stays
    exactly where it was, unstamped, and the Queue lane still carries it.
    Losing the owner's request to an ideas-store hiccup would be strictly
    worse than the clipboard this replaces."""
    from . import ideas
    text = _verified_instruction(entry, u)
    if not text:
        return
    live = {"proposed", "open", "on-hold", "deferred"}
    for it in ideas.list_items():
        if it["status"] in live and it["text"].strip().lower() == text.lower():
            u["idea_id"] = it["id"]
            u["staged"] = _now()
            return
    auto = (u.get("area") or "other") in AUTO_AREAS and not _passive()
    note = f'from a journal note ({entry.get("created", "")[:10]})'
    item = ideas.add(text, status="open" if auto else "proposed",
                     source="journal", note=note, project="Vira")
    u["idea_id"] = item["id"]
    u["staged"] = _now()
    if not auto:
        return
    try:
        from . import session
        jid = session.sessions.launch(
            instruction_prompt(entry, u), cwd=str(REPO),
            idea_id=item["id"],
            meta={"journal_note": entry.get("id"), "kind": "journal"})
        u["job_id"] = jid
        ideas.stamp_note(item["id"], f"{note} — dispatched (job {jid[:8]})")
    except Exception as e:  # noqa: BLE001 — the idea is already on the Queue
        ideas.stamp_note(item["id"],
                         f"{note} — dispatch failed ({str(e)[:120]}); "
                         "run it from the Queue")


def _stage_unapplied(entry, unapplied):
    for u in unapplied:
        try:
            _stage_one(entry, u)
        except Exception:  # noqa: BLE001 — one bad instruction never stops the rest
            pass


def _passive():
    """A test clone stages (its ideas store is cloned and disposable) but
    never dispatches: it has no supervisor, so a launch there mints a job
    that can never run — the documented passive seam."""
    return bool(os.environ.get("VIRA_PASSIVE"))


# ---------- export: un-integrable knowledge as a copyable prompt ----------
# Kept for the instruction that did NOT auto-dispatch and for a session the
# owner would rather run elsewhere. It is composed from the same
# _task_rules/_note_block as the dispatch, so the exported text can no
# longer promise something the dispatched one does not (it used to name the
# repo and the CRM and then omit the branching rule entirely, pointing a
# session straight at the live checkout).

EXPORT_HEAD = """\
You are working for {owner}. Vira — his personal-assistant app — collected
the notes below from {owner}'s own head. Each carries an instruction Vira
could not apply automatically. Work through every one of them.

{rules}

Report what you did per item.
"""


def export_prompt():
    """One self-contained prompt covering every journal note whose
    integration left an unapplied instruction that is still outstanding,
    newest first."""
    from . import settings
    items = []
    for e in reversed(_load()["entries"]):
        for u in (e.get("result") or {}).get("unapplied") or []:
            if u.get("resolved"):
                continue  # marked done by the owner — off the active handoff
            items.append((e, u))
    if not items:
        return {"prompt": "", "count": 0}
    rules = "\n".join(f"- {r}" for r in _task_rules(REPO))
    lines = [EXPORT_HEAD.format(
        owner=settings.get("owner_name") or "the owner", rules=rules)]
    for i, (e, u) in enumerate(items, 1):
        about = f' (about {e["person_name"]})' if e.get("person_name") else ""
        where = f' [written from: {e["context"]}]' if e.get("context") else ""
        lines.append(f'{i}. [{e["created"][:10]}]{about} the owner said: '
                     f'"{e["text"]}"{where}\n   -> '
                     f'{_verified_instruction(e, u)} (area: {u["area"]})')
    return {"prompt": "\n".join(lines), "count": len(items)}


# ---------- resolving unapplied instructions ----------
# An unapplied instruction is a "needs a session" item Vira could not apply
# automatically — it rides the Queue lane until the owner hands it to a
# full-access session and does the work. There is no automatic loop back
# from a copy-out session, so the owner marks it done here: the instruction
# is stamped resolved (kept on the entry, so the Journal window still
# chronicles it as complete) and drops off the Queue lane and the export.
# Instructions carry no id, so a single one is addressed by entry id + its
# exact text — the same key-by-content the loop actions use for open loops.

def resolve_unapplied(entry_id, instruction):
    """Mark one unapplied instruction resolved. Returns True if a matching
    open instruction was found and stamped, False otherwise."""
    with locked(STORE):
        s = _load()
        for e in s["entries"]:
            if e["id"] != entry_id:
                continue
            for u in (e.get("result") or {}).get("unapplied") or []:
                if u.get("instruction") == instruction and not u.get("resolved"):
                    u["resolved"] = _now()
                    _save(s)
                    return True
            return False
    return False


def resolve_all_unapplied():
    """Mark every still-open unapplied instruction resolved. Returns the
    count stamped."""
    with locked(STORE):
        s = _load()
        n = 0
        for e in s["entries"]:
            for u in (e.get("result") or {}).get("unapplied") or []:
                if not u.get("resolved"):
                    u["resolved"] = _now()
                    n += 1
        if n:
            _save(s)
        return n
