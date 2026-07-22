"""Module front doors — the path from a dormant module to a live one.

A module whose config is absent is DORMANT: the code works, it simply has
nothing to show. That is a good failure mode and a dead end. The Reader
took it furthest — with no pages on disk it deleted its own dock icon and
skipped itself in the Launchpad grid, so the one surface where a stranger
could have discovered the module was the one surface that refused to show
it. Applications did the softer version: always visible, always empty,
with no hint that a config key stood between the two states.

A front door replaces the dead end. Every setup-able module declares:

  - `blurb`  — what it is, in one line a stranger understands
  - `what`   — the longer answer behind "What is this?"
  - `demo`   — a short anonymized clip, hosted publicly (see DEMO_BASE)
  - `ask`    — the interview that collects what setup needs
  - `probe`  — whether the module is live yet, derived from the world

`state()` reports every module's readiness the way onboard.steps() does:
DERIVED, never stored. A half-finished setup recomputes exactly where it
stopped, so re-entry is free and no progress file can go stale against
reality.

The write boundary, stated once because it is the security-relevant part:

  - CONFIG goes through a server-side validator. The interview's answers
    compose a prompt; the session proposes a config payload through a
    native tool (server/viratools.py); this module schema-checks it and
    applies it. A bad config breaks the app for every module, so the
    agent never writes one by its own hands — the update_module_map
    discipline, applied to setup.
  - CONTENT is authored normally, inside the directory the owner named
    during the interview (a self-record, a reading room). That is the
    agent doing the job it was dispatched to do, in a place the owner
    chose, and validating prose through a schema would buy nothing.

Store: data/frontdoor.json — which setups have been dispatched, and the
job id of each, so a front door can show a run in flight and link to it.
"""
import base64
import binascii
import json
import re
import threading
import time
from pathlib import Path

from . import jsonstore, reading, settings
from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "frontdoor.json"

# The demo clips live on a PUBLIC path of the owner's site, outside the
# Access-gated /lab/* tree — a stranger's install has no credential, so a
# gated URL would render an empty player for everyone but the owner. The
# clips are anonymized before they ship (walkthrough_anon + the scanner
# gate); the player degrades to the text explainer when the fetch fails,
# so an offline install or a domain outage costs the caption, not the
# front door.
DEMO_BASE = "https://thedurham.nyc/demo"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_UPLOAD = 8 * 1024 * 1024      # a resume; the cap is a sanity bound

_lock = threading.Lock()


# ---------------------------------------------------------------- probes
# Each probe answers one question: is this module live? Derived from the
# world (files on disk, keys in config), never from a stored flag.

def _reader_state():
    pages = reading.list_pages()
    return {
        "ready": bool(pages),
        "detail": (f"{len(pages)} reading room{'s' if len(pages) != 1 else ''}"
                   if pages else "No reading rooms yet."),
        "count": len(pages),
    }


def _applications_state():
    cfg = settings.raw()
    wired = bool(cfg.get("applications_sources") or cfg.get("lab_root"))
    self_rec = cfg.get("self_record")
    facts = None
    if self_rec:
        facts = Path(str(self_rec)).expanduser() / "FACTS.md"
    has_facts = bool(facts and facts.exists())
    if wired and has_facts:
        detail = "Role sources wired, self-record ready."
    elif wired:
        detail = "Sources wired — no self-record yet, so Apply cannot build."
    elif has_facts:
        detail = "Self-record ready — no role sources wired yet."
    else:
        detail = "Not set up yet."
    return {"ready": wired and has_facts, "detail": detail,
            "sources": wired, "self_record": has_facts}


# ------------------------------------------------------------- registry

