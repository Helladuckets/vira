"""Persistent, vault-grounded chat state for Find.

Find's ordinary search and one-shot Ask stay stateless.  This module owns the
other path: a session that can be resumed across browsers, plus the accumulated
Concept Cloud and Related cards derived from that same conversation.

Model calls happen outside the JSON-store lock.  The short compare-and-append
at the end rejects overlapping turns rather than silently interleaving them.
"""
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from . import jsonstore, suggest, vault

STORE = Path(__file__).resolve().parent.parent / "data" / "brain-chat.json"
MAX_SESSIONS = 20
MAX_TURNS = 40
MAX_CHUNKS = 8
MAX_CHUNK_CHARS = 2400
MAX_PRIOR_CONCEPTS = 60


class Conflict(RuntimeError):
    """The active session changed while a model answer was being produced."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _blank_store():
    return {"version": 1, "active_id": None, "sessions": {}}


def _new_session():
    now = _now()
    return {
        "id": "brain_" + secrets.token_hex(8),
        "started": now,
        "updated": now,
        "turns": [],
        "concepts": [],
        "follow_up_questions": [],
        "topic_clusters": [],
        "cited": [],
    }


def _public(session):
    """Copy the stored shape so callers cannot mutate a read result."""
    return json.loads(json.dumps(session))


def current():
    state = jsonstore.read(STORE, _blank_store())
    sid = state.get("active_id")
    session = (state.get("sessions") or {}).get(sid)
    return _public(session) if session else None


def new():
    session = _new_session()

    def update(state):
        state.setdefault("sessions", {})[session["id"]] = session
        state["active_id"] = session["id"]
        state["version"] = 1
        _prune_sessions(state)

    jsonstore.mutate(STORE, update, _blank_store(), indent=2,
                     ensure_ascii=False)
    return _public(session)


def _prune_sessions(state):
    sessions = state.get("sessions") or {}
    if len(sessions) <= MAX_SESSIONS:
        return
    oldest = sorted(sessions, key=lambda sid: sessions[sid].get("updated", ""))
    for sid in oldest[:len(sessions) - MAX_SESSIONS]:
        if sid != state.get("active_id"):
            sessions.pop(sid, None)


def _answer_question(question, prior_turns, hits):
    if not prior_turns:
        return vault.ask(question, hits=hits)
    transcript = []
    for turn in prior_turns[-8:]:
        transcript.append("User: " + str(turn.get("question") or "")[:1200])
        transcript.append("Assistant: " + str(turn.get("answer") or "")[:2400])
    contextual = (
        "Continue this vault-grounded conversation. Use the earlier exchange "
        "only as conversational context; support every factual claim with the "
        "retrieved vault notes and preserve [[wikilink]] citations.\n\n"
        "EARLIER EXCHANGE:\n" + "\n".join(transcript) +
        "\n\nCURRENT QUESTION:\n" + question
    )
    return vault.ask(contextual, hits=hits)


_CONCEPT_PROMPT = """You distill vault-chat sessions into a semantic concept cloud.

Return ONLY one JSON object with these keys:
- concepts: 8-12 concepts central to THIS turn. Each has term, weight (0..1),
  primary_path, and related_paths. Multi-word phrases are good. Avoid generic
  words. Every path must come from CHUNKS. Reuse the exact spelling of a term
  in PRIOR CONCEPTS when it is the same idea.
- follow_up_questions: exactly 3 short, concrete questions the owner might ask
  next, each at most 80 characters.
- topic_clusters: 0-3 objects with label (3-6 words) and paths. Each cluster
  needs at least 2 distinct paths from CHUNKS.

