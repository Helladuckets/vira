"""Idea tagging + similarity — the layer that makes the Queue searchable,
groupable, and self-aware about repetition.

The backlog was chronological and untyped: 135 ideas whose only axis was
the owner-curated `project`, so nothing could say "you had this idea
yesterday" and nothing could gather the six scattered Reader ideas into
one thing to decide about. This module adds the two derived layers that
answer both questions.

TWO RUNGS, the find.py split, and rung 1 stands on its own:

  1. DETERMINISTIC — text tokens and (when Ollama is up) one
     nomic-embed-text vector per idea. No model backend, no network past
     localhost. This alone powers similarity, the duplicate nudge on add,
     and the related-ideas block at dispatch. Ollama down degrades to
     token overlap and SAYS SO (`basis`), rather than quietly returning a
     thinner list that reads as "nothing is similar".
  2. TAGS — one `suggest.complete` pass per batch of ideas, over four
     axes beyond the owner's `project`: module, subproject, theme,
     concept. The model proposes; `_clean_tags` validates against the
     axis table and the shape rules before anything is stored, the
     update_module_map discipline.

THE VOCABULARY IS THE WHOLE POINT. Tagging each idea independently
produces "reader", "Reader", "reading-room" and "reader-queue" — four
tags for one subject, which groups nothing. Every tagging pass is handed
the vocabulary already in use and told to reuse it unless nothing fits,
so the tag set converges instead of fanning out.

DERIVED, THEREFORE REGENERABLE. `data/idea-index.json` is a sidecar
keyed by idea id and content hash: edit an idea's text and its tags and
vector are recomputed on the next pass. Nothing here is canonical, so it
is not in the backup rotation and deleting it costs only the recompute.
Owner corrections are the exception — they live on the idea record in
ideas.json (`tags_add` / `tags_drop`) and are overlaid at read time, the
contactcard.py pattern: a correction written into a derived field is a
correction with a shelf life.
"""
import base64
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import ideas, jsonstore, localmodels, suggest

try:
    import numpy as np
except ImportError:  # minimal install: token overlap still works
    np = None

STORE = Path(__file__).resolve().parent.parent / "data" / "idea-index.json"

# The tag axes, beyond the owner-curated `project`. A table, so adding an
# axis is a data edit: id, the label the UI shows, how many tags one idea
# may carry on it, and the sentence that teaches the model what belongs
# there (the prompt is generated from this, so the two cannot drift).
AXES = (
    {"id": "module", "label": "Module", "max": 3,
     "hint": "the part of the product this touches — a window, surface, or "
             "engine (queue, reader, brief, people, design-studio, "
             "job-runner, media-index). Name what the change lands in."},
    {"id": "subproject", "label": "Sub-project", "max": 2,
     "hint": "the larger named effort this belongs to, if any "
             "(cross-platform, onboarding, public-release, agentic-os, "
             "job-search). Leave empty when the idea stands alone."},
    {"id": "theme", "label": "Theme", "max": 2,
     "hint": "the recurring concern it serves — what KIND of problem this "
             "is, across modules (search-and-recall, mobile-layout, "
             "trust-and-verification, provenance, self-awareness, "
             "performance). Two ideas in different modules that fix the "
             "same class of problem must share a theme."},
    {"id": "concept", "label": "Concept", "max": 3,
     "hint": "the underlying mechanism or object the idea is about "
             "(deduplication, embeddings, tagging, two-way-sync, "
             "permission-gate, worktree). The noun a future search would "
             "reach for."},
)
AXIS_IDS = tuple(a["id"] for a in AXES)
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
BATCH = 12                 # ideas per model call
VOCAB_SHOWN = 60           # tags per axis handed to the tagger

# Similarity fusion. Cosine dominates when vectors exist because it is the
# only signal that catches two ideas that share no words; tags carry the
# grouping the owner actually reads; tokens are the floor that works with
# neither.
W_VEC, W_TAG, W_TOK = 0.55, 0.30, 0.15
RELATED_FLOOR = 0.34       # "worth showing at dispatch"
DUP_FLOOR = 0.62           # "you may already have had this idea"

_STOP = frozenset("""
the a an and or but if then than that this these those there here it its
is are was were be been being do does did done have has had of in on at to
for from with without into onto over under about across as by so such not
no yes can could should would will shall may might must i me my we our you
your they them their he she his her when while where who whom which what
how why all any each every some most more less least very just also only
one two get got make made need needs want wants use used using way ways
thing things something anything everything now new old same other another
still even both after before again once too own off out up down again
""".split())


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


