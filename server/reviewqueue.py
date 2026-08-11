"""Needs review — one queue for every decision that is waiting on the owner.

THE FAILURE THIS ENDS. The corrections ledger's proposal pipeline ran
nightly from 2026-07-25 and staged 113 proposals; NONE were ever approved,
because nothing ever put them in front of the owner. lessons.py is explicit
that "promotion is an explicit act" — it just never said where that act
happens. A pending decision nobody can see is not pending, it is rotting.
The same shape repeats across the machine: self-record inbox stubs whose
documented steady state is empty, open adjudication flags in the canon, and
Vira's own `proposed` ideas.

So this is an AGGREGATOR, not a lessons panel. Each contributing store is a
SOURCE registered here with one reader and (optionally) one actor; the queue
knows nothing about any store's file format beyond what its reader returns.
Adding a fifth source is adding a `register(Source(...))` call.

FOUR RULES THIS MODULE IS BENT AROUND:

1. **A source never breaks the queue.** Every reader runs inside
   `_safe_read`: a raised exception becomes an entry in `errors` and an
   empty list, exactly the way brief.py degrades a section rather than
   502-ing the whole brief. A missing file is not even an error — it is a
   store that has nothing to say today.

2. **VIRA NEVER WRITES THE LEDGER, AND NEVER REPAIRS ITS INPUTS.** Approving
   a lesson shells out to `lessons.py` (list-form argv, never a shell); that
   script owns LESSONS.md, its dedupe, its cap, and the decided-jsonl side
   store. The proposals file belongs to daily-provenance.py and is never
   rewritten from here — including to fix the duplicate ids described in
   `_lessons_act`, which are being fixed at the generator instead.

3. **Reading is not deciding.** Sources whose items are prose in the
   owner's own canon (the self-record inbox, the open adjudication flags)
   are surfaced READ-ONLY: `actions: []`. Absorbing an inbox note into
   canon is an adjudication act that happens in the record with the whole
   document in view, not a button in a brief. Vira's job here is to make
   the item visible, which is the entire thing that was missing.

4. **Every path is configuration.** Nothing here hardcodes a home
   directory; the roots come from settings overrides (the
   `applications.connections_csv` idiom) and otherwise derive from
   `Path.home()`. Tests repoint every root at one tmp fixture, and
   `roots()` declares the whole set in one place so a root added later
   cannot silently read the real machine (the readinglist lesson).
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from . import jsonstore, settings

ROOT = Path(__file__).resolve().parent.parent
# Vira's OWN record of decisions it could not hand upstream — today only the
# duplicated-id lessons rows (see _lessons_act). Small, append-shaped, and
# never a substitute for the upstream store: a row is filtered by this file
# only when the upstream tool cannot be given the decision safely.
DECISIONS = ROOT / "data" / "review-decided.json"

# The picker shows everything; the brief shows the head of the list.
BRIEF_TOP = 5
ACT_TIMEOUT_S = 60
WHY_MAX = 400
TITLE_MAX = 200

# A store id that reaches a subprocess argv. Deliberately far narrower than
# "anything without a shell metacharacter": these ids are minted by the
# proposal pipeline and look like L20260725-vira-1.
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# A markdown bullet list under one heading, up to the next heading of any
# level. Used for both canon sections — they are the same shape at different
# heading depths.
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*$")


# ------------------------------------------------------------------ roots

def _path(key, fallback):
    """A settings override, or the neutral default. The
    applications.connections_csv idiom, applied to every root here."""
    raw = str(settings.raw().get(key) or "").strip()
    return Path(raw).expanduser() if raw else Path(fallback).expanduser()


def lessons_script():
    """The corrections-ledger CLI. The queue calls it; it never reimplements
    it (rule 2)."""
    return _path("lessons_script_path",
                 Path.home() / ".claude" / "scripts" / "lessons.py")


def lessons_state_dir():
    """Shared with lessonwatch.py — one config key for one directory, so the
    counter and the queue can never read different files."""
    return _path("lessons_state_dir", Path.home() / ".claude" / "sessions")


def lessons_proposed():
    return lessons_state_dir() / "lessons-proposed.jsonl"


def lessons_decided():
    """What the ledger CLI has already ruled on. It appends here and never
    rewrites the proposed file, so a row keeps `status: proposed` forever —
    a queue that filtered on that field alone would re-show every approval."""
    return lessons_state_dir() / "lessons-decided.jsonl"


def self_record():
    from . import applications
    return applications.self_record()


def inbox_notes_dir():
    return _path("review_inbox_dir", self_record() / "inbox" / "notes")


def history_path():
    return _path("review_history_path",
                 self_record() / "canon" / "MASTER_HISTORY.md")


def roots():
    """Every filesystem root this module reads. Tests iterate this to assert
    the whole set points into the fixture."""
    return {
        "lessons_script": lessons_script(),
        "lessons_proposed": lessons_proposed(),
        "lessons_decided": lessons_decided(),
        "local_decisions": DECISIONS,
        "inbox_notes": inbox_notes_dir(),
        "history": history_path(),
    }


# ------------------------------------------------------------------ shapes

def _clip(text, limit):
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t[:limit]


def _age_days(stamp):
    """Whole days since an ISO date, or None when the item carries no date.
    None is honest: a canon bullet has no birthday on file, and inventing
    one would put a fake number in the brief."""
    if not stamp:
        return None
    try:
        d = date.fromisoformat(str(stamp)[:10])
    except ValueError:
        return None
    return max(0, (date.today() - d).days)


def _decisions():
    return jsonstore.read(DECISIONS, {})


def _record_decision(source, key, action, mode):
    def _m(s):
        s.setdefault(source, {})[key] = {
            "action": action, "mode": mode,
            "at": datetime.now().isoformat(timespec="seconds")}
    jsonstore.mutate(DECISIONS, _m, {}, indent=1)


def item(source, raw_id, title, why="", stamp="", actions=(), ref="",
         note=""):
    """The one shape every source contributes. `id` is namespaced by source
    so one flat list can be acted on without a second lookup key."""
    return {
        "id": f"{source}:{raw_id}",
        "raw_id": str(raw_id),
        "source": source,
        "source_label": SOURCES[source].label if source in SOURCES else source,
        "title": _clip(title, TITLE_MAX),
        "why": _clip(why, WHY_MAX),
        "date": str(stamp or "")[:10],
        "age_days": _age_days(stamp),
        "actions": list(actions),
        "ref": str(ref or ""),
        # A caveat the owner must see BEFORE acting on the row (today: a
        # proposal whose id is shared, which changes what approve does).
        "note": _clip(note, WHY_MAX),
    }


class Source:
    """One contributing store.

    `read()` returns a list of items. `act(raw_id, action)` performs one of
    `actions` and returns a small result dict; a source with no actor is
    read-only and its items carry `actions: []` (rule 3).
    """

    def __init__(self, key, label, hint, reader, actor=None, actions=()):
        self.key = key
        self.label = label
        self.hint = hint
        self.reader = reader
        self.actor = actor
        self.actions = tuple(actions)

    def read(self):
        return list(self.reader())

    def act(self, raw_id, action):
        if not self.actor:
            raise ValueError(f"{self.key} is read-only here")
        if action not in self.actions:
            raise ValueError(f"unknown action {action!r} for {self.key}")
        return self.actor(raw_id, action)


SOURCES = {}


def register(source):
    SOURCES[source.key] = source
    return source


# ------------------------------------------------------ source: lessons

def _read_jsonl(path):
    """Rows from a jsonl file. A missing file is an empty store, never an
    exception; one malformed line never costs the rest."""
    out = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _text_key(text):
    """A proposal's identity HERE. Deliberately not the row's own `id`:
    the upstream generator mints ids per (day, project, counter) and repeats
    them — on this machine 113 proposal rows carry 44 distinct ids, nine of
    them sharing one id (verified 2026-08-11). Text is what is actually
    unique (113/113), and it survives the file being appended to, which a
    row index would not."""
    norm = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def _lessons_rows():
    """Every undecided proposal, one entry per ROW, each carrying whether its
    id is ambiguous.

    Two filters, because the CLI keeps its decisions in a side file and never
    rewrites the proposed jsonl: an id in lessons-decided.jsonl is ruled on
    upstream, and a text key in Vira's own decision store was ruled on here
    (see `_lessons_act`)."""
    decided_ids = {str(r.get("id") or "")
                   for r in _read_jsonl(lessons_decided())}
    local = set(_decisions().get("lessons") or {})
    rows = [r for r in _read_jsonl(lessons_proposed())
            if str(r.get("status") or "") == "proposed"
            and str(r.get("id") or "")
            and str(r.get("id")) not in decided_ids
            and _text_key(r.get("text")) not in local]
    counts = {}
    for r in rows:
        counts[str(r["id"])] = counts.get(str(r["id"]), 0) + 1
    for r in rows:
        r["_key"] = _text_key(r.get("text"))
        r["_ambiguous"] = counts[str(r["id"])] > 1
    return rows


# Why an ambiguous row cannot take the id path, in the words the UI shows.
AMBIGUOUS_WHY = ("shared proposal id — approve promotes this exact text; "
                 "drop is recorded here only")


def _lessons_read():
    rows = []
    for r in _lessons_rows():
        tier = r.get("tier")
        tag = f"tier {tier}" if tier in (1, 2, 3) else ""
        if r["_ambiguous"]:
            tag = (tag + " · dup id").strip(" ·")
        rows.append(item(
            "lessons", r["_key"],
            r.get("text") or "",
            why=r.get("why") or "",
            stamp=r.get("day") or (r.get("at") or "")[:10],
            actions=("approve", "drop"),
            ref=tag,
            note=AMBIGUOUS_WHY if r["_ambiguous"] else "",
        ))
    rows.sort(key=lambda x: x["date"])
    return rows


def _run_lessons(args):
    """The one place this module launches the ledger CLI. List-form argv, no
    shell, fixed subcommand, arguments validated by the caller."""
    script = lessons_script()
    if not script.is_file():
        raise FileNotFoundError(f"no lessons CLI at {script}")
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=ACT_TIMEOUT_S, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise RuntimeError(f"lessons.py {args[0]} failed: {detail}")
    return (proc.stdout or "").strip()[:300]


def _lessons_act(raw_id, action):
    """Rule on one proposal — always through lessons.py, never by writing
    LESSONS.md or the jsonl (rule 2), and never by rewriting the proposed
    file to repair its ids (that file belongs to daily-provenance.py, and a
    repair here would collide with the fix being made there).

    Two modes, and the row says which one it is in:

    - **unique id** — `lessons.py approve <id>` / `drop <id>`. Exact: the id
      resolves to the one row the owner is looking at.
    - **duplicated id** — the id path is UNSAFE. `pending()` keeps only the
      first row per id, so `approve <id>` would promote that row's text and
      then mark the id decided, burying every sibling. Approve therefore
      goes through `lessons.py add "<text>" --tier N`, which takes the exact
      text on screen and needs no id lookup; drop is recorded locally only,
      because `drop <id>` would bury the siblings just as badly. Either way
      the decision is banked in Vira's own store so the row leaves the queue.
    """
    rows = {r["_key"]: r for r in _lessons_rows()}
    row = rows.get(str(raw_id))
    if not row:
        raise KeyError(f"no pending proposal {raw_id}")
    lesson_id = str(row.get("id") or "")
    if not SAFE_ID_RE.match(lesson_id):
        raise ValueError(f"unsafe proposal id: {lesson_id!r}")
    out = {"ok": True, "action": action, "id": raw_id,
           "lesson_id": lesson_id, "ambiguous": bool(row["_ambiguous"])}
    if not row["_ambiguous"]:
        out["mode"] = "id"
        out["output"] = _run_lessons([action, lesson_id])
        return out
    out["mode"] = "text" if action == "approve" else "local"
    if action == "approve":
        tier = row.get("tier")
        tier = tier if tier in (1, 2, 3) else 2
        # The tier is passed through rather than clamped: this path must
        # promote exactly what the id path would have promoted, and quietly
        # rewriting a tier here would make the two modes disagree about what
        # approving a row means.
        out["output"] = _run_lessons(
            ["add", str(row.get("text") or ""), "--tier", str(tier)])
    else:
        out["output"] = ("dropped in Vira only — the upstream proposal keeps "
                         "its shared id and its siblings stay pending")
    _record_decision("lessons", str(raw_id), action, out["mode"])
    return out


register(Source(
    "lessons", "Lesson proposals",
    "nightly proposals for the corrections ledger — promotion is your act",
    _lessons_read, _lessons_act, ("approve", "drop")))


# -------------------------------------------------- source: inbox stubs

def _first_line(path):
    """The note's own first line of prose, for the row's subtitle. Read
    lazily and clipped — the queue points at the note, it does not render
    it."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _ in range(20):
                line = f.readline()
                if not line:
                    break
                s = line.strip()
                # headings and front-matter rules are the note's furniture;
                # the row wants the first line the owner actually wrote
                if s and not s.startswith("#") and not s.startswith("---"):
                    return s
    except OSError:
        pass
    return ""