MODULES = [
    {
        "id": "reader",
        "title": "Reader",
        "blurb": "A queue of things worth reading, watching, and listening "
                 "to on one subject — built for you, and tracked as you go.",
        "what": "A reading room is a researched consumption queue on a "
                "subject you care about: every worthwhile talk, paper, "
                "post, and episode, deduplicated, dated, and sorted by "
                "how much it actually matters. You pick a mode — watch, "
                "listen, or read — and work down it. What you finish is "
                "marked off server-side, so the room looks the same on "
                "your phone as it does on your desk.\n\n"
                "Vira interviews you about the subject, then researches "
                "it and builds the room. You can have as many as you "
                "like; each one is its own queue.",
        "demo": f"{DEMO_BASE}/reader.mp4",
        "cta": "Build a reading room",
        "probe": _reader_state,
        "ask": [
            {"id": "subject", "kind": "text", "required": True,
             "label": "What subject should this room cover?",
             "help": "Be specific. A person, a company, a field, a "
                     "question you are trying to answer.",
             "placeholder": "e.g. AI interpretability research"},
            {"id": "why", "kind": "textarea", "required": True,
             "label": "Why are you building it?",
             "help": "What you want to walk away understanding. This "
                     "drives what gets ranked P1 versus skipped.",
             "placeholder": "e.g. I want to understand the safety case "
                            "well enough to argue it in an interview."},
            {"id": "modes", "kind": "multi", "required": True,
             "label": "What do you want in it?",
             "options": [
                 {"value": "read", "label": "Reading — posts, papers, docs"},
                 {"value": "listen", "label": "Listening — podcasts, talks"},
                 {"value": "watch", "label": "Watching — video, lectures"},
             ],
             "default": ["read", "listen", "watch"]},
        {"id": "people", "kind": "text", "required": False,
             "label": "Anyone in particular?",
             "help": "Names to prioritize. Optional — leave blank and "
                     "Vira works out who matters.",
             "placeholder": "e.g. Chris Olah, Jared Kaplan"},
            {"id": "depth", "kind": "choice", "required": True,
             "label": "How deep should it go?",
             "options": [
                 {"value": "core", "label": "The core",
                  "hint": "~40 items. The things you would be embarrassed "
                          "not to know."},
                 {"value": "thorough", "label": "Thorough",
                  "hint": "~120 items. Core plus the second tier and the "
                          "useful primary sources."},
                 {"value": "exhaustive", "label": "Exhaustive",
                  "hint": "300+. Everything findable, ranked. Takes "
                          "longer and costs more."},
             ],
             "default": "thorough"},
        ],
    },
    {
        "id": "applications",
        "title": "Applications",
        "blurb": "Every open role worth your time, scored against your "
                 "actual record — and one click that drafts the package.",
        "what": "Applications polls job boards, scores what it finds "
                "against your real experience, and keeps the ones worth "
                "your time in one list you can star, comment on, and "
                "track. Hitting Apply dispatches an agent that builds "
                "the whole package — a tailored CV, a cover letter, the "
                "form answers, and an interview-prep dossier — drafted "
                "from a record of what you have actually done.\n\n"
                "That record is the part setup builds. You upload a "
                "resume, Vira reads it and interviews you to fill the "
                "gaps, and the result becomes the source every future "
                "application draws its claims from. Nothing is ever "
                "submitted for you.",
        "demo": f"{DEMO_BASE}/applications.mp4",
        "cta": "Set up applications",
        "probe": _applications_state,
        "ask": [
            {"id": "resume", "kind": "file", "required": True,
             "label": "Upload your resume",
             "help": "PDF, Word, or plain text. Vira reads it to build "
                     "your record — it is the starting point, not the "
                     "final word.",
             "accept": ".pdf,.doc,.docx,.txt,.md,.rtf"},
            {"id": "target", "kind": "textarea", "required": True,
             "label": "What are you looking for?",
             "help": "Roles, seniority, the kind of work. Plain English "
                     "is fine.",
             "placeholder": "e.g. Forward-deployed / solutions architect "
                            "roles at AI labs, IC or lead, NYC or remote."},
            {"id": "companies", "kind": "text", "required": False,
             "label": "Any companies in particular?",
             "help": "Comma-separated. Optional — leave blank and Vira "
                     "watches the whole board set.",
             "placeholder": "e.g. Anthropic, OpenAI"},
            {"id": "location", "kind": "text", "required": False,
             "label": "Where?",
             "placeholder": "e.g. New York City, or remote"},
            {"id": "comp", "kind": "text", "required": False,
             "label": "Compensation floor?",
             "help": "Optional. Used to sort, never to hide anything "
                     "from you.",
             "placeholder": "e.g. $200k base"},
            {"id": "record_dir", "kind": "text", "required": True,
             "label": "Where should your record live?",
             "help": "A folder Vira creates and owns. Your resume, your "
                     "facts, and every application package land here.",
             "default": "~/.vira/self-record"},
        ],
    },
]