# ---------- store ----------

def _read():
    s = jsonstore.read(STORE, {"entries": {}})
    if not isinstance(s, dict) or not isinstance(s.get("entries"), dict):
        s = {"entries": {}}
    return s


def _write(s):
    jsonstore.write_atomic(STORE, s, indent=1, ensure_ascii=False)


def _prune(s, live_ids):
    """Drop entries for ideas that no longer exist. The sidecar is keyed by
    idea id, and a deleted idea's vector would otherwise keep scoring
    against new ones forever.

    CALL THIS ONLY WITH THE FULL BACKLOG. `live_ids` is read as "every
    idea that exists", so handing it a subset deletes everything else —
    which is exactly what happened when embed_pending([one_idea]) pruned
    134 of 135 entries and silently destroyed the whole derived layer.
    refresh() is the only caller, and it loads the list itself."""
    dead = [k for k in s["entries"] if k not in live_ids]
    for k in dead:
        del s["entries"][k]
    return bool(dead)


# ---------- tags ----------

def norm_tag(t):
    """Free text -> the one spelling a tag is allowed to have: lowercase
    kebab. 'Reading Room' and 'reading_room' must not become two tags."""
    t = re.sub(r"[^a-z0-9]+", "-", str(t or "").strip().lower()).strip("-")
    return t if TAG_RE.match(t) else ""


def _clean_tags(raw):
    """Model output -> a validated {axis: [tag]} map. Unknown axes, bad
    spellings and over-long lists are dropped rather than stored: the
    model proposes, this function decides."""
    out = {}
    for ax in AXES:
        vals = raw.get(ax["id"]) or []
        if isinstance(vals, str):
            vals = [vals]
        seen, keep = set(), []
        for v in vals:
            t = norm_tag(v)
            if t and t not in seen:
                seen.add(t)
                keep.append(t)
            if len(keep) >= ax["max"]:
                break
        out[ax["id"]] = keep
    return out


def _overlay(derived, item):
    """Derived tags with the owner's corrections applied — added tags win,
    dropped tags lose, on every axis. Kept out of the sidecar on purpose:
    a re-tag pass rewrites derived values, and an owner correction stored
    there would be silently reverted by the next pass."""
    add = item.get("tags_add") or {}
    drop = {norm_tag(t) for t in (item.get("tags_drop") or [])}
    out = {}
    for ax in AXIS_IDS:
        vals = list(derived.get(ax) or [])
        for t in (add.get(ax) or []):
            t = norm_tag(t)
            if t and t not in vals:
                vals.append(t)
        out[ax] = [t for t in vals if t not in drop]
    return out


def tags_for(item, s=None):
    """The tags one idea actually carries, corrections included."""
    s = s or _read()
    e = s["entries"].get(item["id"]) or {}
    return _overlay(e.get("tags") or {}, item)


def annotate(items=None):
    """Every idea with `tags` (and `tagged`) attached — what /api/ideas
    serves. One store read for the whole list."""
    items = ideas.list_items() if items is None else items
    s = _read()
    out = []
    for it in items:
        e = s["entries"].get(it["id"]) or {}
        row = dict(it)
        row["tags"] = _overlay(e.get("tags") or {}, it)
        row["tagged"] = bool(e.get("tagged")) or any(row["tags"].values())
        out.append(row)
    return out


def vocabulary(items=None, s=None):
    """{axis: [(tag, count)]} over the whole backlog, commonest first —
    the tag cloud the UI filters by and the vocabulary the tagger is told
    to reuse."""
    items = ideas.list_items() if items is None else items
    s = s or _read()
    counts = {ax: {} for ax in AXIS_IDS}
    for it in items:
        for ax, vals in tags_for(it, s).items():
            for t in vals:
                counts.setdefault(ax, {})
                counts[ax][t] = counts[ax].get(t, 0) + 1
    return {ax: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))
            for ax, c in counts.items()}


# ---------- vectors ----------

def _pack(v):
    return base64.b64encode(v.astype("float16").tobytes()).decode()


def _unpack(b):
    return np.frombuffer(base64.b64decode(b), dtype="float16").astype("float32")


def _embed_text(it):
    """What gets embedded: the idea plus its project, so two ideas about
    the same subject in different projects sit slightly apart."""
    return f"{it.get('project') or ''}: {it.get('text') or ''}"[:2000]