def _inbox_read():
    """Dated capture stubs awaiting absorption into the self-record's canon.
    The documented steady state of that folder is EMPTY, so anything sitting
    in it is by definition waiting on the owner. Read-only here (rule 3)."""
    d = inbox_notes_dir()
    if not d.is_dir():
        return []
    rows = []
    for f in sorted(d.iterdir()):
        if not f.is_file() or f.name.startswith("."):
            continue
        if f.suffix.lower() not in (".md", ".txt"):
            continue
        if f.name.upper().startswith("README"):
            continue
        m = re.match(r"(\d{4}-\d{2}-\d{2})", f.name)
        stamp = m.group(1) if m else ""
        if not stamp:
            try:
                stamp = datetime.fromtimestamp(
                    f.stat().st_mtime).date().isoformat()
            except OSError:
                stamp = ""
        rows.append(item("inbox", f.name, f.stem.replace("_", " "),
                         why=_first_line(f), stamp=stamp, ref=str(f)))
    rows.sort(key=lambda x: x["date"])
    return rows


register(Source(
    "inbox", "Self-record inbox",
    "captured notes waiting to be absorbed into canon (steady state: empty)",
    _inbox_read))


# ---------------------------------------------- source: adjudication flags

def _section_bullets(text, heading_needle):
    """Top-level bullets under the first heading containing `heading_needle`,
    up to the next heading of ANY level. Continuation lines fold onto their
    bullet, so a wrapped flag is one item rather than three."""
    lines = (text or "").splitlines()
    out, inside = [], False
    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            if inside:
                break
            inside = heading_needle.lower() in m.group(1).lower()
            continue
        if not inside:
            continue
        stripped = line.strip()
        if stripped.startswith("- ") or stripped.startswith("* "):
            out.append(stripped[2:].strip())
        elif stripped and out:
            out[-1] += " " + stripped
    return [b for b in out if b]


