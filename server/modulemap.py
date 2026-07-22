"""The system map — a living registry of every Vira module.

One structured record per module: what it is, what it does, what it
reads and feeds, and (for the ask/search surfaces) which corpus it
answers from. The Modules page of the system atlas
(static/explainer/modules.html) renders this registry live, so the map
is only ever as stale as the registry — never as stale as a frozen
diagram export.

Two halves, per the house rule that everything deterministic stays
deterministic:

  - DERIVED AT READ TIME: `payload()` returns the registry plus the
    recent Vira-scoped change log, each entry keyword-tagged with the
    modules it touches. No sync step; the "what changed" rail is always
    current.
  - AI-REFRESHED: the module descriptions themselves drift as the app
    grows. `refresh_prompt()` composes a job (dispatched by the weekly
    "System map" routine, or POST /api/map/refresh) that reads the
    recent change log and rewrites the registry via the native
    `update_module_map` session tool — the write is validated and
    applied server-side (viratools.py), never by the agent's own hands.

Store: data/modules.json (instance copy, routine-editable, backed by
the seed below on first read). The seed ships with the code so a fresh
clone gets a correct-as-of-last-commit map.
"""
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "modules.json"

LAYERS = ("source", "store", "engine", "surface")
_lock = threading.Lock()

TODAY = "2026-07-12"

