"""The source registry: every data source Vira can read, as a table.

models.PROVIDERS answers "what AI backends does this machine have?"; this
module answers the same question for data. Each source — the AddressBook
stores, chat.db, the Calendar store, a Google Contacts export, a mail
account — is a row: what kind of data it yields, which platforms it exists
on at all, and how to probe it. Setup renders the rows it gets, so the
platform fork (Apple cards on a Mac, the cross-platform siblings elsewhere)
is a filter over data, not branching in the wizard.

Three separate facts per source, probed independently (the PROVIDERS
lesson: installed, signed in, and capable are different things):

  - supported — the source can exist on this OS at all (platforms row)
  - present   — it actually exists on this machine (stores found, file on
                disk; always true for pure import paths like a CSV upload)
  - configured — Vira is really wired to it (people imported from it, the
                 store readable past Full Disk Access, an account added)

Everything is derived from the world at probe time and nothing is stored —
the onboard.steps() rule, which is what keeps Setup's re-entry free. All
probes are deterministic, cheap (a glob, a stat, a COUNT on an indexed
sqlite), and never raise: a probe that crashes the caller is worse than one
that says "unknown". Adding a source (a P2 Google Calendar sync, a Windows
messages bridge) is a new row here plus its card in the Setup UI — not new
wizard branching.
"""
import json
import sqlite3
from pathlib import Path

from . import settings

# Kinds — what a source yields. The step machine groups cards by these.
CONTACTS, MESSAGES, CALENDAR, MAIL = "contacts", "messages", "calendar", "mail"

# Platform tokens. A row lists where the source can exist; the current
# platform is read from settings.IS_MAC/IS_WIN at probe time (never captured
# at import) so tests — and a future second platform — can patch it.
MAC, WIN, LINUX = "mac", "win", "linux"
ALL_PLATFORMS = (MAC, WIN, LINUX)
_PLAT_NAMES = {MAC: "macOS", WIN: "Windows", LINUX: "Linux"}

# Apple's local stores, named once. onboard.py reads contacts through
# addressbook_dbs() and the feed status through chatdb_state(), so the
# registry and the importers can never disagree about where a store lives.
AB_SOURCES = Path.home() / "Library" / "Application Support" / "AddressBook"
CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
CAL_DB = (Path.home() / "Library" / "Group Containers"
          / "group.com.apple.calendar" / "Calendar.sqlitedb")


def _platform():
    return MAC if settings.IS_MAC else WIN if settings.IS_WIN else LINUX


def platform_label():
    """"macOS" / "Windows" / "Linux" — for honest skip reasons in Setup."""
    return _PLAT_NAMES[_platform()]


def _crm_root() -> Path:
    """The configured CRM root — the REAL one, never the fixture redirect
    (Setup reports the owner's actual state, same as onboard.crm_target)."""
    return Path(str(settings.get("crm_root"))).expanduser()


