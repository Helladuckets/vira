"""Lesson recurrence — count how often each standing rule actually breaks.

The corrections ledger (~/.claude/LESSONS.md) is loaded into every session,
and nothing ever reads it back: when a later session re-learns a rule the
ledger already carries, the proposal pipeline drops the duplicate as ledger
hygiene, so the strongest recurrence signal is discarded. This module is
the read-back: match every session retrospective's reversal entries against
each standing rule, count how many DISTINCT SESSIONS have broken each rule
since it became active, and — once a tier-2 rule crosses the threshold —
stage ONE proposal in Vira's Queue describing the hook, test, or guard that
would make the mistake impossible. A tier-1 rule that recurs is a louder
finding: the mechanism did not hold (`guard_did_not_hold`).

Two rungs, the find.py split. RUNG 1 is deterministic and stands alone:
ledger parse, evidence gather (results JSON first, markdown retros as the
fallback), lexical + corpus-calibrated cosine scoring, and the verbatim
RESTATEMENT short-circuit — a later session independently proposing a rule
that is already on the ledger counts with no model at all. RUNG 2 is one
`suggest.complete` adjudication per rule per candidate batch, and every
`breaks: true` verdict must carry a quote that appears verbatim in that
candidate's text or it is demoted to false (the grounded-or-dropped
discipline of journal/_pid_checker, resolver._grounded, evidence).

TWO RULES THIS MODULE IS BENT AROUND:

1. VIRA NEVER WRITES THE LEDGER. lessons.py's own docstring: nothing
   enters that file automatically, because its lines change how every
   future session behaves. This module reads LESSONS.md and the two jsonl
   side stores and writes none of them — pinned by
   test_the_pass_never_writes_the_ledger. It also never proposes a tier-1
   LINE: tier 1 means a mechanism exists, and writing the line before the
   mechanism would make the ledger lie about its own enforcement (the
   branch-guard incident, 2026-07-29). The proposal is the WORK of
   building the mechanism, staged as a `proposed` idea behind the Queue's
   existing approval bar.

2. THE COUNTER MEASURES SUMMARIES, NOT GROUND TRUTH. Reversals are
   themselves model-written digests of sessions. The honest claim is "how
   often a session's own retrospective described something that reads as
   breaking this rule" — status() and every proposal say so.

Counting is DISTINCT SESSIONS, never evidence lines: one retro routinely
describes one incident across three bullets. Evidence is also deduped by
normalized text across sources (results -> session retros -> day retros),
because the day retro re-renders per-session bullets verbatim and each file
carries a different session key — without the text dedupe one incident
would count once per rendering.

Sections are cut with evidence.SECTION_RE over the RAW file text rather
than through evidence.parse_retro: that parser flattens each section body
to one whitespace-collapsed line (changelog._clean), which destroys the
bullet boundaries this module splits on. The section REGEX is shared so
the two cannot drift on what a heading is.

Store: data/lesson-recurrence.json (jsonstore discipline — fresh read,
locked mutate, atomic write). Derived and regenerable EXCEPT owner verdict
overrides (`owner: true`, never overwritten by a pass), rule dismissals,
and aliases — which is why the store is in the backup rotation.

No VIRA_PASSIVE guard, deliberately (the evidence.py reasoning): the pass
is read-only against files outside data/, and its one write — a `proposed`
idea — lands harmlessly in a test clone's cloned store. The weekly routine
does not run on a passive instance anyway.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

from . import admission, jsonstore, settings
from .evidence import DATE_RE, SECTION_RE, DEFAULT_RETRO_DIR

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "lesson-recurrence.json"
REPO_RETROS_DIR = ROOT / "retros"

# Rung-1 knobs. RESTATE_FLOOR is high on purpose: a restatement is counted
# with no model, so only a near-verbatim re-derivation qualifies.
RESTATE_FLOOR = 0.75
CANDIDATE_FLOOR = 0.45
EMBED_TIMEOUT_S = 90          # background pass; a request path never waits here
MAX_EVIDENCE_KEYS = 4000      # evidence + its verdicts prune together
MAX_RUNS = 20
PROPOSAL_MAX = 900

def _empty():
    """A FRESH default store. Never a module-level constant: jsonstore
    hands the default straight to the mutator, so a shared dict's nested
    buckets would silently accumulate every pass's state."""
    return {"rules": {}, "verdicts": {}, "evidence": {}, "runs": []}

