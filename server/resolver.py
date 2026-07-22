"""AI-assisted contact resolver for triage.

Given an unknown iMessage handle (and, when the enrichment merge already
minted one, its placeholder person_id) plus whatever the owner types into
the "Tell Vira" box, figure out the contact's real name. Three deterministic
evidence sources are gathered, then ONE model pass proposes a name. The
proposal is verified against the gathered evidence (grounded-or-held,
mirroring journal._pid_checker) and RETURNED — never written. The
Add-to-CRM flow is still the only thing that writes people.json.

Evidence sources
  (a) the handle's own thread   imessage.thread_for_person / triage._recent_inbound
  (b) the referral chain        a referrer named in the evidence/memory ->
                                crm.search_people -> the group they share with
                                the unknown handle (imessage.groups_for_person,
                                already carries resolved co-members, so this is
                                a filter, not new SQL) -> that group's messages
  (c) shared contact cards      media.person_media / media_for_chats docs ->
                                the .vcf body (full name + number are in it)

A referral is returned as a `fact` string; the Add route writes it via
crm.add_fact (source:"vira", which survives profile re-synthesis). `how_we_met`
is deliberately untouched — it is model-synthesized and not writable through
any durable path (see docs/data-map.md + PROFILE_EDITABLE_FIELDS).
"""
import re
from pathlib import Path

from . import data as crm
from . import imessage
from . import journal
from . import media
from . import suggest
from . import triage

_CLASSES = {"friend", "family", "business", "service", "company"}


# ---------- referrer extraction (deterministic, precise on purpose) ----------
# "intro'd by Eric", "referred by Sarah Chen", "Eric connected us". A generic
# capitalized-word scan would pull "Brooklyn" and "Saturday" out of the
# enrichment evidence, so only these targeted phrases count — which is also
# why a card only auto-resolves when one of them is present.

_NAME = r"([A-Z][A-Za-z'’.-]+(?:\s+[A-Z][A-Za-z'’.-]+){0,2})"
_REFERRAL_RX = (
    re.compile(r"\b(?:intro(?:'?d|duced)?|referred|connected|sent|recommended)\b"
               r"[^.\n]{0,24}?\bby\s+" + _NAME),
    re.compile(r"\breferral\s+(?:from|by)\s+" + _NAME),
    re.compile(_NAME + r"\s+(?:was|is|were)\b[^.\n]{0,40}?"
               r"\bconnect(?:ed|ing)?\s+(?:me|us)\b"),
    re.compile(r"\b(?:thanks to|via)\s+" + _NAME +
               r"(?:'?s)?\s+(?:intro|referral|connect)"),
)


def _owner_first():
    from . import settings
    return ((settings.get("owner_name") or "").split() or [""])[0].lower()


def referrer_from_text(text):
    """The person who introduced this contact, pulled from free text (an
    enrichment verdict's evidence or the owner's typed memory), or "" when no
    referral phrase is present. Days/months and the owner's own name are
    rejected as referrer names."""
    text = text or ""
    owner = _owner_first()
    for rx in _REFERRAL_RX:
        for m in rx.finditer(text):
            name = re.sub(r"['’]s$", "", re.sub(r"\s+", " ", m.group(1)).strip(" .'’"))
            toks = journal._norm(name).split()
            if not toks:
                continue
            if toks[0] == owner or toks[0] in journal._ENTITY_STOP:
                continue
            return name
    return ""


# ---------- evidence gathering (deterministic) ----------

def _thread_texts(handle, person_id, limit=30):
    """(who, text) for the unknown contact's own thread. Uses the placeholder
    person when there is one, else the inbound-only content probe on the raw
    handle."""
    if person_id:
        msgs = imessage.thread_for_person(person_id, limit=limit)
        if msgs:
            return [("me" if m["from_me"] else "them", m["text"])
                    for m in msgs if m.get("text")]
    return [("them", t) for t in triage._recent_inbound(handle, limit=limit)]


def _name_matches(referrer, name):
    """Does the referrer string match a person's NAME (not just a handle/email,
    which search_people also matches on)?"""
    rn = journal._norm(referrer)
    nn = journal._norm(name)
    if not rn or not nn:
        return False
    return journal._found_norm(rn, nn) or all(
        journal._found_norm(t, nn) for t in rn.split())