def _flag_title(bullet):
    """A canon flag opens with a bolded subject — that is the title, and the
    rest is the why. Without one, the first sentence stands in."""
    m = re.match(r"\*\*(.+?)\*\*[.:]?\s*(.*)$", bullet, re.S)
    if m:
        # the canon writes the sentence period INSIDE the bold; a title is
        # not a sentence, so it comes off
        return m.group(1).strip().rstrip(".:; "), m.group(2).strip()
    head, _, rest = bullet.partition(". ")
    return head.strip(), rest.strip()


def _flag_id(source_name, bullet):
    """Stable across edits to surrounding text, unstable across edits to the
    flag itself — which is correct: a rewritten flag is a new thing to read."""
    norm = re.sub(r"[^a-z0-9]+", " ", bullet.lower()).strip()
    return source_name + "-" + hashlib.sha1(
        norm.encode("utf-8")).hexdigest()[:10]


def _resolved(bullet):
    """A flag the record has already ruled on. The canon keeps resolved
    entries in place as a correction record, so the queue filters on the
    record's own uppercase RESOLVED marker rather than deleting anything."""
    return "RESOLVED" in bullet


def _read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


FLAG_SECTIONS = (
    # (file getter, heading needle, short label for the row)
    # Master History is the single canonical record as of 2026-08-11; the
    # former FACTS.md ledger and its "Open reconciliation flags" section were
    # folded into it, so open flags now have one home.
    (history_path, "Open questions and contradictions", "MASTER_HISTORY.md"),
)