DEFAULT_MODULES = [
    # ---------- sources: data that exists outside Vira ----------
    {"id": "chat-db", "name": "Messages (chat.db)", "layer": "source",
     "group": "communicate", "kind": "macOS database",
     "what": "The live iMessage history on this Mac, read in place. Every "
             "thread, group, and attachment; the watcher tails it for new "
             "arrivals. Needs Full Disk Access granted to Vira's python.",
     "links": [], "keywords": ["chat.db", "imessage", "full disk"],
     "updated": TODAY},
    {"id": "addressbook", "name": "AddressBook (photos)", "layer": "source",
     "group": "communicate", "kind": "macOS database",
     "what": "Apple's contacts database, used for one thing: contact "
             "photos, extracted into Vira's photo cache keyed by person.",
     "links": [], "keywords": ["addressbook", "contact photo"],
     "updated": TODAY},
    {"id": "crm-data", "name": "CRM stores", "layer": "source",
     "group": "communicate", "kind": "JSON files (~/workspace/crm)",
     "what": "The memory: ~1,000 people in people.json, evidence-rich "
             "master records, one synthesized dossier per active person, "
             "and the exported iMessage archive. Vira reads them in place "
             "and writes back exactly three things: hooks, open loops, and "
             "new/renamed people — atomically, stamped, backed up.",
     "links": [], "keywords": ["crm", "people.json", "profile", "dossier",
                               "hook", "open loop"],
     "updated": TODAY},
    {"id": "vault-src", "name": "Knowledge vault (TC-IL)", "layer": "source",
     "group": "know", "kind": "Obsidian vault",
     "what": "Thousands of markdown notes — companies, people, decisions, "
             "session retros. The raw material the Brain answers from.",
     "links": [], "keywords": ["vault", "obsidian", "tc-il"],
     "updated": TODAY},
    {"id": "retros-src", "name": "Session retros", "layer": "source",
     "group": "operate", "kind": "markdown (~/TC-IL/Sessions)",
     "what": "One retrospective per Vira working session; each 'Shipped' "
             "section is that session's changes. The change log is derived "
             "from these at read time — they are the source of truth for "
             "shipped work.",
     "links": [], "keywords": ["retro", "session", "shipped"],
     "updated": TODAY},
    {"id": "mail-src", "name": "Mailboxes", "layer": "source",
     "group": "communicate", "kind": "Gmail IMAP + M365 Graph",
     "what": "Both mail accounts, watched for arrivals and searched on "
             "demand. Secrets live in the macOS Keychain only.",
     "links": [], "keywords": ["mail", "gmail", "imap", "graph", "m365",
                               "outlook"],
     "updated": TODAY},
    {"id": "whatsapp-src", "name": "WhatsApp (linked device)", "layer": "source",
     "group": "communicate", "kind": "multi-device sidecar",
     "what": "Inbound WhatsApp via a local linked-device sidecar "
             "(bridge/whatsapp, receive-only). Messages join the live feed "
             "by phone number; content never leaves the machine.",
     "links": [], "keywords": ["whatsapp", "sidecar", "linked device",
                               "baileys"],
     "updated": TODAY},
    {"id": "calendars-src", "name": "Calendars", "layer": "source",
     "group": "rhythm", "kind": "macOS EventKit + M365",
     "what": "Personal, family, and birthday calendars merged with the "
             "work calendar — the schedule half of the Daily Brief and the "
             "agent's calendar tool.",
     "links": [], "keywords": ["calendar", "eventkit"],
     "updated": TODAY},
    {"id": "mercury-src", "name": "Mercury bank feed", "layer": "source",
     "group": "money", "kind": "bank API",
     "what": "Transaction history for the subscriptions ledger — polled, "
             "never written. Token in the Keychain.",
     "links": [], "keywords": ["mercury", "transaction", "bank"],
     "updated": TODAY},
    {"id": "library-src", "name": "Claude library", "layer": "source",
     "group": "operate", "kind": "~/.claude",
     "what": "The central library of skills, commands, and agents. The "
             "Actions window is a cockpit over this — every card is one "
             "library entry.",
     "links": [], "keywords": ["skill", "command", "library", "cockpit"],
     "updated": TODAY},

    # ---------- stores: state Vira builds and owns (data/) ----------
    {"id": "media-index", "name": "Media index", "layer": "store",
     "group": "know", "kind": "sqlite + vectors",
     "what": "Everything ever shared in iMessage — photos, videos, links, "
             "documents, voice memos — indexed three ways: exact text, "
             "scene similarity, text similarity, plus named faces. This is "
             "what the Search window answers from.",
     "links": [{"to": "chat-db", "how": "indexes attachments from"}],
     "keywords": ["media index", "siglip", "face", "ocr"],
     "updated": TODAY},
    {"id": "vault-index", "name": "Vault index", "layer": "store",
     "group": "know", "kind": "sqlite + vectors",
     "what": "The vault chunked by heading path and indexed for exact and "
             "semantic recall. Regenerable sidecar; rescans every five "
             "minutes. This is what the Brain answers from.",
     "links": [{"to": "vault-src", "how": "indexes"}],
     "keywords": ["vault index", "chunk", "embed"],
     "updated": TODAY},
    {"id": "ideas-store", "name": "Ideas backlog", "layer": "store",
     "group": "operate", "kind": "data/ideas.json",
     "what": "The cross-session backlog: every idea and on-hold item, "
             "tagged by project and status. /resume reads it, "
             "/close-session syncs into it, Muse proposes into it. The "
             "master copy — retro Ideas sections are mirrors.",
     "links": [], "keywords": ["idea", "backlog", "on-hold", "proposed"],
     "updated": TODAY},
    {"id": "job-ledger", "name": "Job ledger", "layer": "store",
     "group": "operate", "kind": "data/jobs-log.json",
     "what": "A durable row for every agent job ever launched: prompt, "
             "target repo, model, outcome, and the session id that names "
             "the on-disk transcript. Cross-process safe; jobs survive "
             "restarts.",
     "links": [], "keywords": ["ledger", "job", "transcript"],
     "updated": TODAY},
    {"id": "routines-store", "name": "Routines store", "layer": "store",
     "group": "operate", "kind": "data/routines.json",
     "what": "The standing agent loops and their cadence, last run, and "
             "last outcome.",
     "links": [], "keywords": ["routine", "cadence"],
     "updated": TODAY},
    {"id": "module-registry", "name": "Module registry", "layer": "store",
     "group": "know", "kind": "data/modules.json",
     "what": "This map's own data: one record per module, refreshed "
             "periodically from the change log by the System map routine. "
             "The Modules atlas page renders it live.",
     "links": [], "keywords": ["module", "registry", "map", "atlas",
                               "explainer"],
     "updated": TODAY},
    {"id": "vira-state", "name": "Instance state", "layer": "store",
     "group": "operate", "kind": "data/ + Keychain",
     "what": "Everything else Vira remembers about itself: config, the "
             "watcher watermark, feed read-state, triage dismissals, the "
             "photo cache, subscription ledger, daily backups of the "
             "non-regenerable files. Secrets only in the Keychain.",
     "links": [], "keywords": ["config", "watermark", "backup"],
     "updated": TODAY},

    # ---------- engines: the server subsystems ----------
    {"id": "watcher", "name": "iMessage watcher", "layer": "engine",
     "group": "communicate", "kind": "background thread",
     "what": "Polls chat.db every three seconds past a watermark, joins "
             "senders to CRM people, and pushes new messages to the feed "
             "and the live event stream every open page listens to.",
     "links": [{"to": "chat-db", "how": "tails"},
               {"to": "crm-data", "how": "joins senders against"}],
     "endpoints": ["/api/feed", "/api/stream"],
     "keywords": ["watcher", "feed", "sse", "stream"],
     "updated": TODAY},
    {"id": "mail-engine", "name": "Mail engine", "layer": "engine",
     "group": "communicate", "kind": "watchers + drafts",
     "what": "Watches both mailboxes, folds mail into the feed and brief, "
             "searches on demand, and saves drafted replies as real drafts "
             "in the account.",
     "links": [{"to": "mail-src", "how": "polls + drafts into"}],
     "endpoints": ["/api/mail/draft"],
     "keywords": ["mail", "draft", "imap"],
     "updated": TODAY},
    {"id": "media-engine", "name": "Media engine", "layer": "engine",
     "group": "know", "kind": "indexer + hybrid search",
     "what": "Builds the media index and answers queries by fusing exact "
             "text, scene similarity, text similarity, faces, and filters "
             "— any layer can carry a query the others miss. Its ask mode "
             "has the model turn a question into a structured search plan, "
             "runs the plan deterministically, and relaxes constraints one "
             "at a time when the strict answer is empty, so wrong-memory "
             "questions get a near-miss answer instead of a bare no.",
     "links": [{"to": "media-index", "how": "builds + queries"},
               {"to": "suggest", "how": "borrows the model backend of"}],
     "endpoints": ["/api/search", "/api/search/ask", "/api/search/faces"],
     "keywords": ["search", "media", "ask vira", "rrf", "hybrid"],
     "updated": TODAY},
    {"id": "vault-engine", "name": "Vault engine", "layer": "engine",
     "group": "know", "kind": "indexer + grounded ask",
     "what": "Indexes the vault and answers questions from it: retrieve "
             "the best chunks, send only those excerpts to the model, and "
             "return an answer whose every citation is a real note you can "
             "open. Nothing leaves the machine at index time.",
     "links": [{"to": "vault-index", "how": "builds + queries"},
               {"to": "vault-src", "how": "rescans"}],
     "endpoints": ["/api/vault/search", "/api/vault/ask", "/api/vault/note"],
     "keywords": ["vault", "brain", "citation", "grounded"],
     "updated": TODAY},
    {"id": "suggest", "name": "Reply drafting", "layer": "engine",
     "group": "communicate", "kind": "headless model calls",
     "what": "Voice-matched suggested replies and hook openers, drafted "
             "from the dossier plus the live thread. The one place message "
             "content meets the model; Max-plan CLI by default, API "
             "optional.",
     "links": [{"to": "crm-data", "how": "reads dossiers from"},
               {"to": "chat-db", "how": "reads threads from"}],
     "endpoints": ["/api/suggest"],
     "keywords": ["suggest", "reply", "draft", "voice"],
     "updated": TODAY},
    {"id": "brief-engine", "name": "Brief engine", "layer": "engine",
     "group": "rhythm", "kind": "deterministic composer",
     "what": "Assembles the daily brief: today and tomorrow's calendar, "
             "who is waiting on a reply, open loops, contacts going quiet, "
             "renewals, queued drafts, triage count. Deterministic "
             "sections; the model only narrates on request.",
     "links": [{"to": "calendars-src", "how": "reads"},
               {"to": "crm-data", "how": "reads loops + cadence from"},
               {"to": "subs-engine", "how": "gets renewals from"},
               {"to": "watcher", "how": "gets waiting-on-reply from"}],
     "endpoints": ["/api/brief"],
     "keywords": ["brief", "waiting", "quiet", "journal"],
     "updated": TODAY},
    {"id": "radar-engine", "name": "Radar engine", "layer": "engine",
     "group": "rhythm", "kind": "scoring",
     "what": "Scores who to talk to next (every row says why) and sizes "
             "GROUPINGS — two to five people who share ground, with the "
             "move that fits (post to the thread they already have, start "
             "a group chat, make an introduction). Two triggers: standing "
             "profile overlap, and links your contacts actually shared "
             "lately. An item that lands on one person becomes a "
             "conversation marker on their row instead.",
     "links": [{"to": "crm-data", "how": "scores people from"},
               {"to": "chat-db", "how": "reads shared links from"}],
     "endpoints": ["/api/radar"],
     "keywords": ["radar", "grouping", "marker", "intro", "score"],
     "updated": TODAY},
    {"id": "sessions", "name": "Agent runtime", "layer": "engine",
     "group": "operate", "kind": "supervisor + durable runner",
     "what": "Runs every agent job as a live two-way session in a detached "
             "durable process: streaming terminals, permission cards, "
             "say-mid-run, restart survival. Sessions carry native Vira "
             "tools — CRM lookup, threads, mail, media and vault search, "
             "calendar, the brief, idea staging, and the map registry "
             "write — so agents get the deep connection without shelling "
             "out.",
     "links": [{"to": "job-ledger", "how": "records every run in"},
               {"to": "library-src", "how": "runs skills/commands from"}],
     "endpoints": ["/api/actions/run", "/api/jobs", "/api/session/{sid}/*"],
     "keywords": ["session", "runner", "terminal", "permission", "durable",
                  "viratools"],
     "updated": TODAY},
    {"id": "circuits-engine", "name": "Circuits", "layer": "engine",
     "group": "operate", "kind": "pipeline runner",
     "what": "Multi-step agent pipelines with handoffs and a fresh-eyes "
             "judge between steps — grade gates decide retry, continue, or "
             "stop.",
     "links": [{"to": "sessions", "how": "dispatches steps through"}],
     "endpoints": ["/api/circuits"],
     "keywords": ["circuit", "judge", "pipeline", "grade"],
     "updated": TODAY},
    {"id": "scheduler", "name": "Routine scheduler", "layer": "engine",
     "group": "operate", "kind": "60s tick",
     "what": "Dispatches standing loops on their cadence: Muse proposes "
             "ideas each morning, watchers watch, digests digest, the "
             "System map refresh keeps this very page current. Skips while "
             "the previous run is live; pings when a run finishes.",
     "links": [{"to": "routines-store", "how": "reads + stamps"},
               {"to": "sessions", "how": "dispatches jobs through"}],
     "endpoints": ["/api/routines"],
     "keywords": ["routine", "muse", "scheduler", "loop", "digest"],
     "updated": TODAY},
    {"id": "subs-engine", "name": "Subscriptions engine", "layer": "engine",
     "group": "money", "kind": "poller + cadence detector",
     "what": "Polls Mercury, reconciles transactions into a subscription "
             "ledger, detects billing cadence deterministically, forecasts "
             "renewals, and files receipts.",
     "links": [{"to": "mercury-src", "how": "polls"},
               {"to": "vira-state", "how": "keeps the ledger in"}],
     "endpoints": ["/api/subs"],
     "keywords": ["subscription", "renewal", "receipt", "ledger",
                  "mercury"],
     "updated": TODAY},
    {"id": "triage-engine", "name": "Triage", "layer": "engine",
     "group": "communicate", "kind": "identity resolution",
     "what": "Unknown senders get looked up, identified, and either added "
             "to the CRM as real people or dismissed — the only path that "
             "writes new people into the registry, always with a backup "
             "first.",
     "links": [{"to": "crm-data", "how": "appends/renames people in"},
               {"to": "chat-db", "how": "finds unknowns in"}],
     "endpoints": ["/api/triage", "/api/crm/add"],
     "keywords": ["triage", "unknown", "unidentified"],
     "updated": TODAY},
    {"id": "changelog-engine", "name": "Change log", "layer": "engine",
     "group": "operate", "kind": "derived at read time",
     "what": "Derives the per-session change log from the session retros, "
             "resolved backlog items, and the job ledger — Vira-project "
             "entries only (scoped 2026-07-12; other projects keep their "
             "own logs). No parallel store to sync; the retros are the "
             "source of truth.",
     "links": [{"to": "retros-src", "how": "parses Shipped sections of"},
               {"to": "ideas-store", "how": "folds resolved items from"},
               {"to": "job-ledger", "how": "folds Vira-repo jobs from"}],
     "endpoints": ["/api/changelog"],
     "keywords": ["change log", "changelog", "shipped", "scoped"],
     "updated": TODAY},
    {"id": "housekeeping", "name": "Housekeeping", "layer": "engine",
     "group": "operate", "kind": "notify + updater + backups",
     "what": "iMessage pings when something needs eyes, the in-app git "
             "updater (one click fast-forwards and restarts), and daily "
             "rotation of the non-regenerable state files.",
     "links": [{"to": "vira-state", "how": "backs up"}],
     "endpoints": ["/api/notify", "/api/update"],
     "keywords": ["notify", "update", "backup", "launchd"],
     "updated": TODAY},

    # ---------- surfaces: what the owner actually touches ----------
    {"id": "feed-win", "name": "Incoming", "layer": "surface",
     "group": "communicate", "kind": "dock window / mobile tab",
     "what": "The live feed: every new message joined to its person, with "
             "read state, swipe actions, and one-tap reply drafting.",
     "links": [{"to": "watcher", "how": "streams from"},
               {"to": "suggest", "how": "drafts replies via"}],
     "keywords": ["feed", "incoming"],
     "updated": TODAY},
    {"id": "people-win", "name": "People", "layer": "surface",
     "group": "communicate", "kind": "dock window / mobile tab",
     "what": "The CRM directory and the person pages behind it: dossier on "
             "the left, live conversation on the right, hooks and open "
             "loops editable in place. Its search box filters the "
             "directory by name, email, or phone — navigation, not "
             "content search.",
     "ask": {"label": "Search name, email, phone",
             "corpus": "CRM registry (who someone is)",
             "engine": "instant filter in the page"},
     "links": [{"to": "crm-data", "how": "renders + writes back to"},
               {"to": "vault-engine", "how": "pulls person notes from"},
               {"to": "suggest", "how": "drafts via"}],
     "keywords": ["people", "person page", "profile", "focus mode"],
     "updated": TODAY},
    {"id": "search-win", "name": "Search", "layer": "surface",
     "group": "know", "kind": "dock window",
     "what": "Finds things people sent you. Search mode is instant hybrid "
             "retrieval over every photo, link, and document ever shared; "
             "Ask mode answers questions about that same corpus and "
             "handles misremembered details with near-miss answers.",
     "ask": {"label": "Search / Ask Vira",
             "corpus": "everything ever shared in iMessage",
             "engine": "media engine (instant; ask mode ~seconds)"},
     "links": [{"to": "media-engine", "how": "queries"}],
     "keywords": ["search window", "shared media", "ask vira"],
     "updated": TODAY},
    {"id": "brain-win", "name": "Brain", "layer": "surface",
     "group": "know", "kind": "dock window",
     "what": "Ask what your vault knows — companies, people, decisions, "
             "past sessions. Every answer cites the notes it came from; "
             "tap a chip to open the note.",
     "ask": {"label": "Ask your second brain",
             "corpus": "the knowledge vault (what you wrote down)",
             "engine": "vault engine (~seconds, grounded + cited)"},
     "links": [{"to": "vault-engine", "how": "asks"}],
     "keywords": ["brain", "second brain"],
     "updated": TODAY},
    {"id": "actions-win", "name": "Actions", "layer": "surface",
     "group": "operate", "kind": "dock window / mobile tab",
     "what": "The cockpit: every library skill and command as a card, plus "
             "a free-form bar that hands any request to a live agent "
             "session. The most powerful ask in the app — and the "
             "slowest; it can use every other module's engine as a tool.",
     "ask": {"label": "Ask Claude anything",
             "corpus": "everything (live agent with native Vira tools)",
             "engine": "agent runtime (a real session; ~minutes)"},
     "links": [{"to": "sessions", "how": "launches jobs through"},
               {"to": "library-src", "how": "lists cards from"}],
     "keywords": ["actions", "cockpit", "run"],
     "updated": TODAY},
    {"id": "brief-win", "name": "Daily Brief", "layer": "surface",
     "group": "rhythm", "kind": "dock window",
     "what": "The morning read: schedule, who is waiting, open loops, "
             "renewals — every row interactive: clear it, note it, or tell "
             "Vira what you know.",
     "links": [{"to": "brief-engine", "how": "renders"}],
     "keywords": ["daily brief"],
     "updated": TODAY},
    {"id": "triage-win", "name": "Triage", "layer": "surface",
     "group": "communicate", "kind": "dock window",
     "what": "The unknown-senders queue: identify, add to the CRM, or "
             "dismiss.",
     "links": [{"to": "triage-engine", "how": "drives"}],
     "keywords": ["triage window"],
     "updated": TODAY},
    {"id": "jobs-win", "name": "Jobs", "layer": "surface",
     "group": "operate", "kind": "dock window",
     "what": "Live and historical agent runs. Every job opens in its own "
             "floating terminal; history reopens any past run read-only "
             "from the ledger.",
     "links": [{"to": "sessions", "how": "watches"},
               {"to": "job-ledger", "how": "renders history from"}],
     "keywords": ["jobs window", "history", "terminal"],
     "updated": TODAY},
    {"id": "ideas-win", "name": "Ideas & On-Hold", "layer": "surface",
     "group": "operate", "kind": "dock window",
     "what": "The backlog as a work queue: add, edit, filter by project, "
             "and dispatch any idea as a Plan or Implement job. The second "
             "tab is the change log — every shipped change per session, "
             "Vira-scoped.",
     "links": [{"to": "ideas-store", "how": "edits"},
               {"to": "changelog-engine", "how": "renders the log from"},
               {"to": "sessions", "how": "dispatches ideas through"}],
     "keywords": ["ideas window", "plan", "implement"],
     "updated": TODAY},
    {"id": "radar-win", "name": "Radar", "layer": "surface",
     "group": "rhythm", "kind": "dock window",
     "what": "Who to talk to next and who to put in a room together — the "
             "relationship rhythm surface. Grouping cards name the topic, "
             "the audience, and the move; person rows carry the live "
             "marker when something just landed on their ground.",
     "links": [{"to": "radar-engine", "how": "renders"}],
     "keywords": ["radar window", "groupings"],
     "updated": TODAY},
    {"id": "circuits-win", "name": "Circuits", "layer": "surface",
     "group": "operate", "kind": "dock window",
     "what": "Build and run multi-step pipelines; watch each step's "
             "terminal and the judge's verdicts between them.",
     "links": [{"to": "circuits-engine", "how": "drives"}],
     "keywords": ["circuits window"],
     "updated": TODAY},
    {"id": "routines-win", "name": "Agent Loops", "layer": "surface",
     "group": "operate", "kind": "dock window",
     "what": "The standing loops: what runs on what cadence, when it last "
             "ran, and how it went. Muse's proposals land in Ideas for "
             "approval.",
     "links": [{"to": "scheduler", "how": "manages"}],
     "keywords": ["agent loops", "routines window"],
     "updated": TODAY},
    {"id": "subs-win", "name": "Subscriptions", "layer": "surface",
     "group": "money", "kind": "dock window / mobile tab",
     "what": "The subscription ledger: what renews when, what looks off, "
             "receipts attached.",
     "links": [{"to": "subs-engine", "how": "renders"}],
     "keywords": ["subscriptions window"],
     "updated": TODAY},
    {"id": "picker-win", "name": "Morning Picker", "layer": "surface",
     "group": "money", "kind": "dock window (once-a-day)",
     "what": "The keyframe picker that arrives with the 06:00 message — "
             "pick, apply headlessly, done.",
     "links": [{"to": "subs-engine", "how": "feeds picks to"}],
     "keywords": ["morning picker", "keyframe"],
     "updated": TODAY},
    {"id": "map-win", "name": "System Map", "layer": "surface",
     "group": "know", "kind": "dock window + atlas page",
     "what": "This map: every module, what it does, how they connect, and "
             "what changed recently — rendered live from the module "
             "registry inside the system atlas. Refreshed periodically "
             "from the change log by its routine.",
     "links": [{"to": "module-registry", "how": "renders"},
               {"to": "changelog-engine", "how": "shows recent changes from"}],
     "endpoints": ["/api/map", "/api/map/refresh"],
     "keywords": ["system map", "modules page", "atlas"],
     "updated": TODAY},
    {"id": "quick", "name": "Quick actions", "layer": "surface",
     "group": "operate", "kind": "Cmd-K + right-click",
     "what": "The palette opens any window or person from the keyboard; "
             "right-click anywhere captures an idea about what you're "
             "looking at or spawns an agent session with the click's "
             "context attached.",
     "links": [{"to": "ideas-store", "how": "captures ideas into"},
               {"to": "sessions", "how": "spawns context sessions via"}],
     "keywords": ["palette", "right-click", "context menu"],
     "updated": TODAY},
]