BY_ID = {m["id"]: m for m in MODULES}


def _public(mod, st):
    """The registry entry as the UI sees it — no probe callable, and the
    derived readiness merged in."""
    out = {k: v for k, v in mod.items() if k != "probe"}
    out.update(st)
    return out


# ------------------------------------------------------------- the store

def _load():
    try:
        s = json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        s = {}
    if not isinstance(s, dict):
        s = {}
    s.setdefault("runs", {})       # module id -> {job_id, started, answers}
    s.setdefault("dismissed", [])  # module ids whose front door was skipped
    return s


def _save(s):
    jsonstore.write_atomic(STORE, s, indent=1)


def get_run(module_id):
    return _load()["runs"].get(module_id)


def record_run(module_id, job_id, answers):
    with locked(STORE):
        s = _load()
        s["runs"][module_id] = {
            "job_id": job_id,
            "started": int(time.time()),
            # The resume is a path, never its bytes; answers are echoed
            # back to the front door so a run in flight can show what it
            # was told without re-asking.
            "answers": {k: v for k, v in (answers or {}).items()},
        }
        _save(s)
        return s["runs"][module_id]


def dismiss(module_id, undo=False):
    """Hide a front door's prompt without setting the module up. The tile
    stays in the Launchpad — dismissing silences the nudge, it never
    removes the door."""
    with locked(STORE):
        s = _load()
        d = set(s["dismissed"])
        d.discard(module_id) if undo else d.add(module_id)
        s["dismissed"] = sorted(d)
        _save(s)
        return s["dismissed"]


# -------------------------------------------------------------- the state

def state():
    """Every setup-able module, its readiness, and any run in flight.

    The Launchpad calls this on every render: a dormant module draws in
    the unconfigured state instead of vanishing, and a module whose setup
    is mid-flight shows that rather than looking untouched."""
    s = _load()
    out = []
    for mod in MODULES:
        st = mod["probe"]()
        rec = _public(mod, st)
        rec["dismissed"] = mod["id"] in s["dismissed"]
        run = s["runs"].get(mod["id"])
        if run and not st["ready"]:
            rec["run"] = run          # in flight; a ready module drops it
        out.append(rec)
    return {"modules": out}


def ready_ids():
    """Ids of the modules that are live — the frontend's dormancy test."""
    return [m["id"] for m in MODULES if m["probe"]()["ready"]]


# ------------------------------------------------------- interview -> prompt

def slugify(text, fallback="room"):
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:48].strip("-")
    return s or fallback


DEPTH_TARGET = {
    "core": ("about 40", "the things the owner would be embarrassed not to "
             "know. Ruthless — no completionism."),
    "thorough": ("about 120", "the core, plus the strong second tier and the "
                 "primary sources worth reading firsthand."),
    "exhaustive": ("300 or more", "everything findable that is genuinely "
                   "worth someone's time, ranked honestly so the tail is "
                   "visibly the tail."),
}