def embed_pending(items=None, limit=200):
    """Fill in missing/stale vectors. Deterministic and local (Ollama on
    this machine) — no model backend involved, so it runs on any install
    where the daemon is up and simply reports 0 where it is not."""
    items = ideas.list_items() if items is None else items
    if np is None:
        return {"embedded": 0, "reason": "numpy missing"}
    s = _read()
    todo = []
    for it in items:
        e = s["entries"].get(it["id"]) or {}
        if e.get("vhash") != _hash(_embed_text(it)):
            todo.append(it)
        if len(todo) >= limit:
            break
    if not todo:
        return {"embedded": 0}
    vecs = localmodels.ollama_embed([_embed_text(it) for it in todo])
    if not vecs:
        return {"embedded": 0, "reason": "ollama unreachable"}
    for it, v in zip(todo, vecs):
        e = s["entries"].setdefault(it["id"], {})
        e["vec"] = _pack(v)
        e["vhash"] = _hash(_embed_text(it))
    _write(s)     # never prunes: `items` may be a single idea (see _prune)
    return {"embedded": len(todo)}


def _space(items, s):
    """(ids, unit matrix) over the ideas that have a current vector."""
    if np is None:
        return None
    ids, rows = [], []
    for it in items:
        e = s["entries"].get(it["id"]) or {}
        if e.get("vec") and e.get("vhash") == _hash(_embed_text(it)):
            ids.append(it["id"])
            rows.append(_unpack(e["vec"]))
    if not rows:
        return None
    M = np.stack(rows)
    return ids, M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)


# A RAW COSINE FLOOR DOES NOT WORK ON THIS CORPUS, and the measurement is
# worth keeping: across the real 135-idea backlog, off-diagonal cosine ran
# min 0.38 / median 0.64 / p99 0.78. Every item is "a Vira feature idea",
# so a general-purpose text embedder puts the whole corpus inside a narrow
# band — a fixed floor of 0.5 matches everything and 0.8 matches nothing,
# and either way the number tells the owner nothing about whether two
# ideas are actually the same. So cosine is scored RELATIVE to the corpus
# it lives in: the median pair is 0, a pair as close as the 99th
# percentile is 1. That self-calibrates to any backlog — a corpus of
# genuinely unrelated ideas spreads out and the same floors still mean
# "unusually close for this set".
BASE_PCT, TOP_PCT = 50, 99
_SAMPLE = 600              # rows sampled for the baseline on a big corpus
_base_cache = {}


def _baseline(M):
    """(median, p99) of off-diagonal cosine for this vector space."""
    n = len(M)
    key = (n, float(M[0][0]))
    hit = _base_cache.get(key)
    if hit:
        return hit
    if n < 3:
        return 0.0, 1.0
    A = M if n <= _SAMPLE else M[np.linspace(0, n - 1, _SAMPLE).astype(int)]
    S = A @ A.T
    off = S[~np.eye(len(A), dtype=bool)]
    base = float(np.percentile(off, BASE_PCT))
    top = float(np.percentile(off, TOP_PCT))
    if top - base < 0.02:                     # degenerate space: no spread
        top = base + 0.02
    _base_cache.clear()                       # one space at a time
    _base_cache[key] = (base, top)
    return base, top


def _cos_rel(cos, base, top):
    """Raw cosine -> 0..1 relative to this corpus's own spread."""
    return max(0.0, min(1.0, (cos - base) / (top - base)))


# ---------- similarity ----------

def _tokens(it):
    words = re.findall(r"[a-z0-9]{3,}", (it.get("text") or "").lower())
    return {w for w in words if w not in _STOP}


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def _flat_tags(tagmap):
    return {t for vals in (tagmap or {}).values() for t in vals}


def _reasons(a_tags, b_tags, rel, tok):
    """Why this pair scored — named signals, never a bare number. A
    related-ideas list the owner cannot audit is one they will not trust
    enough to fold anything in from. `rel` is corpus-relative closeness
    (see _cos_rel), not raw cosine."""
    out = []
    for ax in AXES:
        shared = sorted(set(a_tags.get(ax["id"]) or [])
                        & set(b_tags.get(ax["id"]) or []))
        if shared:
            out.append(f"same {ax['label'].lower()}: {', '.join(shared)}")
    if rel >= 0.8:
        out.append("reads as the same subject")
    elif rel >= 0.5 and not out:
        out.append("similar subject")
    if tok >= 0.18 and len(out) < 2:
        out.append("shared wording")
    return out