def _resolve_referrer(referrer):
    """(pids, candidate_names, ambiguous) for a referrer name. One name match
    -> that pid; several -> union the pids (the intro group is among them) and
    flag ambiguous so the UI can ask which one."""
    if not referrer:
        return set(), [], False
    matches = crm.search_people(q=referrer, limit=8)
    named = [m for m in matches if _name_matches(referrer, m["name"])]
    matches = named or matches
    if not matches:
        return set(), [], False
    if len(matches) == 1:
        return {matches[0]["id"]}, [], False
    return ({m["id"] for m in matches},
            [m["name"] for m in matches[:6]], True)


def _referral_groups(person_id, referrer_pids):
    """Groups the unknown contact shares with the referrer — the thread where
    the introduction happened. groups_for_person already resolves every
    co-member to a pid, so this is a filter, not new SQL."""
    if not person_id or not referrer_pids:
        return []
    out = []
    for g in imessage.groups_for_person(person_id):
        pids = {p.get("person_id") for p in g.get("participants", [])}
        if pids & referrer_pids:
            out.append(g)
    return out


def _read_vcard(att_id):
    """The raw .vcf body for a shared-contact attachment (FN/N/TEL/EMAIL),
    or "" when the file is purged/unreadable. A vCard is plain text, so this
    is the same read _extract_doc_text does for .vcf — no heavy import."""
    try:
        path, _mime, _name = media.attachment_path(att_id)
    except Exception:  # noqa: BLE001 — chat.db best-effort
        return ""
    if not path:
        return ""
    try:
        return Path(path).read_text(errors="ignore")
    except OSError:
        return ""


def _vcards(person_id, group_chat_ids):
    """Text of shared contact cards in the contact's direct thread and any
    referral group. When someone 'connects us' it often literally means a
    card was sent — carrying the full name and number."""
    docs = []
    try:
        if person_id:
            docs += media.person_media(person_id).get("docs", [])
        if group_chat_ids:
            docs += media.media_for_chats(group_chat_ids).get("docs", [])
    except Exception:  # noqa: BLE001 — media pipeline best-effort
        docs = []
    cards, seen = [], set()
    for d in docs:
        if (d.get("ext") or "").upper() != "VCF":
            continue
        att = d.get("id")
        if att in seen:
            continue
        seen.add(att)
        txt = _read_vcard(att).strip()
        if txt:
            cards.append(txt[:2000])
    return cards


def gather_evidence(handle, person_id=None, memory=""):
    memory = memory or ""
    verdict = triage.verdict_for(handle) or {}
    evtext = " ".join(str(verdict.get(k) or "")
                      for k in ("relationship", "evidence"))
    referrer = referrer_from_text(memory) or referrer_from_text(evtext)

    thread = _thread_texts(handle, person_id)
    referrer_pids, candidates, ambiguous = _resolve_referrer(referrer)

    group_msgs, group_chat_ids = [], []
    for g in _referral_groups(person_id, referrer_pids)[:3]:
        group_chat_ids += g.get("chat_ids", [])
        for m in imessage.group_thread(g["chat_ids"], limit=40):
            if m.get("text"):
                who = m.get("sender") or ("me" if m["from_me"] else "them")
                group_msgs.append((who, m["text"]))

    cards = _vcards(person_id, group_chat_ids)

    sources = []
    if memory.strip():
        sources.append("memory")
    if thread:
        sources.append("thread")
    if group_msgs:
        sources.append("referral")
    if cards:
        sources.append("cards")
    return {"thread": thread, "group_msgs": group_msgs, "cards": cards,
            "referrer": referrer, "referrer_pids": referrer_pids,
            "ambiguous": ambiguous, "candidates": candidates,
            "sources": sources, "verdict": verdict}


# ---------- the model pass + grounded-or-held verification ----------