def _reader_prompt(answers):
    subject = _s(answers.get("subject"))
    slug = slugify(subject, "reading-room")
    # A rebuild of the same subject is a repass, not a collision: the page
    # is replaced and item ids stay stable, so done-marks carry over.
    title = f"{subject} reading room" if len(subject) < 60 else subject
    modes = answers.get("modes") or ["read", "listen", "watch"]
    if isinstance(modes, str):
        modes = [m.strip() for m in modes.split(",") if m.strip()]
    depth = _s(answers.get("depth")) or "thorough"
    count, depth_note = DEPTH_TARGET.get(depth, DEPTH_TARGET["thorough"])
    people = _s(answers.get("people"))
    why = _s(answers.get("why"))

    lines = [
        f"Build a reading room in Vira on: {subject}",
        "",
        "WHAT THE OWNER WANTS OUT OF IT (this is the ranking rule — read "
        f"it twice):\n{why}",
        "",
        f"INCLUDE: {', '.join(modes)}.",
        f"TARGET SIZE: {count} items — {depth_note}",
    ]
    if people:
        lines.append(f"PRIORITIZE THESE PEOPLE: {people}")
    lines += [
        "",
        "METHOD",
        "1. Research properly. Use web search, and use your native Vira "
        "tools — vault_search for anything the owner has already written "
        "or saved on this, media_search for what has come through their "
        "messages. Something already in their vault is worth marking as "
        "such, not re-recommending as new.",
        "2. Go past the first page of results. Primary sources, the "
        "original talk rather than the writeup, the paper rather than the "
        "thread about the paper.",
        "3. Rank against what the owner said they want to understand. "
        "P1 = load-bearing, they would be embarrassed to miss it. "
        "P2 = strongly worth the time. P3 = for completeness.",
        "4. Set `status` honestly: HAVE if it is already in their vault "
        "(put the note path in `vault`), PARTIAL if they have only met it "
        "secondhand, MISSING otherwise. When you have no vault signal at "
        "all, MISSING is the honest answer.",
        "5. Write one-sentence `note` (what it is) and `why` (why it sits "
        "at that priority). No filler — these are what the owner reads "
        "when deciding what to open next.",
        "",
        "THEN WRITE IT",
        "Call the mcp__vira__create_reading_room tool with the whole item "
        "list in one call. That tool renders and writes the page itself — "
        "do NOT write HTML, and do not create files under static/reading "
        "by hand. If it returns an error, fix the payload and call it "
        "again.",
        "",
        f"slug: {slug}",
        f"title: {title}",
        "subtitle: one line saying what the room covers and how it is "
        "ranked.",
        "",
        "Item shape (a JSON array for the items_json argument):",
        '{"title": str, "url": str, "date": "YYYY-MM-DD", "type": '
        '"podcast|paper|post|talk|video|book", "mode": "watch|listen|read", '
        '"prio": "P1|P2|P3", "people": [str], "venue": str, "note": str, '
        '"why": str, "status": "MISSING|PARTIAL|HAVE", "vault": str, '
        '"pay": bool}',
        "",
        "When the tool reports success you are done — say what you built "
        "and what the owner should open first.",
    ]
    return "\n".join(lines), {"slug": slug, "title": title}