def score_pairs(target, others, s=None, space=None):
    """[{id, score, reasons, ...}] for one idea against a list, best
    first. `basis` is 'vector' when embeddings carried the comparison and
    'text' when they were unavailable — an honest thinner answer, never a
    silent one.

    `space` is an optional prebuilt (ids, matrix) for callers that score
    many targets against one pool (duplicates): rebuilding it per target
    is O(n^2) blob unpacking for no gain."""
    s = s or _read()
    if space is None:
        space = _space([target] + [o for o in others
                                   if o["id"] != target["id"]], s)
    sims, base, top = {}, 0.0, 1.0
    if space:
        ids, M = space
        idx = {pid: i for i, pid in enumerate(ids)}
        if target["id"] in idx:
            base, top = _baseline(M)
            row = M @ M[idx[target["id"]]]
            sims = {pid: float(row[i]) for pid, i in idx.items()}
    t_tags = tags_for(target, s)
    t_tok = _tokens(target)
    t_flat = _flat_tags(t_tags)
    out = []
    for o in others:
        if o["id"] == target["id"]:
            continue
        o_tags = tags_for(o, s)
        cos = sims.get(o["id"]) if o["id"] in sims else None
        tag_j = _jaccard(t_flat, _flat_tags(o_tags))
        tok_j = _jaccard(t_tok, _tokens(o))
        if cos is None:
            # No vector for this pair: renormalize onto the two signals
            # that exist rather than scoring it as if cosine were zero,
            # which would bury every un-embedded idea below every
            # embedded one.
            rel = 0.0
            score = (W_TAG * tag_j + W_TOK * tok_j) / (W_TAG + W_TOK)
            basis = "text"
        else:
            rel = _cos_rel(cos, base, top)
            score = W_VEC * rel + W_TAG * tag_j + W_TOK * tok_j
            basis = "vector"
        out.append({
            "id": o["id"], "text": o.get("text") or "",
            "project": o.get("project") or "", "status": o.get("status"),
            "created": o.get("created"), "updated": o.get("updated"),
            "tags": o_tags, "score": round(score, 4), "basis": basis,
            "cos": None if cos is None else round(cos, 4),
            "closeness": round(rel, 4),
            "reasons": _reasons(t_tags, o_tags, rel, tok_j),
        })
    out.sort(key=lambda r: -r["score"])
    return out


def related(idea_id, items=None, limit=15, floor=RELATED_FLOOR,
            include_parked=False):
    """Ideas worth considering alongside this one. Done/dropped ideas are
    excluded by default — folding a finished idea into a new task is
    noise — but stay reachable, because "we already built that" is
    sometimes exactly the answer."""
    items = ideas.list_items() if items is None else items
    target = next((i for i in items if i["id"] == idea_id), None)
    if not target:
        raise KeyError(idea_id)
    # Embed the TARGET on demand if it is new or just edited. Without this
    # the one case that matters most — "did I already have this idea?",
    # asked seconds after typing it — is the one case with no vector, and
    # would fall back to word overlap and quietly find nothing.
    e = _read()["entries"].get(idea_id) or {}
    if e.get("vhash") != _hash(_embed_text(target)):
        embed_pending([target])
    pool = items if include_parked else [
        i for i in items if i.get("status") not in ("done", "dropped")]
    rows = [r for r in score_pairs(target, pool) if r["score"] >= floor]
    return {"id": idea_id, "text": target.get("text") or "",
            "related": rows[:limit],
            "basis": rows[0]["basis"] if rows else (
                "vector" if _space(items, _read()) else "text")}


def duplicates(items=None, floor=DUP_FLOOR, limit=60):
    """Near-identical pairs across the live backlog, strongest first —
    "you already had this idea", computed rather than remembered."""
    items = ideas.list_items() if items is None else items
    live = [i for i in items if i.get("status") not in ("done", "dropped")]
    s = _read()
    space = _space(live, s)          # built once, scored against n times
    seen, out = set(), []
    for it in live:
        for r in score_pairs(it, live, s, space=space):
            if r["score"] < floor:
                break
            key = tuple(sorted((it["id"], r["id"])))
            if key in seen:
                continue
            seen.add(key)
            out.append({"a": it["id"], "a_text": it.get("text") or "",
                        "b": r["id"], "b_text": r["text"],
                        "score": r["score"], "reasons": r["reasons"]})
    out.sort(key=lambda r: -r["score"])
    return out[:limit]


# ---------- the tagging pass ----------