RESOLVE_PROMPT = """You are {owner}'s assistant. An unknown contact needs a real name. Using ONLY the evidence below, identify the contact's name. Never invent a name the evidence does not support.

Unknown contact handle: {handle}
{memory_block}{referrer_block}Recent messages in this contact's OWN thread (them = the unknown contact):
{thread}

{group_block}{cards_block}Reply with ONE JSON object and nothing else:
{{
  "full_name": "<the contact's full name, or empty string if the evidence doesn't reveal it>",
  "first_name": "<first name only, when that is all the evidence supports; else empty>",
  "class_hint": "<one of friend|family|business|service|company, or empty>",
  "confidence": "<high|medium|low>",
  "evidence": "<one short sentence: where the name/details came from>",
  "referral_fact": "<if a referrer connected them, one durable factual sentence like 'Introduced to {owner} by Eric Roth (Mar 2026)'; else empty>"
}}
Rules:
- Prefer a full name printed on a shared contact card or plainly stated in the messages.
- If the evidence only gives a first name, put it in first_name and leave full_name empty.
- referral_fact is provenance only — one factual sentence, no speculation."""


def _fmt_msgs(msgs, cap=30):
    return "\n".join(f"{who}: {t}" for who, t in msgs[-cap:] if t) or "(none)"


def _build_prompt(handle, memory, ev):
    from . import settings
    owner = settings.get("owner_name") or "the owner"
    memory_block = (f"What {owner} remembers about them:\n{memory.strip()}\n\n"
                    if memory.strip() else "")
    referrer_block = ""
    if ev["referrer"]:
        rb = f"They appear to have been introduced by {ev['referrer']}."
        if ev["ambiguous"]:
            rb += (f" (More than one contact is named {ev['referrer']}: "
                   f"{', '.join(ev['candidates'])}.)")
        referrer_block = rb + "\n\n"
    group_block = ""
    if ev["group_msgs"]:
        group_block = (f"Messages in the group thread they share with "
                       f"{ev['referrer'] or 'the referrer'} (where the "
                       f"introduction likely happened):\n"
                       f"{_fmt_msgs(ev['group_msgs'])}\n\n")
    cards_block = ""
    if ev["cards"]:
        cards_block = ("Shared contact card(s) exchanged in these threads:\n"
                       + "\n---\n".join(ev["cards"]) + "\n\n")
    return RESOLVE_PROMPT.format(
        owner=owner, handle=handle, memory_block=memory_block,
        referrer_block=referrer_block, thread=_fmt_msgs(ev["thread"]),
        group_block=group_block, cards_block=cards_block)


def _clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _class(s):
    s = _clean(s).lower()
    return s if s in _CLASSES else None


def _grounded(display, ev, memory):
    """Every significant token of the proposed name must appear (whole-word,
    normalized) somewhere in the gathered evidence — the owner's memory, the
    thread, the referral group, or a shared card. A name nothing supports is
    held: shown as a weak guess, never auto-committed. Same rule as
    journal._pid_checker's 'a guess nothing supports is corrected or held'."""
    hay = journal._norm("\n".join(
        [memory or ""]
        + [t for _who, t in ev["thread"]]
        + [t for _who, t in ev["group_msgs"]]
        + ev["cards"]))
    toks = [journal._norm(w) for w in display.split()]
    toks = [t for t in toks if len(t) > 2]
    return bool(toks) and all(journal._found_norm(t, hay) for t in toks)


def _finalize(data, ev, memory):
    name = _clean(data.get("full_name"))
    first = _clean(data.get("first_name"))
    conf = (_clean(data.get("confidence")) or "").lower()
    if conf not in ("high", "medium", "low"):
        conf = "medium"
    display = name or first
    held = bool(display) and not _grounded(display, ev, memory)
    if held:
        conf = "low"
    return {
        "name": name,
        "first_name": first if not name else "",
        "class_hint": _class(data.get("class_hint")),
        "confidence": conf,
        "held": held,
        "evidence": _clean(data.get("evidence"))[:240],
        "fact": (_clean(data.get("referral_fact"))[:240] or None),
        "referrer": ev["referrer"] or None,
        "ambiguous": ev["ambiguous"],
        "candidates": ev["candidates"],
        "sources": ev["sources"],
    }


def resolve(handle, person_id=None, memory=""):
    """Gather evidence, run one model pass, verify grounded-or-held, and
    return the proposal. Writes nothing."""
    ev = gather_evidence(handle, person_id, memory)
    data = suggest._extract_json(suggest.complete(_build_prompt(handle, memory, ev)))
    return _finalize(data, ev, memory)