GROUPS = {
    "communicate": "Communicate",
    "know": "Know",
    "operate": "Operate",
    "rhythm": "Rhythm",
    "money": "Money",
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load():
    try:
        s = json.loads(STORE.read_text())
        if isinstance(s, dict) and isinstance(s.get("modules"), list) \
                and s["modules"]:
            return s
    except (OSError, json.JSONDecodeError):
        pass
    return {"modules": [dict(m) for m in DEFAULT_MODULES],
            "meta": {"seeded": _now_iso(), "last_refresh": None}}


def _save(s):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
    tmp.replace(STORE)


def list_modules():
    with _lock, locked(STORE):
        return _load()["modules"]


def validate(mods, previous_ids=None):
    """Schema-check a candidate registry. Returns a problem string or
    None. Guards the native-tool write path against a bad or destructive
    replacement."""
    if not isinstance(mods, list) or not mods:
        return "modules must be a non-empty list"
    seen = set()
    for m in mods:
        if not isinstance(m, dict):
            return "every module must be an object"
        mid = m.get("id") or ""
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", mid):
            return f"bad module id: {mid!r}"
        if mid in seen:
            return f"duplicate module id: {mid}"
        seen.add(mid)
        if m.get("layer") not in LAYERS:
            return f"{mid}: layer must be one of {LAYERS}"
        for field in ("name", "what"):
            if not (m.get(field) or "").strip():
                return f"{mid}: {field} is required"
        for link in m.get("links") or []:
            if not isinstance(link, dict) or not link.get("to"):
                return f"{mid}: links must be objects with a 'to'"
    if previous_ids:
        kept = len(previous_ids & seen)
        if kept < len(previous_ids) * 0.6:
            return ("replacement drops too many existing modules "
                    f"({kept}/{len(previous_ids)} kept) — refusing; "
                    "update entries rather than starting over")
    return None


def replace_modules(mods):
    """Validated full-registry replacement (the native tool's write path).
    Returns a summary string; raises ValueError on a bad payload."""
    with _lock, locked(STORE):
        s = _load()
        prev = {m["id"] for m in s["modules"]}
        problem = validate(mods, previous_ids=prev)
        if problem:
            raise ValueError(problem)
        new = {m["id"] for m in mods}
        s["modules"] = mods
        s.setdefault("meta", {})["last_refresh"] = _now_iso()
        _save(s)
    added, removed = sorted(new - prev), sorted(prev - new)
    return (f"Registry updated: {len(mods)} modules"
            + (f", added {', '.join(added)}" if added else "")
            + (f", removed {', '.join(removed)}" if removed else "") + ".")


def payload():
    """Everything the Modules page needs: the registry, the group legend,
    and the recent Vira-scoped change log with each entry tagged with the
    modules its text mentions."""
    from . import changelog
    with _lock, locked(STORE):
        s = _load()
        if not STORE.exists():   # first read seeds the instance copy
            _save(s)
    mods = s["modules"]
    recent = []
    for g in changelog.groups()[:8]:
        entries = []
        for e in g["entries"]:
            low = e["text"].lower()
            tags = [m["id"] for m in mods
                    if any(k in low for k in m.get("keywords") or [])]
            entries.append({**e, "modules": tags[:4]})
        recent.append({**g, "entries": entries})
    return {"modules": mods, "groups": GROUPS, "layers": list(LAYERS),
            "meta": s.get("meta") or {}, "recent": recent}


def refresh_prompt():
    """The System-map refresh job: composed server-side with the current
    registry and change log inline, so the session needs no file reads
    outside its own repo and writes only through the native tool."""
    from . import changelog
    s = _load()
    log_lines = []
    for g in changelog.groups()[:10]:
        head = g["date"] or "recent"
        for e in g["entries"]:
            log_lines.append(f"[{head}] ({e['kind']}) {e['text']}")
    return (
        "You are Vira's cartographer. The System Map (the Modules page of "
        "the system atlas) renders a registry of every module in this app "
        "— data/modules.json in this repo. Your job: bring that registry "
        "up to date with what actually shipped, using the change log "
        "below.\n\n"
        "1. Read the CURRENT REGISTRY (inline below) against the RECENT "
        "CHANGE LOG (also below). Look for: new windows or engines that "
        "have no module entry; entries whose 'what' no longer matches "
        "reality; removed features still described. Cross-check the code "
        "when unsure — static/app.js WINDOWS array lists every dock "
        "window, server/*.py docstrings describe every engine.\n"
        "2. Produce the FULL updated registry (every module, not a diff) "
        "and submit it with ONE call to mcp__vira__update_module_map, "
        "passing the complete JSON array as modules_json. Keep ids "
        "stable; set each edited entry's 'updated' to today; keep prose "
        "in the house voice — plain words, what the module does for the "
        "owner, no jargon, no emojis. Only change entries the change log "
        "or the code actually justifies changing.\n"
        "3. If nothing needs changing, say so and stop — do not write.\n\n"
        "The write is validated server-side; a rejected payload returns "
        "the reason so you can fix and retry.\n\n"
        "CURRENT REGISTRY:\n"
        + json.dumps(s["modules"], indent=1, ensure_ascii=False)
        + "\n\nRECENT CHANGE LOG (Vira-scoped, newest first):\n"
        + ("\n".join(log_lines) or "(no entries yet)"))