TAG_PROMPT = """You are tagging items on a personal engineering backlog so \
they can be searched, grouped, and checked for repetition.

Tag each idea on these axes:
{axes}

Rules:
- REUSE the existing vocabulary below whenever a tag fits. Only mint a new \
tag when nothing existing describes the idea. Two ideas about the same \
subject must come out carrying the same tag, spelled identically.
- Tags are lowercase-kebab, one to three words (search-and-recall, \
mobile-layout, reader). Never a sentence, never a person's name.
- Tag what the idea IS ABOUT, not how it is phrased.
- An axis with nothing to say gets an empty list. Guessing to fill a slot \
makes the vocabulary useless.

Existing vocabulary (tag x how many ideas carry it):
{vocab}

Ideas to tag:
{items}

Return ONLY JSON:
{{"tags": [{{"id": "<the idea id>", {shape}}}]}}"""


def _vocab_block(vocab):
    lines = []
    for ax in AXES:
        pairs = vocab.get(ax["id"]) or []
        shown = ", ".join(f"{t} x{n}" for t, n in pairs[:VOCAB_SHOWN])
        lines.append(f"  {ax['id']}: {shown or '(none yet)'}")
    return "\n".join(lines)


def _tag_prompt(batch, vocab):
    axes = "\n".join(f"- {a['id']}: {a['hint']} (at most {a['max']})"
                     for a in AXES)
    shape = ", ".join(f'"{a["id"]}": []' for a in AXES)
    items = "\n\n".join(
        f"- id: {it['id']}\n  project: {it.get('project') or 'Vira'}\n"
        f"  idea: {(it.get('text') or '')[:900]}" for it in batch)
    return TAG_PROMPT.format(axes=axes, vocab=_vocab_block(vocab),
                             items=items, shape=shape)


def _pending(items, s):
    """Ideas whose text has changed since they were tagged (or never
    were). Content-hashed, so editing an idea re-tags it and nothing
    else does."""
    out = []
    for it in items:
        e = s["entries"].get(it["id"]) or {}
        if e.get("hash") != _hash(it.get("text") or ""):
            out.append(it)
    return out


def tag_pending(items=None, batches=1):
    """Tag up to `batches` batches of untagged/stale ideas. ONE model call
    per batch; a failed batch leaves those ideas pending rather than
    stamping them tagged-with-nothing, so the next pass retries them."""
    items = ideas.list_items() if items is None else items
    s = _read()
    todo = _pending(items, s)
    if not todo:
        return {"tagged": 0, "pending": 0}
    vocab = vocabulary(items, s)
    done, errs = 0, []
    for n in range(batches):
        batch = todo[n * BATCH:(n + 1) * BATCH]
        if not batch:
            break
        try:
            parsed = suggest._extract_json(suggest.complete(
                _tag_prompt(batch, vocab)))
        except Exception as e:  # noqa: BLE001 — a dead backend is a skip
            errs.append(str(e)[:200])
            break
        by_id = {it["id"]: it for it in batch}
        s = _read()                       # re-read: the pass is slow and a
        for row in (parsed.get("tags") or []):   # runner may have written
            it = by_id.get(row.get("id"))
            if not it:
                continue
            e = s["entries"].setdefault(it["id"], {})
            e["tags"] = _clean_tags(row)
            e["hash"] = _hash(it.get("text") or "")
            e["tagged"] = _now()
            done += 1
        _write(s)     # pruning belongs to refresh() alone (see _prune)
        # Later batches see the tags the earlier ones just minted, so the
        # vocabulary converges within a single run instead of only across
        # runs.
        vocab = vocabulary(items, s)
    return {"tagged": done, "pending": max(len(todo) - done, 0),
            "errors": errs}


def refresh(batches=1):
    """One maintenance pass: vectors first (cheap, local, and the thing
    similarity actually needs), then a bounded amount of tagging, then the
    prune. It deliberately takes NO item list — pruning needs the
    authoritative full backlog, and letting a caller scope it is how
    entries get deleted wholesale (see _prune)."""
    items = ideas.list_items()
    emb = embed_pending(items)
    tag = tag_pending(items, batches=batches) if batches else {"tagged": 0}
    s = _read()
    if _prune(s, {it["id"] for it in items}):
        _write(s)
    return {"embedded": emb.get("embedded", 0),
            "embed_reason": emb.get("reason"),
            **{k: v for k, v in tag.items()}}