_pass_lock = threading.Lock()


def _p(key, fallback):
    raw = str(settings.get(key) or "").strip()
    return Path(raw).expanduser() if raw else Path(fallback).expanduser()


# Named module globals for every source root — the readinglist isolation
# lesson (2026-07-28): tests repoint ALL of these at one tmp fixture, and
# _sources() declares the whole set in one place so a root added later
# cannot silently read the real machine.
LEDGER_PATH = _p("lessons_ledger_path", Path.home() / ".claude" / "LESSONS.md")
STATE_DIR = _p("lessons_state_dir", Path.home() / ".claude" / "sessions")
RESULTS_DIR = STATE_DIR / "results"
PROPOSED_PATH = STATE_DIR / "lessons-proposed.jsonl"
DECIDED_PATH = STATE_DIR / "lessons-decided.jsonl"


def _retro_dirs_default():
    raw = str(settings.get("lessons_retro_dirs") or "").strip()
    if raw:
        return [Path(p.strip()).expanduser() for p in raw.split(",") if p.strip()]
    er = str(settings.get("evidence_retro_dir") or "").strip()
    return [Path(er).expanduser() if er else DEFAULT_RETRO_DIR, REPO_RETROS_DIR]


RETRO_DIRS = _retro_dirs_default()


def _sources():
    """Every filesystem root this module reads. Tests iterate this to
    assert the whole set points into the fixture."""
    return {
        "ledger": LEDGER_PATH,
        "results": RESULTS_DIR,
        "proposed": PROPOSED_PATH,
        "decided": DECIDED_PATH,
        "retro_dirs": list(RETRO_DIRS),
        "store": STORE,
    }


# ---------------------------------------------------------------- identity

def _norm(t):
    """Lowercase, punctuation to spaces, whitespace collapsed — rule and
    evidence identity both derive from this."""
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def rule_id(text):
    return "lr_" + hashlib.sha1(_norm(text).encode("utf-8")).hexdigest()[:10]


def _evidence_id(session_key, text):
    raw = f"{session_key}|{_norm(text)}"
    return "ev_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- ledger

LINE_RE = re.compile(
    r"^- \[tier (\d+)\]\s+(.*?)\s+\((\d{4}-\d{2}-\d{2})\)(?:\s*<([^>]*)>)?\s*$")


def _read_jsonl(path):
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return out


def _active_from(text, stamp, proposed, decided_at):
    """The date a rule actually started being loaded into sessions, down a
    two-rung ladder: (1) join to a proposal row by normalized text (a
    proposal may be capped shorter than the ledger line, so prefix-match
    both ways past 40 chars), take its decided `at`; (2) the line's own
    stamp. Records which rung answered — today every live line is
    <manual>, so rung 1 is dead on this machine and status() says so
    rather than implying a precision the data does not have."""
    n = _norm(text)
    for row in proposed:
        pn = _norm(row.get("text") or "")
        if not pn:
            continue
        if pn == n or (min(len(pn), len(n)) >= 40
                       and (pn.startswith(n) or n.startswith(pn))):
            at = decided_at.get(row.get("id"))
            if at:
                return at[:10], "decided"
    return stamp, "line"


def ledger():
    """Parse LESSONS.md into rule records. A missing ledger is dormant
    (empty list), never an exception; a malformed line is ignored."""
    try:
        text = LEDGER_PATH.read_text(encoding="utf-8")
    except OSError:
        return []
    proposed = _read_jsonl(PROPOSED_PATH)
    decided_at = {r.get("id"): str(r.get("at") or "")
                  for r in _read_jsonl(DECIDED_PATH)
                  if r.get("status") == "approved"}
    rules = []
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        tier, body, stamp, source = (int(m.group(1)), m.group(2).strip(),
                                     m.group(3), (m.group(4) or "").strip())
        active, active_src = _active_from(body, stamp, proposed, decided_at)
        rules.append({
            "id": rule_id(body), "tier": tier, "text": body,
            "written": stamp, "source": source,
            "active_from": active, "active_from_source": active_src,
        })
    return rules


# ---------------------------------------------------------------- evidence

# Heading names are matched through _norm, so punctuation and case drift in
# the retro skill cannot silently drop a section. Widening either set is a
# one-line data edit.
REVERSAL_HEADINGS = {"reversals", "reversals and dead ends",
                     "what didn t work lessons learned", "what did not work"}
RESTATED_HEADINGS = {"lessons proposed", "lessons"}