def _applications_prompt(answers):
    record = _s(answers.get("record_dir")) or "~/.vira/self-record"
    resume = _s(answers.get("resume"))
    target = _s(answers.get("target"))
    companies = _s(answers.get("companies"))
    location = _s(answers.get("location"))
    comp = _s(answers.get("comp"))

    lines = [
        "Set up the Applications module in Vira for its owner. This is "
        "first-run setup: nothing is configured yet and you are building "
        "the foundation every future application will draw on.",
        "",
        f"RESUME: {resume}",
        f"WHAT THEY ARE LOOKING FOR: {target}",
    ]
    if companies:
        lines.append(f"COMPANIES THEY NAMED: {companies}")
    if location:
        lines.append(f"LOCATION: {location}")
    if comp:
        lines.append(f"COMPENSATION FLOOR: {comp}")
    lines += [
        f"RECORD DIRECTORY: {record}",
        "",
        "STEP 1 — READ THE RESUME",
        f"Read {resume} in full. It is the starting point, not the final "
        "word. (It sits in a staging folder — step 2 gives it a permanent "
        "home.)",
        "",
        "STEP 2 — BUILD THE SELF-RECORD",
        f"Create {record}, copy the resume into it, and write two more "
        "files there.",
        "",
        "FACTS.md — the ground truth of what this person has actually "
        "done. Every role: employer, title, dates, what they owned, team "
        "size, systems, and any number the resume states. This file is a "
        "CLAIM GATE: every future CV, cover letter, and form answer is "
        "checked against it, and anything it does not support does not "
        "get asserted. So where the resume is vague, where a number is "
        "missing, where a date is ambiguous — WRITE THE GAP DOWN under a "
        "'Gaps to fill' heading. Do not smooth over it and do not invent "
        "a plausible number. A recorded gap is useful; a fabricated fact "
        "is a liability in an interview.",
        "",
        "CLAUDE.md — the standing rules for this folder, stated plainly: "
        "FACTS.md governs every claim made about the owner; everything "
        "produced here is a DRAFT; nothing is ever submitted on the "
        "owner's behalf.",
        "",
        "STEP 3 — WORK OUT THE BOARDS",
        "From what they are looking for, decide which companies' job "
        "boards to watch. Include the ones they named, and add the "
        "obvious peers they did not — the point is a real feed, not an "
        "echo of the prompt. For each company, find its actual applicant "
        "tracking system and board identifier. Supported kinds: "
        "greenhouse, ashby, lever, microsoft, google, and `manual` for a "
        "board that cannot be fetched (it will be shown as manual, never "
        "silently dropped). Verify the board slug resolves — a guessed "
        "slug is a board that returns nothing forever.",
        "",
        "STEP 4 — APPLY THE CONFIGURATION",
        "Call mcp__vira__configure_applications ONCE with the full "
        "payload. The server validates it, writes the config, registers "
        "the boards, and kicks off the first poll — do not edit "
        "data/config.json yourself and do not create the boards registry "
        "by hand.",
        "",
        "Payload shape (config_json):",
        '{"record_dir": str, "locations": [str], "remote_ok": bool, '
        '"boards": [{"company": str, "ats": str, "slug": str, '
        '"query": str, "location": str, "note": str}]}',
        "",
        "`locations` is the list of place names they will work in — leave "
        "it EMPTY if they did not say, because an empty list means "
        "unfiltered and a guessed city silently hides most of the board.",
        "",
        "STEP 5 — REPORT",
        "Say what you built, how many boards are registered, what the "
        "first poll found, and — most importantly — list the gaps you "
        "recorded in FACTS.md and ask them to fill the ones that matter. "
        "That list is the honest state of their record.",
    ]
    return "\n".join(lines), {"record_dir": record}


def _s(v):
    return " ".join(str(v or "").split())


PROMPTS = {"reader": _reader_prompt, "applications": _applications_prompt}


def setup_prompt(module_id, answers):
    """The dispatch prompt for a module's setup, plus the derived facts
    the server pinned (slug, record dir) so the caller can echo them."""
    if module_id not in PROMPTS:
        raise ValueError(f"unknown module: {module_id}")
    mod = BY_ID[module_id]
    missing = [q["label"] for q in mod["ask"]
               if q.get("required") and not _s((answers or {}).get(q["id"]))]
    if missing:
        raise ValueError("missing required answers: " + "; ".join(missing))
    return PROMPTS[module_id](answers or {})


# ------------------------------------------------- the validated apply
# The setup session proposes; this applies. Config is the reason the
# boundary exists: these keys decide what every other part of the module
# reads, and a session that hand-edited data/config.json could break the
# app for modules it was never asked to touch.


class ConfigError(ValueError):
    """Message written for the model that proposed the payload — handed
    back as the tool result so it can correct and retry."""