def status(items=None):
    items = ideas.list_items() if items is None else items
    s = _read()
    space = _space(items, s)
    return {
        "total": len(items),
        "tagged": sum(1 for i in items
                      if any(tags_for(i, s).values())),
        "pending": len(_pending(items, s)),
        "vectors": len(space[0]) if space else 0,
        "axes": [{"id": a["id"], "label": a["label"]} for a in AXES],
    }


# ---------- fold analysis (the dispatch-time question) ----------

FOLD_PROMPT = """The owner is about to dispatch a coding agent on ONE task \
from their backlog. Other backlog items look related. Decide, for each one, \
whether it should be folded into this dispatch or left for its own session.

Fold it in when doing the main task means touching the same code anyway, \
when leaving it out would make the main task's result immediately wrong or \
incomplete, or when it is the same idea said twice.

Leave it separate when it is a genuinely different piece of work that merely \
shares a subject, when it is large enough to deserve its own decision, or \
when folding it in would widen the task past what the owner asked for. \
Scope creep dressed as efficiency is the failure mode here: when in doubt, \
separate.

THE TASK BEING DISPATCHED:
{target}

CANDIDATES:
{cands}

Return ONLY JSON:
{{"verdicts": [{{"id": "<id>", "verdict": "fold" | "separate", \
"why": "<one short sentence>"}}]}}"""


def fold_analysis(idea_id, candidate_ids, items=None):
    """"There are 15 things like this — which belong in this task?" One
    model pass over the candidates the owner is looking at. It returns a
    RECOMMENDATION with reasons: the checkboxes stay the owner's, because
    widening a dispatch is a scope decision, not a retrieval one."""
    items = ideas.list_items() if items is None else items
    target = next((i for i in items if i["id"] == idea_id), None)
    if not target:
        raise KeyError(idea_id)
    by_id = {i["id"]: i for i in items}
    cands = [by_id[c] for c in candidate_ids if c in by_id and c != idea_id]
    if not cands:
        return {"verdicts": [], "analyzed": 0}
    s = _read()

    def block(it):
        tags = ", ".join(sorted(_flat_tags(tags_for(it, s)))) or "untagged"
        return (f"- id: {it['id']}\n  status: {it.get('status')}\n"
                f"  tags: {tags}\n  idea: {(it.get('text') or '')[:700]}")

    prompt = FOLD_PROMPT.format(
        target=f"{(target.get('text') or '')[:1500]}",
        cands="\n\n".join(block(c) for c in cands)[:24_000])
    parsed = suggest._extract_json(suggest.complete(prompt))
    ok = {c["id"] for c in cands}
    out = []
    for row in (parsed.get("verdicts") or []):
        cid = row.get("id")
        if cid not in ok:
            continue          # a verdict on an idea we did not ask about
        ok.discard(cid)       # is dropped, never invented forward
        out.append({"id": cid,
                    "verdict": "fold" if row.get("verdict") == "fold"
                               else "separate",
                    "why": str(row.get("why") or "")[:300]})
    # Anything the model skipped stays separate by default — silence is
    # never taken as permission to widen the task.
    for cid in ok:
        out.append({"id": cid, "verdict": "separate",
                    "why": "not assessed — left for its own session"})
    return {"verdicts": out, "analyzed": len(cands)}


# ---------- background pass ----------

class Indexer(threading.Thread):
    """Keeps the derived layer close behind the store. Vectors every tick
    (local, seconds); tagging one batch per tick, because it is a model
    call and a backlog that tags itself over an hour is fine — nothing
    waits on it. Started outside VIRA_PASSIVE only, like every other
    worker; a test clone tags on demand through the route instead."""

    def __init__(self, interval_min=None):
        super().__init__(daemon=True)
        self.interval = max(60.0, float(interval_min or 10) * 60.0)
        self.last = None

    def run(self):
        while True:
            try:
                # refresh() is a no-op when nothing is stale, and costs no
                # model call in that case — so the tick can be unconditional.
                self.last = refresh(batches=1)
            except Exception as e:  # noqa: BLE001 — never kill the thread
                self.last = {"error": str(e)[:200]}
            time.sleep(self.interval)


# ---------- CLI ----------

if __name__ == "__main__":          # pragma: no cover
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "refresh":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        print(json.dumps(refresh(batches=n), indent=1))
    elif cmd == "dupes":
        for d in duplicates():
            print(f"{d['score']:.3f}  {d['a_text'][:70]}\n"
                  f"       {d['b_text'][:70]}\n")
    else:
        print(json.dumps(status(), indent=1))