_SESSION_ID_RE = re.compile(r"^session_id:\s*(\S+)", re.M)
_PROJECT_RE = re.compile(r"^project:\s*(\S+)", re.M)


def _bullets(body):
    """Split a raw section body into bullet texts; continuation lines fold
    onto their bullet. `_none_` and blanks are skipped."""
    out = []
    for line in body.splitlines():
        s = line.rstrip()
        if not s.strip():
            continue
        if s.lstrip().startswith("- "):
            out.append(s.lstrip()[2:].strip())
        elif out:
            out[-1] += " " + s.strip()
    return [b for b in out if b and _norm(b) not in ("none", "")]


def _lesson_text(entry):
    if isinstance(entry, dict):
        return str(entry.get("text") or "").strip()
    return str(entry or "").strip()


def _results_items():
    items = []
    if not RESULTS_DIR.is_dir():
        return items
    for f in sorted(RESULTS_DIR.glob("*/*.json"), reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        row = d.get("row") or {}
        obj = d.get("obj") or {}
        skey = str(row.get("session_id") or f.stem)
        day = str(row.get("day") or f.parent.name)
        project = str(row.get("project") or "")
        for kind, entries in (("reversal", obj.get("reversals") or []),
                              ("restated", obj.get("lessons") or [])):
            for e in entries:
                text = _lesson_text(e)
                if text:
                    items.append({"kind": kind, "text": text, "day": day,
                                  "project": project, "session_id": skey,
                                  "retro": "", "source": str(f)})
    return items


def _retro_files():
    out = []
    for d in RETRO_DIRS:
        if d.is_dir():
            # every project's retros — the ledger is global, and scoping to
            # one project would silently measure a fraction of the evidence
            out.extend(sorted(d.glob("*.md"), reverse=True))
    return out


def _markdown_items():
    items = []
    for f in _retro_files():
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = _SESSION_ID_RE.search(text)
        skey = m.group(1) if m else f.stem
        m = DATE_RE.search(text)
        day = m.group(1).strip() if m else ""
        if not day:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", f.name)
            day = m.group(1) if m else ""
        m = _PROJECT_RE.search(text)
        project = m.group(1) if m else ""
        for sm in SECTION_RE.finditer(text):
            h = _norm(sm.group(1))
            kind = ("reversal" if h in REVERSAL_HEADINGS
                    else "restated" if h in RESTATED_HEADINGS else None)
            if not kind:
                continue
            for b in _bullets(sm.group(2)):
                items.append({"kind": kind, "text": b, "day": day,
                              "project": project, "session_id": skey,
                              "retro": str(f), "source": str(f)})
    return items


def _evidence():
    """Every reversal/restatement item, shaped {id, kind, text, day,
    project, session_id, retro, source}. Results JSON is the primary (the
    markdown is a rendering of it); dedupe is by id AND by normalized
    text, first source wins — see the module docstring."""
    out, seen_ids, seen_text = [], set(), set()
    for item in _results_items() + _markdown_items():
        eid = _evidence_id(item["session_id"], item["text"])
        n = _norm(item["text"])
        if eid in seen_ids or n in seen_text:
            continue
        seen_ids.add(eid)
        seen_text.add(n)
        item["id"] = eid
        out.append(item)
    return out


# ---------------------------------------------------------------- scoring

_STOP = {"the", "and", "for", "that", "this", "with", "was", "were", "not",
         "never", "always", "its", "his", "her", "their", "from", "into",
         "has", "have", "had", "are", "but", "when", "what", "which", "one",
         "out", "all", "any", "can", "could", "would", "should", "does",
         "did", "than", "then", "them", "they", "you", "your", "over",
         "before", "after", "session", "sessions"}


def _tokens(t):
    return {w for w in re.findall(r"[a-z0-9]+", (t or "").lower())
            if len(w) >= 3 and w not in _STOP}


def _tok_score(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _embed(texts):
    """Unit vectors via Ollama, or None (basis degrades to text). The
    network wait stays OUTSIDE the CPU gate."""
    try:
        from . import localmodels
        return localmodels.ollama_embed(texts, timeout=EMBED_TIMEOUT_S)
    except Exception:
        return None


def _rel_matrix(rule_vecs, ev_vecs):
    """Rule-by-evidence cosine, calibrated RELATIVE to this corpus: the
    median pair maps to 0 and the 99th percentile to 1 (the ideatags
    measurement — on a homogeneous corpus a fixed cosine floor matches
    everything or nothing)."""
    import numpy as np
    R = np.stack(rule_vecs)
    E = np.stack(ev_vecs)
    M = R @ E.T
    flat = M.flatten()
    if flat.size < 4:
        return None
    med = float(np.median(flat))
    p99 = float(np.percentile(flat, 99))
    if p99 - med < 1e-6:
        return None
    return np.clip((M - med) / (p99 - med), 0.0, 1.0)


# ---------------------------------------------------------------- rung 2

def _adjudicate_prompt(rule, cands):
    lines = [
        "You are auditing whether session retrospectives describe breaking",
        "ONE standing rule the agent had already been given.",
        "",
        f"THE RULE (tier {rule['tier']}, active since {rule['active_from']}):",
        f"\"{rule['text']}\"",
        "",
        "CANDIDATES — each is one entry from a session's retrospective:",
        "",
    ]
    for c in cands:
        lines.append(f"[{c['id']}] ({c.get('project') or '?'}, "
                     f"{c.get('day') or '?'})")
        lines.append(c["text"])
        lines.append("")
    lines += [
        "For each candidate, decide breaks: true ONLY if the entry",
        "describes the same mistake this rule forbids, in a situation",
        "where the rule applied.",
        "- An entry about a DIFFERENT mistake that merely shares",
        "  vocabulary is not a break.",
        "- The rule must have been applicable to what that session was",
        "  actually doing.",
        "- When in doubt, false. A wrong true proposes changing how every",
        "  future session behaves.",
        "",
        "Reply with STRICT JSON only, every candidate id exactly once:",
        '{"verdicts": [{"id": "ev_...", "breaks": true,',
        ' "quote": "<verbatim span copied from THAT candidate\'s text>",',
        ' "why": "<one line>"}]}',
        "For breaks: false the quote may be empty. A quote that is not a",
        "verbatim copy from that candidate is discarded.",
    ]
    return "\n".join(lines)


def _squash(t):
    return re.sub(r"\s+", " ", (t or "")).strip().lower()


def _clean_verdicts(raw, cands):
    """The grounded contract, in order: unknown/duplicate ids dropped; a
    breaks:true whose quote does not appear (whitespace-normalized) in
    that candidate's text is DEMOTED to false and counted; a candidate the
    model did not mention is false — silence is not evidence."""
    by_id = {c["id"]: c for c in cands}
    out, ungrounded, seen = {}, 0, set()
    for v in (raw or {}).get("verdicts") or []:
        if not isinstance(v, dict):
            continue
        vid = str(v.get("id") or "")
        if vid not in by_id or vid in seen:
            continue
        seen.add(vid)
        breaks = bool(v.get("breaks"))
        quote = str(v.get("quote") or "").strip()[:400]
        why = str(v.get("why") or "").strip()[:300]
        if breaks and (not quote or _squash(quote) not in
                       _squash(by_id[vid]["text"])):
            breaks, quote = False, ""
            ungrounded += 1
            why = (why + " [demoted: ungrounded quote]").strip()
        out[vid] = {"breaks": breaks, "quote": quote, "why": why}
    for c in cands:
        if c["id"] not in out:
            out[c["id"]] = {"breaks": False, "quote": "",
                            "why": "not addressed by the model"}
    return out, ungrounded


def _adjudicate(rule, cands):
    from . import suggest
    raw = suggest._extract_json(
        suggest.complete(_adjudicate_prompt(rule, cands)))
    return _clean_verdicts(raw, cands)


# ---------------------------------------------------------------- counting

def _breaks_for(rid, store):
    """Verdicted breaks for one rule, honoring owner overrides, joined to
    their evidence rows."""
    rows = []
    for key, v in (store.get("verdicts") or {}).items():
        if not key.startswith(rid + ":"):
            continue
        if not v.get("breaks"):
            continue
        ev = (store.get("evidence") or {}).get(key.split(":", 1)[1])
        if ev:
            rows.append({**ev, "id": key.split(":", 1)[1],
                         "quote": v.get("quote") or "",
                         "why": v.get("why") or "",
                         "kind": v.get("kind") or ev.get("kind") or "",
                         "owner": bool(v.get("owner"))})
    rows.sort(key=lambda r: r.get("day") or "", reverse=True)
    return rows


def _count_sessions(rows):
    return len({r.get("session_id") or r.get("id") for r in rows})


# ---------------------------------------------------------------- proposal

def _promote_at():
    return max(1, int(settings.get("lesson_promote_at")))


def _cands_per_rule():
    return max(1, int(settings.get("lesson_candidates_per_rule")))


def _verified_citations(rows):
    """A citation must resolve on disk — the provenance-system rule. Retro
    paths and results files both count; anything else is dropped."""
    cites = []
    for r in rows:
        for p in (r.get("retro"), r.get("source")):
            if p and Path(p).exists():
                cites.append(p)
                break
    return sorted(set(cites))


def _mechanism_line(rule, rows):
    """Rung 2 of the proposal: one model call recommending the mechanism
    family. Validated (non-trivial, capped); any failure falls back to a
    deterministic line rather than blocking the proposal."""
    try:
        from . import suggest
        quotes = "\n".join(f"- {r['quote'] or r['text']}"[:300]
                           for r in rows[:6])
        prompt = (
            "A standing rule for coding-agent sessions keeps being broken. "
            "Recommend, in ONE short paragraph (under 500 characters), the "
            "concrete mechanism that would make the mistake impossible, "
            "picking a family: a preflight check row (for a repo "
            "invariant), a session hook (for session behaviour with no "
            "repo artifact), or a test / runner-gate branch (for an "
            "app-internal invariant). Name what the mechanism inspects "
            "and when it fires. No preamble.\n\n"
            f"THE RULE: {rule['text']}\n\nHOW IT KEEPS BREAKING:\n{quotes}"
        )
        line = str(suggest.complete(prompt) or "").strip()
        line = re.sub(r"\s+", " ", line)
        if 40 <= len(line) <= 600:
            return line
    except Exception:
        pass
    return ("Build the preflight row, hook, or test that makes this "
            "mistake impossible, then move the ledger line to tier 1 "
            "via lessons.py.")


def _propose_promotion(rule, rows, store_rules, force=False):
    """Stage the WORK of building the mechanism as a `proposed` idea.
    Minted once: no second proposal while the last one's idea is still
    proposed/open, and after a decline not until the count grows by
    another promote_at (a declined proposal must not permanently silence
    a rule that goes on being broken)."""
    from . import ideas
    rec = store_rules.get(rule["id"]) or {}
    count = _count_sessions(rows)
    prior = rec.get("proposal")
    if prior and not force:
        idea = next((i for i in ideas.list_items()
                     if i.get("id") == prior.get("idea_id")), None)
        if idea and idea.get("status") in ("proposed", "open", "on-hold"):
            return None
        if count < int(prior.get("count") or 0) + _promote_at():
            return None
    sessions = "; ".join(
        f"{r.get('project') or '?'} {r.get('day') or '?'}"
        for r in rows[:6])
    cites = _verified_citations(rows)
    text = (
        f"Promote a standing rule to a mechanism: \"{rule['text']}\" "
        f"(tier {rule['tier']}, active since {rule['active_from']}) has "
        f"been described as broken in {count} sessions' own "
        f"retrospectives ({sessions}). {_mechanism_line(rule, rows)} "
        "Counts come from model-written retro summaries, not an audited "
        "log."
    )[:PROPOSAL_MAX]
    note = ("evidence: " + "; ".join(cites[:5])) if cites else ""
    item = ideas.add(text, status="proposed", source="lesson-recurrence",
                     note=note, project="Vira")
    return {"idea_id": item["id"], "at": _now(), "count": count}


# ---------------------------------------------------------------- the pass

def _now():
    return datetime.now().isoformat(timespec="seconds")


def run_pass(adjudicate=True):
    """The whole counter, one run: gather -> score -> restatements ->
    adjudicate new candidates -> store -> counts -> proposals/flags.
    Returns the run record. Serialized: two concurrent passes would
    adjudicate the same pairs twice for nothing."""
    with _pass_lock:
        return _run_pass_locked(adjudicate)


def _run_pass_locked(adjudicate):
    rules = ledger()
    if not rules:
        return {"dormant": True,
                "reason": f"no corrections ledger at {LEDGER_PATH}"}
    evidence_items = _evidence()
    prior = jsonstore.read(STORE, _empty())
    prior_verdicts = prior.get("verdicts") or {}

    # per-rule in-window evidence: a rule cannot be broken before it exists
    windows = {}
    for r in rules:
        windows[r["id"]] = [e for e in evidence_items
                            if (e.get("day") or "") >= r["active_from"]]

    # vectors: one Ollama batch, outside the CPU gate
    all_in_window = {e["id"]: e for evs in windows.values() for e in evs}
    ev_list = list(all_in_window.values())
    basis = "text"
    rel = None
    if ev_list:
        vecs = _embed([r["text"] for r in rules] +
                      [e["text"] for e in ev_list])
        if vecs:
            rel = _rel_matrix(vecs[:len(rules)], vecs[len(rules):])
            if rel is not None:
                basis = "hybrid"
    ev_index = {e["id"]: i for i, e in enumerate(ev_list)}

    def _score_all():
        scored = {}
        for ri, r in enumerate(rules):
            rows = []
            for e in windows[r["id"]]:
                tok = _tok_score(r["text"], e["text"])
                if rel is not None:
                    s = 0.6 * float(rel[ri][ev_index[e["id"]]]) + 0.4 * tok
                else:
                    s = tok
                rows.append((s, tok, e))
            rows.sort(key=lambda t: (t[2].get("day") or "", t[0]),
                      reverse=True)
            scored[r["id"]] = rows
        return scored

    # scoring is CPU work; take a gate slot only on a request thread —
    # background passes are not what the gate protects (admission doctrine)
    if admission.is_worker():
        with admission.cpu("lessonwatch"):
            scored = _score_all()
    else:
        scored = _score_all()

    new_verdicts = {}
    pending = {}
    adjudicate_error = ""
    candidates_total = dropped_total = adjudicated = ungrounded_total = 0
    cap = _cands_per_rule()
    for r in rules:
        rid = r["id"]
        cands = []
        for s, tok, e in scored[rid]:
            key = f"{rid}:{e['id']}"
            if key in prior_verdicts or key in new_verdicts:
                continue
            if e["kind"] == "restated" and tok >= RESTATE_FLOOR:
                # rung 1 counts the strongest evidence class with no model
                new_verdicts[key] = {"breaks": True, "kind": "restated",
                                     "quote": e["text"][:400],
                                     "why": "rule independently restated "
                                            "by a later session",
                                     "owner": False, "at": _now()}
                continue
            if s >= CANDIDATE_FLOOR:
                cands.append(e)
        candidates_total += len(cands)
        if len(cands) > cap:
            dropped_total += len(cands) - cap   # logged, never silent
            cands = cands[:cap]
        if not cands:
            continue
        if not adjudicate:
            pending[rid] = [c["id"] for c in cands]
            continue
        try:
            verdicts, ung = _adjudicate(r, cands)
        except Exception as e:  # model down: candidates stay pending
            pending[rid] = [c["id"] for c in cands]
            adjudicate_error = str(e)[:200]
            continue
        adjudicated += len(verdicts)
        ungrounded_total += ung
        for vid, v in verdicts.items():
            new_verdicts[f"{rid}:{vid}"] = {
                **v, "kind": "reversal", "owner": False, "at": _now()}

    run = {"at": _now(), "rules": len(rules), "evidence": len(ev_list),
           "candidates": candidates_total, "adjudicated": adjudicated,
           "ungrounded_dropped": ungrounded_total,
           "candidates_dropped": dropped_total, "basis": basis,
           "proposals": 0, "flags": 0}
    if adjudicate_error:
        run["adjudicate_error"] = adjudicate_error

    def _mutate(s):
        s.setdefault("rules", {})
        s.setdefault("verdicts", {})
        s.setdefault("evidence", {})
        s.setdefault("runs", [])
        live_ids = set()
        for r in rules:
            live_ids.add(r["id"])
            rec = s["rules"].setdefault(r["id"], {})
            rec.update({k: r[k] for k in
                        ("text", "tier", "written", "source", "active_from",
                         "active_from_source")})
            rec.setdefault("dismissed", False)
            rec.setdefault("aliases", [])
            rec["retired"] = False
            rec["pending"] = pending.get(r["id"], [])
            rec["last_seen"] = _now()
            rec.setdefault("first_seen", _now())
        for rid, rec in s["rules"].items():
            if rid not in live_ids:
                # evicted from the ledger, counts kept — worth showing
                rec["retired"] = True
        for e in ev_list:
            s["evidence"][e["id"]] = {k: e[k] for k in
                                      ("text", "day", "project",
                                       "session_id", "retro", "kind")}
        for key, v in new_verdicts.items():
            # an existing verdict is never re-adjudicated, and an
            # owner-marked one is never overwritten — setdefault is both
            s["verdicts"].setdefault(key, v)
        # prune evidence and its verdicts TOGETHER, oldest day first, so a
        # verdict never outlives the item it cites
        if len(s["evidence"]) > MAX_EVIDENCE_KEYS:
            stamps = {k: (v.get("day") or "")
                      for k, v in s["evidence"].items()}
            jsonstore.prune_oldest(stamps, MAX_EVIDENCE_KEYS)
            dead = set(s["evidence"]) - set(stamps)
            for k in dead:
                s["evidence"].pop(k, None)
            for key in [k for k in s["verdicts"]
                        if k.split(":", 1)[1] in dead]:
                s["verdicts"].pop(key, None)

        # counts -> flags (tier 1) and promotion CANDIDATES (tier 2). The
        # proposal itself is a model call and the tier-1 ping is an
        # outbound send — both slow, both run OUTSIDE this file lock.
        for r in rules:
            rec = s["rules"][r["id"]]
            rows = _breaks_for(r["id"], s)
            count = _count_sessions(rows)
            prev_count = int(rec.get("count") or 0)
            rec["count"] = count
            rec["last_broken"] = rows[0].get("day") if rows else ""
            if r["tier"] == 1:
                rec["guard_did_not_hold"] = count > 0
                if count > prev_count:
                    run["flags"] += 1
                    to_ping.append((r, count))
            elif (count >= _promote_at() and not rec.get("dismissed")):
                to_propose.append((r, rows, dict(s["rules"])))
        s["runs"] = (s["runs"] + [run])[-MAX_RUNS:]
        return s

    to_ping, to_propose = [], []
    jsonstore.mutate(STORE, _mutate, _empty(), indent=1)
    for r, count in to_ping:
        _ping_tier1(r, count)
    proposals = {}
    for r, rows, store_rules in to_propose:
        prop = _propose_promotion(r, rows, store_rules)
        if prop:
            proposals[r["id"]] = prop
            run["proposals"] += 1
    if proposals or run["proposals"]:
        def _record(s):
            for rid, prop in proposals.items():
                s.setdefault("rules", {}).setdefault(rid, {})["proposal"] = prop
            if s.get("runs"):
                s["runs"][-1]["proposals"] = run["proposals"]
        jsonstore.mutate(STORE, _record, _empty(), indent=1)
    return run


def _ping_tier1(rule, count):
    """A tier-1 recurrence means the mechanism did not hold — the
    branch-guard failure class, rare and worth the phone. Best-effort;
    notify's own throttles dedupe, and a passive clone's send path is
    blocked at send_message."""
    try:
        from . import notify
        notify.agent_ping(
            f"Vira: a tier-1 rule reads as broken ({count} sessions) — "
            f"\"{rule['text'][:140]}\" — the guard did not hold.",
            key=f"lesson-t1:{rule['id']}")
    except Exception:
        pass


# ---------------------------------------------------------------- reads

def _vault_rel(path):
    """A retro that lives inside the connected vault gets a vault-relative
    path, so the UI can open it in the note panel; anything else is ''."""
    if not path:
        return ""
    try:
        from . import vault
        return str(Path(path).resolve().relative_to(
            Path(vault.vault_root()).resolve()))
    except Exception:
        return ""


def report():
    """The panel's payload: every rule (live + retired) with its count,
    breaks, flags and proposal. Read-only, no model."""
    live = {r["id"]: r for r in ledger()}
    s = jsonstore.read(STORE, _empty())
    rows = []
    ids = set(live) | set(s.get("rules") or {})
    for rid in ids:
        rec = dict((s.get("rules") or {}).get(rid) or {})
        base = live.get(rid)
        if base:
            rec.update(base)
            rec["retired"] = False
        elif not rec:
            continue
        breaks = _breaks_for(rid, s)
        for b in breaks:
            b["note"] = _vault_rel(b.get("retro"))
        rows.append({
            "id": rid, "text": rec.get("text", ""),
            "tier": int(rec.get("tier") or 2),
            "written": rec.get("written", ""),
            "active_from": rec.get("active_from", ""),
            "active_from_source": rec.get("active_from_source", "line"),
            "count": _count_sessions(breaks),
            "last_broken": breaks[0].get("day") if breaks else "",
            "breaks": breaks,
            "pending": len(rec.get("pending") or []),
            "retired": bool(rec.get("retired")),
            "dismissed": bool(rec.get("dismissed")),
            "guard_did_not_hold": bool(rec.get("guard_did_not_hold")),
            "at_threshold": (int(rec.get("tier") or 2) == 2
                             and _count_sessions(breaks) >= _promote_at()),
            "proposal": rec.get("proposal"),
        })
    # tier-1 failures lead — a guard found absent is the headline
    rows.sort(key=lambda r: (not r["guard_did_not_hold"], -r["count"],
                             r["text"]))
    return {"rules": rows, "status": status()}


def status():
    s = jsonstore.read(STORE, _empty())
    rules = ledger()
    runs = s.get("runs") or []
    last = runs[-1] if runs else None
    return {
        "ledger_path": str(LEDGER_PATH),
        "ledger_exists": LEDGER_PATH.exists(),
        "rules": len(rules),
        "tier1": sum(1 for r in rules if r["tier"] == 1),
        "tier2": sum(1 for r in rules if r["tier"] == 2),
        "active_from_sources": {
            src: sum(1 for r in rules if r["active_from_source"] == src)
            for src in ("decided", "line")},
        "pending_adjudication": sum(
            len(rec.get("pending") or [])
            for rec in (s.get("rules") or {}).values()
            if isinstance(rec.get("pending"), list)),
        "promote_at": _promote_at(),
        "last_run": last,
        "note": ("Counts describe what each session's own retrospective "
                 "said — model-written summaries, not an audited log."),
    }


# ---------------------------------------------------------------- owner ops

def set_dismissed(rid, dismissed):
    def _m(s):
        rec = (s.get("rules") or {}).get(rid)
        if not rec:
            raise KeyError(rid)
        rec["dismissed"] = bool(dismissed)
    jsonstore.mutate(STORE, _m, _empty(), indent=1)
    return {"id": rid, "dismissed": bool(dismissed)}


def set_break(rid, evidence_id, breaks):
    """The owner override: outranks the model and is sticky — no later
    pass overwrites an owner-marked verdict (a correction written where
    the next pass rewrites it is a correction with a shelf life)."""
    key = f"{rid}:{evidence_id}"

    def _m(s):
        v = (s.get("verdicts") or {}).get(key)
        if not v:
            if evidence_id not in (s.get("evidence") or {}):
                raise KeyError(key)
            v = {"kind": "reversal", "quote": "", "why": ""}
            s.setdefault("verdicts", {})[key] = v
        v["breaks"] = bool(breaks)
        v["owner"] = True
        v["why"] = ((v.get("why") or "") + " [owner override]").strip()
        v["at"] = _now()
        rec = (s.get("rules") or {}).get(rid)
        if rec is not None:
            rows = _breaks_for(rid, s)
            rec["count"] = _count_sessions(rows)
            rec["last_broken"] = rows[0].get("day") if rows else ""
    jsonstore.mutate(STORE, _m, _empty(), indent=1)
    return {"key": key, "breaks": bool(breaks), "owner": True}


def force_propose(rid):
    """Right-click 'Propose promotion now'. 409-shaped ValueError when a
    live proposal already exists; KeyError on an unknown rule."""
    rules = {r["id"]: r for r in ledger()}
    rule = rules.get(rid)
    if not rule:
        raise KeyError(rid)
    if rule["tier"] == 1:
        raise ValueError("tier-1 rules are a defect report, not a "
                         "promotion candidate")
    from . import ideas
    s = jsonstore.read(STORE, _empty())
    rec = (s.get("rules") or {}).get(rid) or {}
    prior = rec.get("proposal")
    if prior:
        idea = next((i for i in ideas.list_items()
                     if i.get("id") == prior.get("idea_id")), None)
        if idea and idea.get("status") in ("proposed", "open", "on-hold"):
            raise ValueError("a proposal for this rule is already live")
    rows = _breaks_for(rid, s)
    prop = _propose_promotion(rule, rows, s.get("rules") or {}, force=True)

    def _m(st):
        st.setdefault("rules", {}).setdefault(rid, {})["proposal"] = prop
    jsonstore.mutate(STORE, _m, _empty(), indent=1)
    return prop


# ---------------------------------------------------------------- CLI

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "run":
        out = run_pass(adjudicate="--no-adjudicate" not in sys.argv)
    elif cmd == "report":
        out = report()
    else:
        out = status()
    print(json.dumps(out, indent=1))