def _flags_read():
    """Open adjudication flags the self-record is holding for the owner.
    Read-only: ruling on one of these is a canon edit."""
    rows = []
    for getter, needle, label in FLAG_SECTIONS:
        path = getter()
        for bullet in _section_bullets(_read_text(path), needle):
            if _resolved(bullet):
                continue
            title, why = _flag_title(bullet)
            rows.append(item("flags", _flag_id(label, bullet), title,
                             why=why, ref=f"{label}: {needle}"))
    return rows


register(Source(
    "flags", "Open adjudication flags",
    "questions the self-record is holding open for your ruling",
    _flags_read))


# ------------------------------------------------- source: proposed ideas

def _ideas_read():
    """Vira's own staged proposals — the muse routine, the lesson-recurrence
    counter, propose_idea. `proposed` means nothing runs until the owner
    says so (server/ideas.py), which is precisely a pending owner decision."""
    from . import ideas
    rows = []
    for it in ideas.list_items():
        if it.get("status") != "proposed":
            continue
        rows.append(item("ideas", it["id"], it.get("text") or "",
                         why=it.get("note") or "",
                         stamp=(it.get("created") or "")[:10],
                         actions=("approve", "drop"),
                         ref=it.get("project") or ""))
    rows.sort(key=lambda x: x["date"])
    return rows