def configure_applications(payload):
    """Validate and apply an Applications setup payload.

    Creates the record and analysis directories, pins the paths and the
    location rule into config, registers the boards, and starts the first
    poll in the background (a cold sweep of a dozen boards is HTTP-bound
    and would otherwise hold the session's tool call open for a minute)."""
    from . import jobboards, onboard

    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except json.JSONDecodeError as e:
            raise ConfigError(f"config_json is not valid JSON ({e})")
    if not isinstance(payload, dict):
        raise ConfigError("config_json must be a JSON object")

    record = _s(payload.get("record_dir"))
    if not record:
        raise ConfigError("record_dir is required — the folder that holds "
                          "FACTS.md and every application package")
    root = Path(record).expanduser()
    if not root.is_absolute():
        raise ConfigError(f"record_dir must be an absolute path (or ~/…), "
                          f"got {record!r}")

    locations = payload.get("locations") or []
    if isinstance(locations, str):
        locations = [p.strip() for p in locations.split(",") if p.strip()]
    if not isinstance(locations, list):
        raise ConfigError("locations must be a list of place names "
                          "(empty list = unfiltered)")
    locations = [_s(x)[:80] for x in locations if _s(x)][:20]

    boards = payload.get("boards") or []
    if not isinstance(boards, list):
        raise ConfigError("boards must be a list")
    if not boards:
        raise ConfigError("boards is empty — the module has nothing to "
                          "poll. Register at least one company board.")
    clean_boards = []
    for i, b in enumerate(boards):
        if not isinstance(b, dict):
            raise ConfigError(f"boards[{i}] is not an object")
        company = _s(b.get("company"))
        ats = _s(b.get("ats")).lower()
        if not company:
            raise ConfigError(f"boards[{i}].company is required")
        if ats not in jobboards.ATS_KINDS:
            raise ConfigError(
                f"boards[{i}].ats must be one of "
                f"{'|'.join(jobboards.ATS_KINDS)}, got {ats!r}")
        clean_boards.append({
            "company": company, "ats": ats,
            "slug": _s(b.get("slug")), "query": _s(b.get("query")),
            "location": _s(b.get("location")), "note": _s(b.get("note")),
        })

    analysis = root / "analysis"
    try:
        (analysis / "candidate-universe" / "role").mkdir(parents=True,
                                                         exist_ok=True)
        (analysis / "boards").mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ConfigError(f"could not create {analysis}: {e}")

    onboard.config_set(
        self_record=str(root),
        applications_universe=str(analysis),
        applications_locations=locations,
        applications_remote_ok=payload.get("remote_ok", True) is not False,
    )

    added, skipped = [], []
    for b in clean_boards:
        try:
            jobboards.add_board(**b)
            added.append(b["company"])
        except ValueError as e:
            skipped.append(f"{b['company']} ({e})")

    if added:
        threading.Thread(target=_first_poll, daemon=True,
                         name="frontdoor-first-poll").start()

    return {"record_dir": str(root), "universe_dir": str(analysis),
            "locations": locations, "added": added, "skipped": skipped,
            "polling": bool(added)}


def _first_poll():
    from . import jobboards
    try:
        # Never ping the phone for the first sweep: every role on every
        # board is "new" the first time, and a cold poll would arrive as
        # a hundred-role text message.
        jobboards.poll_once(notify_new=False)
    except Exception:  # noqa: BLE001 — a failed first poll is not fatal
        pass


def stage_resume(filename, data_b64):
    """Park an uploaded resume where the setup session can read it.

    The record directory does not exist yet when the file arrives — the
    interview has not run — so the upload lands in a staging folder and
    the session copies it into the record it creates. Base64 in a JSON
    body rather than multipart: every other endpoint in this app is JSON,
    and a resume is small enough that one more dependency would be a poor
    trade."""
    name = Path(str(filename or "resume")).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "resume"
    if len(name) > 80:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:70] + dot + ext) if dot else name[:80]
    try:
        blob = base64.b64decode(str(data_b64 or ""), validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("file is not valid base64")
    if not blob:
        raise ValueError("file is empty")
    if len(blob) > MAX_UPLOAD:
        raise ValueError(
            f"file is {len(blob) // 1024}KB — the cap is "
            f"{MAX_UPLOAD // 1024}KB. A resume should be well under it.")
    dest_dir = ROOT / "data" / "frontdoor"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{int(time.time())}-{name}"
    dest.write_bytes(blob)
    return {"path": str(dest), "name": name, "bytes": len(blob)}


def configure_summary(res):
    """One line for the tool result the model reads back."""
    bits = [f"Applications configured. Record at {res['record_dir']}, "
            f"universe at {res['universe_dir']}."]
    if res["added"]:
        bits.append(f"Registered {len(res['added'])} boards: "
                    + ", ".join(res["added"]) + ".")
    if res["skipped"]:
        bits.append("Skipped: " + "; ".join(res["skipped"]) + ".")
    bits.append("Locations: " + (", ".join(res["locations"])
                                 if res["locations"] else
                                 "unfiltered (none given)") + ".")
    if res["polling"]:
        bits.append("First poll running in the background — roles will "
                    "appear in the module shortly.")
    return " ".join(bits)