def _people():
    try:
        doc = json.loads((_crm_root() / "people.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return doc.get("people", []) if isinstance(doc, dict) else []


def _imported(ctx, source_tag):
    """How many CRM people carry this import tag — the importers stamp
    refs.import_source, so 'configured' is readable off the data itself."""
    return sum(1 for p in ctx["people"]
               if (p.get("refs") or {}).get("import_source") == source_tag)


# ---------- store probes (shared with onboard.py) ----------

def addressbook_dbs():
    dbs = sorted(AB_SOURCES.glob("Sources/*/AddressBook-v22.abcddb"))
    root = AB_SOURCES / "AddressBook-v22.abcddb"
    if root.exists():
        dbs.append(root)
    return dbs


def _sqlite_state(path, probe_sql):
    """missing | no-access | ok for a read-only local store. "no-access" is
    the pre-Full-Disk-Access shape: the file is there, the read fails."""
    if not path.exists():
        return "missing"
    con = None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.execute(probe_sql).fetchone()
        return "ok"
    except sqlite3.Error:
        return "no-access"
    finally:
        if con is not None:
            con.close()


def _sqlite_count(path, sql):
    con = None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        return int(con.execute(sql).fetchone()[0] or 0)
    except sqlite3.Error:
        return 0
    finally:
        if con is not None:
            con.close()


def chatdb_state():
    return _sqlite_state(CHAT_DB, "SELECT 1 FROM message LIMIT 1")


def _mail_accounts():
    from . import mail
    try:
        raw = json.loads(mail.ACCOUNTS.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return raw if isinstance(raw, list) else raw.get("accounts", [])


# ---------- per-source probes ----------
# Each returns the world-facing half of a record: present / configured /
# count / detail / action. The identity half comes from the table row.

def _probe_apple_contacts(ctx):
    dbs = addressbook_dbs()
    n = _imported(ctx, "apple-contacts")
    if not dbs:
        detail = ("No AddressBook stores found — is this Mac signed into "
                  "Contacts?")
    elif n:
        detail = f"{n} imported from this Mac's Contacts."
    else:
        detail = (f"{len(dbs)} store{'s' if len(dbs) != 1 else ''} found, "
                  "nothing imported yet.")
    return {"present": bool(dbs), "configured": n > 0, "count": n,
            "detail": detail,
            "action": "" if n or not dbs else "Import Apple Contacts"}


def _probe_google_csv(ctx):
    n = _imported(ctx, "google-csv")
    return {"present": True, "configured": n > 0, "count": n,
            "detail": (f"{n} imported from a Google Contacts export."
                       if n else "Export from Google Contacts (contacts."
                       "google.com > Export > Google CSV) and upload it."),
            "action": "" if n else "Import Google Contacts CSV"}


def _probe_imessage(ctx):
    state = chatdb_state()
    n = (_sqlite_count(CHAT_DB, "SELECT COUNT(*) FROM message")
         if state == "ok" else 0)
    detail = {"ok": f"{n:,} messages readable.",
              "no-access": "Messages database found but unreadable — grant "
                           "Full Disk Access.",
              "missing": "No Messages database on this machine."}[state]
    return {"present": state != "missing", "configured": state == "ok",
            "count": n, "detail": detail,
            "action": "" if state != "no-access" else "Grant Full Disk Access"}


def _probe_apple_calendar(ctx):
    state = _sqlite_state(CAL_DB, "SELECT 1 FROM CalendarItem LIMIT 1")
    n = (_sqlite_count(CAL_DB, "SELECT COUNT(*) FROM CalendarItem")
         if state == "ok" else 0)
    detail = {"ok": f"{n:,} calendar items readable.",
              "no-access": "Calendar store found but unreadable — grant "
                           "Full Disk Access.",
              "missing": "No local Calendar store on this machine."}[state]
    return {"present": state != "missing", "configured": state == "ok",
            "count": n, "detail": detail,
            "action": "" if state != "no-access" else "Grant Full Disk Access"}


def _probe_imap(ctx):
    n = sum(1 for a in _mail_accounts() if a.get("type") != "graph")
    return {"present": True, "configured": n > 0, "count": n,
            "detail": (f"{n} IMAP account{'s' if n != 1 else ''} connected."
                       if n else "Gmail or any IMAP mailbox — add from "
                       "Settings (the gear, top right)."),
            "action": "" if n else "Add in Settings"}


def _probe_m365(ctx):
    n = sum(1 for a in _mail_accounts() if a.get("type") == "graph")
    return {"present": True, "configured": n > 0, "count": n,
            "detail": (f"{n} Microsoft 365 account{'s' if n != 1 else ''} "
                       "connected — also feeds the work calendar."
                       if n else "Outlook / Exchange Online via a one-time "
                       "device login — connect from Settings."),
            "action": "" if n else "Connect M365 in Settings"}


def _probe_companion(ctx):
    """The Android companion (P2): a pairing-based source, so like the
    CSV row it is "present" everywhere; configured = a phone is paired."""
    from . import companion
    devs = [d for d in companion.devices() if not d.get("pending")]
    n = companion.stats()["messages"]
    if devs:
        names = ", ".join(d.get("name") or d["id"] for d in devs)
        detail = (f"{names} paired — {n:,} message"
                  f"{'s' if n != 1 else ''} received.")
    else:
        detail = ("Pair an Android phone to feed its texts and message "
                  "notifications into Vira (Phone Link window).")
    return {"present": True, "configured": bool(devs), "count": n,
            "detail": detail,
            "action": "" if devs else "Pair in Phone Link"}


def _probe_whatsapp(ctx):
    """WhatsApp (P3): a pairing-based source like the companion. Present
    once the sidecar toolchain is installed; configured = a device link
    exists. Purely filesystem — never calls the sidecar, so the probe can
    never block a Setup render on an HTTP timeout."""
    from . import whatsapp
    linked = whatsapp.linked()
    if linked:
        detail = ("Linked as a WhatsApp device — inbound messages land in "
                  "the feed. Receive-only: Vira never sends.")
    elif whatsapp.installed():
        detail = ("Link Vira as a WhatsApp device to feed inbound messages "
                  "into the feed. Receive-only.")
    else:
        detail = ("Sidecar not installed — run: cd bridge/whatsapp && "
                  "npm install, then link in Settings > WhatsApp.")
    return {"present": whatsapp.installed(), "configured": linked,
            "count": 0, "detail": detail,
            "action": "" if linked else "Connect in Settings > WhatsApp"}


# ---------- the table ----------
# id doubles as the refs.import_source tag the importers stamp, so a row's
# "configured" is readable straight off the imported data. needs_disk marks
# the rows Full Disk Access gates — the step machine derives the FDA step's
# relevance (and the contacts blocker) from it instead of hardcoding
# platform checks. card names the Setup card that renders the row.
SOURCES = {
    "apple-contacts": {
        "label": "Apple Contacts",
        "kind": CONTACTS,
        "platforms": (MAC,),
        "needs_disk": True,
        "card": "apple-contacts",
        "probe": _probe_apple_contacts,
    },
    "google-csv": {
        "label": "Google Contacts",
        "kind": CONTACTS,
        "platforms": ALL_PLATFORMS,
        "needs_disk": False,
        "card": "google-csv",
        "probe": _probe_google_csv,
    },
    "imessage": {
        "label": "iMessage",
        "kind": MESSAGES,
        "platforms": (MAC,),
        "needs_disk": True,
        "card": "imessage",
        "probe": _probe_imessage,
    },
    "apple-calendar": {
        "label": "Apple Calendar",
        "kind": CALENDAR,
        "platforms": (MAC,),
        "needs_disk": True,
        "card": "apple-calendar",
        "probe": _probe_apple_calendar,
    },
    "companion": {
        "label": "Android phone",
        "kind": MESSAGES,
        "platforms": ALL_PLATFORMS,
        "needs_disk": False,
        "card": "companion",
        "probe": _probe_companion,
    },
    "whatsapp": {
        "label": "WhatsApp",
        "kind": MESSAGES,
        "platforms": ALL_PLATFORMS,
        "needs_disk": False,
        "card": "whatsapp",
        "probe": _probe_whatsapp,
    },
    "imap-mail": {
        "label": "Email (IMAP)",
        "kind": MAIL,
        "platforms": ALL_PLATFORMS,
        "needs_disk": False,
        "card": "mail",
        "probe": _probe_imap,
    },
    "m365-mail": {
        "label": "Microsoft 365",
        "kind": MAIL,
        "platforms": ALL_PLATFORMS,
        "needs_disk": False,
        "card": "mail",
        "probe": _probe_m365,
    },
}


def _ctx():
    """Shared probe context, built once per discover() so six probes cost
    one people.json read."""
    return {"people": _people()}


def probe(sid, ctx=None):
    """One source's full record. Never raises."""
    spec = SOURCES.get(sid)
    if not spec:
        return None
    rec = {"id": sid, "label": spec["label"], "kind": spec["kind"],
           "platforms": list(spec["platforms"]),
           "supported": _platform() in spec["platforms"],
           "needs_disk": spec["needs_disk"], "card": spec["card"],
           "present": False, "configured": False, "count": 0,
           "detail": "", "action": ""}
    if not rec["supported"]:
        names = "/".join(_PLAT_NAMES[p] for p in spec["platforms"])
        rec["detail"] = f"{spec['label']} is {names}-only — not on this machine."
        return rec
    try:
        rec.update(spec["probe"](ctx if ctx is not None else _ctx()))
    except Exception as e:  # noqa: BLE001 — a probe must never crash Setup
        rec["detail"] = f"probe error: {str(e)[:120]}"
    return rec


def discover():
    """Every known source, probed. The one registry payload Setup reads."""
    ctx = _ctx()
    return [probe(sid, ctx) for sid in SOURCES]


def available(rows=None):
    """The rows that exist on THIS platform — the Setup fork in one filter."""
    return [r for r in (rows if rows is not None else discover())
            if r["supported"]]


def of_kind(kind, rows):
    return [r for r in rows if r["kind"] == kind]