def _ideas_act(raw_id, action):
    """The same two writes /api/ideas/{id}/approve and /decline perform.
    Approve here is deliberately the plain one (proposed -> open) with no
    build dispatch: a one-tap approval from a phone must not launch an
    agent session — that choice lives in the Queue, where the build option
    is in front of the owner."""
    from . import ideas
    if action == "approve":
        return {"ok": True, "action": action, "id": raw_id,
                "idea": ideas.update(raw_id, status="open")}
    return {"ok": True, "action": action, "id": raw_id,
            "idea": ideas.stamp_note(
                raw_id,
                f"declined by the owner {date.today().isoformat()}",
                status="dropped")}


register(Source(
    "ideas", "Proposed work",
    "ideas Vira staged for you — nothing proposed ever runs unapproved",
    _ideas_read, _ideas_act, ("approve", "drop")))


# ------------------------------------------------------------------ reads

def _safe_read(source):
    """A source that raises costs its own rows and nothing else — the brief's
    never-break-on-a-section contract, applied per source."""
    try:
        return source.read(), ""
    except Exception as e:  # noqa: BLE001 — a broken store is not an outage
        return [], f"{type(e).__name__}: {str(e)[:160]}"


def items():
    """The whole queue: every source's items in one flat list, with per-source
    counts and any read errors named rather than swallowed."""
    rows, counts, errors = [], {}, {}
    for key, source in SOURCES.items():
        got, err = _safe_read(source)
        counts[key] = len(got)
        if err:
            errors[key] = err
        rows.extend(got)
    # Oldest first, undated last: this queue exists because things rot in it,
    # so the thing that has waited longest is the thing to look at.
    rows.sort(key=lambda r: (r["age_days"] is None, -(r["age_days"] or 0)))
    return {
        "items": rows,
        "total": len(rows),
        "counts": counts,
        "errors": errors,
        "sources": [{"key": s.key, "label": s.label, "hint": s.hint,
                     "actions": list(s.actions), "count": counts.get(s.key, 0)}
                    for s in SOURCES.values()],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def summary(top=BRIEF_TOP):
    """The brief's payload. None when the queue is empty — a section that
    says "nothing waiting" every morning teaches the owner to skip it."""
    q = items()
    if not q["total"]:
        return None
    return {"total": q["total"], "counts": q["counts"],
            "errors": q["errors"],
            "top": q["items"][:top],
            "sources": [s for s in q["sources"] if s["count"]]}


def act(item_id, action):
    """Perform one action on one item, addressed by the namespaced id the
    reads hand out. KeyError = unknown source; ValueError = unknown or
    unsupported action; the source's own errors pass through."""
    key, _, raw_id = str(item_id or "").partition(":")
    source = SOURCES.get(key)
    if not source or not raw_id:
        raise KeyError(f"unknown review item: {item_id}")
    return source.act(raw_id, action)