Do not invent paths. If the chunks do not support a cluster, return [].
"""


def _extract_json(text):
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise ValueError("concept model returned no JSON object")
    return json.loads(match.group(0))


def _concept_prompt(question, answer, hits, prior):
    chunks = []
    for i, hit in enumerate(hits[:MAX_CHUNKS], 1):
        heading = hit.get("heading") or hit.get("heading_path") or ""
        label = str(hit.get("path") or "(unknown)")
        if heading:
            label += " | " + str(heading)
        chunks.append(
            f"--- CHUNK {i} | {label} ---\n"
            + str(hit.get("text") or "")[:MAX_CHUNK_CHARS]
        )
    prior_text = ", ".join(
        f"{c.get('term')} (w={float(c.get('weight') or 0):.2f})"
        for c in prior[:MAX_PRIOR_CONCEPTS] if c.get("term")
    ) or "(none; this is the first turn)"
    return (
        _CONCEPT_PROMPT + "\nQUESTION:\n" + question[:4000]
        + "\n\nANSWER:\n" + answer[:8000]
        + "\n\nCHUNKS:\n" + "\n\n".join(chunks)
        + "\n\nPRIOR CONCEPTS:\n" + prior_text
    )


def _validate_concepts(raw, hits):
    valid = {str(h.get("path")) for h in hits if h.get("path")}
    concepts = []
    for item in raw.get("concepts") or []:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        primary = str(item.get("primary_path") or "")
        weight = item.get("weight")
        if not term or primary not in valid or not isinstance(weight, (int, float)):
            continue
        related = []
        for path in item.get("related_paths") or []:
            if path in valid and path != primary and path not in related:
                related.append(path)
        concepts.append({"term": term[:120],
                         "weight": max(0.0, min(1.0, float(weight))),
                         "primary_path": primary, "related_paths": related})
    followups = [str(q).strip()[:80] for q in
                 (raw.get("follow_up_questions") or [])
                 if isinstance(q, str) and q.strip()][:3]
    clusters = []
    for item in raw.get("topic_clusters") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        paths = []
        for path in item.get("paths") or []:
            if path in valid and path not in paths:
                paths.append(path)
        if label and len(paths) >= 2:
            clusters.append({"label": label[:100], "paths": paths})
    return concepts, followups, clusters[:3]


def _merge_concepts(prior, incoming):
    out = [dict(c, related_paths=list(c.get("related_paths") or []))
           for c in prior]
    by_term = {str(c.get("term") or "").lower().strip(): c for c in out}
    for item in incoming:
        key = item["term"].lower().strip()
        old = by_term.get(key)
        if old:
            old["turns"] = int(old.get("turns") or 1) + 1
            old["weight"] = min(
                1.0, max(float(old.get("weight") or 0), item["weight"])
                + 0.05 * (old["turns"] - 1))
            for path in item.get("related_paths") or []:
                if path != old.get("primary_path") and path not in old["related_paths"]:
                    old["related_paths"].append(path)
        else:
            added = dict(item, turns=1)
            out.append(added)
            by_term[key] = added
    return out


def _merge_cited(prior, citations, turn_number):
    by_path = {c.get("path"): dict(c) for c in prior if c.get("path")}
    for cite in citations:
        path = cite.get("path")
        if not path:
            continue
        item = by_path.setdefault(path, {
            "path": path, "title": cite.get("title") or Path(path).stem,
            "count": 0, "last_cited_in_turn": turn_number,
        })
        item["count"] = int(item.get("count") or 0) + 1
        item["last_cited_in_turn"] = turn_number
        if cite.get("title"):
            item["title"] = cite["title"]
    return sorted(by_path.values(),
                  key=lambda c: c.get("last_cited_in_turn", 0), reverse=True)


def ask(question, session_id=None):
    question = (question or "").strip()
    if not question:
        raise ValueError("empty question")

    state = jsonstore.read(STORE, _blank_store())
    sid = session_id or state.get("active_id")
    session = (state.get("sessions") or {}).get(sid)
    if not session:
        session = _new_session()
        sid = session["id"]
        expected_turns = 0
    else:
        session = _public(session)
        expected_turns = len(session.get("turns") or [])

    research_result = None
    try:
        from . import research
        if research.may_answer(question):
            research_result = research.answer_question(question)
    except Exception:  # a dormant graph must never break ordinary vault chat
        research_result = None
    if research_result:
        hits = research_result.get("hits") or []
        answer = {
            "answer": research_result.get("answer") or "",
            "citations": research_result.get("citations") or [],
            "hits": hits,
        }
    else:
        hits = vault.search(question, limit=10)
        answer = _answer_question(question, session.get("turns") or [], hits)
    answer_text = str(answer.get("answer") or "")

    concepts, followups, clusters = [], [], []
    if answer_text.strip() and hits:
        try:
            raw = _extract_json(suggest.complete(_concept_prompt(
                question, answer_text, hits, session.get("concepts") or [])))
            concepts, followups, clusters = _validate_concepts(raw, hits)
        except Exception:  # the answer is useful even if its companions fail
            pass

    turn_number = expected_turns + 1
    turn = {
        "question": question,
        "answer": answer_text,
        "citations": answer.get("citations") or [],
        "hits": answer.get("hits") or [],
        "created": _now(),
    }
    if research_result:
        turn["research"] = {
            key: value for key, value in research_result.items()
            if key not in {"answer", "hits", "citations"}
        }
    session["turns"] = (session.get("turns") or []) + [turn]
    session["turns"] = session["turns"][-MAX_TURNS:]
    session["concepts"] = _merge_concepts(session.get("concepts") or [], concepts)
    session["follow_up_questions"] = followups
    session["topic_clusters"] = clusters
    session["cited"] = _merge_cited(session.get("cited") or [],
                                    turn["citations"], turn_number)
    session["updated"] = _now()

    def commit(latest):
        sessions = latest.setdefault("sessions", {})
        found = sessions.get(sid)
        if found is not None and len(found.get("turns") or []) != expected_turns:
            raise Conflict("another chat turn finished first")
        if found is None and expected_turns:
            raise Conflict("chat session changed while answering")
        sessions[sid] = session
        latest["active_id"] = sid
        latest["version"] = 1
        _prune_sessions(latest)

    jsonstore.mutate(STORE, commit, _blank_store(), indent=2,
                     ensure_ascii=False)
    return _public(session)
