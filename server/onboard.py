"""First-run onboarding: connect real contact data, build first dossiers,
wire the Brain — the stranger's path from fixture demo to their own Vira.

Deterministic movers, one model seam:

- import_apple() / import_google_csv(text) — read what the user already has
  (the local AddressBook stores; a Google Contacts export) and write
  schema-compatible people.json / master.json into the CONFIGURED crm_root,
  created on demand. Importing IS the fixture->real transition: the moment
  people.json lands, settings.fixture_mode() flips off on its own.
- DossierBuilder — background thread that walks the most-active imported
  people (ranked by their real chat.db history), pulls each conversation,
  and asks the configured model backend for a first dossier (relationship
  summary, hooks, open loops), written to profiles/p_*.json. One model call
  per person, same privacy boundary as reply drafting. Skips people who
  already have a profile, so it is resumable and re-runnable.
- vault_setup(path, init) — point vault_root at an existing notes vault, or
  seed a fresh one with the bundled qocha CLI (`qocha init`), then write the
  config so the Brain indexer picks it up without a restart.

Never touches the fixture copy: all writes go to the real crm_root."""
import csv
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import date
from pathlib import Path

from . import data as crm
from . import imessage, secrets, settings, suggest

AB_SOURCES = Path.home() / "Library" / "Application Support" / "AddressBook"
CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"

_lock = threading.Lock()          # people.json / master.json writes
_build_lock = threading.Lock()    # dossier-builder state
_build = {"running": False, "done": 0, "total": 0, "current": "",
          "built": [], "errors": [], "started": None, "finished": None}


def crm_target() -> Path:
    """The configured CRM root — the REAL one, never the fixture redirect."""
    return Path(str(settings.get("crm_root"))).expanduser()


def config_set(**updates):
    """Merge keys into data/config.json (atomic). settings has no setter and
    suggest.save_config filters to the AI keys only — this one is for
    identity/data keys (vault_root, crm_root)."""
    cfg = settings.raw()
    cfg.update(updates)
    settings.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings.CONFIG_PATH.with_name("config.json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(settings.CONFIG_PATH)
    return cfg


# ---------- contact sources ----------

def _addressbook_dbs():
    dbs = sorted(AB_SOURCES.glob("Sources/*/AddressBook-v22.abcddb"))
    root = AB_SOURCES / "AddressBook-v22.abcddb"
    if root.exists():
        dbs.append(root)
    return dbs


def read_apple_contacts(dbs=None):
    """[{name, company, title, emails, phones10}] from the local AddressBook
    sqlite stores. Read-only; merges duplicates across sources by name."""
    merged = {}
    for db in (dbs if dbs is not None else _addressbook_dbs()):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute(
                """SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION,
                          ZJOBTITLE FROM ZABCDRECORD""").fetchall()
            emails = {}
            for owner, addr in con.execute(
                    "SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                if addr:
                    emails.setdefault(owner, []).append(addr.strip().lower())
            phones = {}
            for owner, num in con.execute(
                    "SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                d = crm.norm_digits(num or "")
                if d:
                    phones.setdefault(owner, []).append(d)
            con.close()
        except sqlite3.Error:
            continue
        for pk, first, last, org, title in rows:
            name = " ".join(x.strip() for x in (first, last) if x and x.strip())
            if not name:
                name = (org or "").strip()
            es, ps = emails.get(pk, []), phones.get(pk, [])
            if not name or not (es or ps):
                continue
            key = name.lower()
            c = merged.setdefault(key, {"name": name, "company": "",
                                        "title": "", "emails": [],
                                        "phones10": []})
            c["company"] = c["company"] or (org or "").strip()
            c["title"] = c["title"] or (title or "").strip()
            c["emails"] = sorted(set(c["emails"]) | set(es))
            c["phones10"] = sorted(set(c["phones10"]) | set(ps))
    return list(merged.values())


def _csv_multi(row, pattern):
    """Collect Google-CSV multi-columns ('E-mail 1 - Value', ...), splitting
    the ' ::: ' multi-value packing."""
    out = []
    rx = re.compile(pattern)
    for k, v in row.items():
        if k and v and rx.match(k.strip()):
            out.extend(p.strip() for p in v.split(":::") if p.strip())
    return out


def read_google_csv(text):
    """[{name, company, title, emails, phones10}] from a Google Contacts
    export CSV (both the old Given/Family header set and the 2023+ First/
    Middle/Last one)."""
    text = text.lstrip("﻿")
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        name = (row.get("Name") or "").strip()
        if not name:
            parts = [row.get("First Name") or row.get("Given Name"),
                     row.get("Middle Name") or row.get("Additional Name"),
                     row.get("Last Name") or row.get("Family Name")]
            name = " ".join(p.strip() for p in parts if p and p.strip())
        company = (row.get("Organization Name")
                   or row.get("Organization 1 - Name") or "").strip()
        title = (row.get("Organization Title")
                 or row.get("Organization 1 - Title") or "").strip()
        emails = sorted({e.lower() for e in
                         _csv_multi(row, r"^E-?mail \d+ - Value$")})
        phones = sorted({crm.norm_digits(p) for p in
                         _csv_multi(row, r"^Phone \d+ - Value$")} - {""})
        if not name:
            name = company
        if not name or not (emails or phones):
            continue
        out.append({"name": name, "company": company, "title": title,
                    "emails": emails, "phones10": phones})
    return out


# ---------- the merge into people.json / master.json ----------

def import_contacts(contacts, source):
    """Write contacts into the configured crm_root: new people appended to
    people.json (schema-compatible with triage adds), company/title rows to
    master.json. People whose handles already resolve to a CRM person are
    left untouched (counted as known). Returns counts."""
    root = crm_target()
    with _lock:
        root.mkdir(parents=True, exist_ok=True)
        people_path = root / "people.json"
        master_path = root / "master.json"
        try:
            doc = json.loads(people_path.read_text())
        except (OSError, json.JSONDecodeError):
            doc = {"people": []}
        try:
            master = json.loads(master_path.read_text())
        except (OSError, json.JSONDecodeError):
            master = []
        known = {}
        for p in doc["people"]:
            hs = p.get("handles", {})
            for e in hs.get("emails", []):
                known[e.lower()] = p["id"]
            for ph in hs.get("phones10", []):
                known[ph] = p["id"]
        added = existing = skipped = 0
        today = time.strftime("%Y-%m-%d")
        for c in contacts:
            handles = [*c.get("emails", []), *c.get("phones10", [])]
            if not handles:
                skipped += 1
                continue
            if any(h in known for h in handles):
                existing += 1
                continue
            person = {
                "id": "p_" + uuid.uuid4().hex[:12],
                "name": c["name"],
                "class_hint": None,
                "refs": {"vira_imported": today, "import_source": source},
                "handles": {
                    "imessage": [*c.get("emails", []),
                                 *("+1" + p for p in c.get("phones10", []))],
                    "phones10": c.get("phones10", []),
                    "emails": c.get("emails", []),
                },
                "master_tier": "C-review",
                "activity": {},
            }
            doc["people"].append(person)
            for h in handles:
                known[h] = person["id"]
            if c.get("company") or c.get("title"):
                master.append({"id": person["id"], "full_name": c["name"],
                               "company": c.get("company", ""),
                               "title": c.get("title", ""),
                               "emails": c.get("emails", []),
                               "phones": c.get("phones10", [])})
            added += 1
        for path, payload in ((people_path, doc), (master_path, master)):
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
            tmp.replace(path)
        crm.invalidate()
    return {"added": added, "already_known": existing, "skipped": skipped,
            "total_people": len(doc["people"]), "source": source}


def import_apple():
    dbs = _addressbook_dbs()
    if not dbs:
        raise RuntimeError(
            "no AddressBook stores found — is this Mac signed into Contacts?")
    contacts = read_apple_contacts(dbs)
    if not contacts:
        raise RuntimeError(
            "AddressBook stores found but no readable contacts — grant Full "
            "Disk Access to .venv/bin/python and retry")
    return import_contacts(contacts, "apple-contacts")


def import_google_csv(text):
    contacts = read_google_csv(text)
    if not contacts:
        raise ValueError("no contacts found in that CSV — export from "
                         "Google Contacts > Export > Google CSV")
    return import_contacts(contacts, "google-csv")


# ---------- first dossiers ----------

DOSSIER_PROMPT = """You are building a first CRM dossier for {owner}'s \
private assistant. Below is their recent iMessage history with {name}.

Return STRICT JSON only, no prose:
{{"relationship_class": "<friend|family|colleague|service|other>",
 "relationship_summary": "<2-3 sentences on who this person is to {owner}, \
evidenced by the messages>",
 "comms_style": "<1 sentence on how they talk to each other>",
 "topics": ["<recurring topic>", ...],
 "personal_facts": ["<fact evidenced in the messages>", ...],
 "hooks": [{{"angle": "<a natural conversation opener {owner} could send>", \
"detail": "<why, grounded in the messages>"}}, ...],
 "open_loops": [{{"what": "<an unresolved commitment or question>", \
"owed_by": "<me|them>"}}, ...]}}

Rules: only what the messages evidence — no invention; empty lists are fine.
At most 4 hooks, 4 open_loops, 6 facts. "me" in the transcript is {owner}.

Messages (oldest first):
{thread}
"""


def _msg_counts():
    """pid -> direct-message count, one bulk chat.db query."""
    con = imessage._connect()
    try:
        rows = con.execute(
            """SELECT h.id, COUNT(m.ROWID) FROM message m
               JOIN handle h ON h.ROWID = m.handle_id
               GROUP BY h.id""").fetchall()
    finally:
        con.close()
    counts = {}
    for handle, n in rows:
        pid = crm.resolve_handle(handle)
        if pid:
            counts[pid] = counts.get(pid, 0) + n
    return counts


def _clean_str(v, cap=400):
    return str(v).strip()[:cap] if isinstance(v, (str, int, float)) else ""


def _profile_from(pid, name, parsed, n_msgs):
    hooks = []
    for h in (parsed.get("hooks") or [])[:4]:
        if isinstance(h, dict) and _clean_str(h.get("angle")):
            hooks.append({"angle": _clean_str(h.get("angle")),
                          "detail": _clean_str(h.get("detail")),
                          "grounded_in": "conversation"})
    loops = []
    for l in (parsed.get("open_loops") or [])[:4]:
        if isinstance(l, dict) and _clean_str(l.get("what")):
            loops.append({"what": _clean_str(l.get("what")),
                          "owed_by": "me" if l.get("owed_by") == "me"
                          else "them",
                          "since": date.today().isoformat(),
                          "channel": "imessage", "status": "open"})
    facts = [_clean_str(f) for f in (parsed.get("personal_facts") or [])[:6]
             if _clean_str(f)]
    topics = [_clean_str(t, 60) for t in (parsed.get("topics") or [])[:6]
              if _clean_str(t, 60)]
    return {
        "id": pid, "name": name, "schema_version": 1,
        "relationship_class": _clean_str(parsed.get("relationship_class"), 40)
        or "unknown",
        "relationship_summary": _clean_str(
            parsed.get("relationship_summary"), 800),
        "comms_style": _clean_str(parsed.get("comms_style")),
        "personal_facts": facts, "topics": topics,
        "hooks": hooks, "open_loops": loops,
        "stats": {"messages": n_msgs},
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "generated_by": "vira-onboard",
    }


def _build_one(pid, name, prof_dir, owner):
    thread = imessage.thread_for_person(pid, limit=60)
    if len(thread) < 3:
        return None
    lines = []
    for m in thread:
        who = "me" if m["from_me"] else name
        lines.append(f"{who}: {m['text'][:300]}")
    text = "\n".join(lines)[-8000:]
    raw = suggest.complete(DOSSIER_PROMPT.format(
        owner=owner or "the owner", name=name, thread=text))
    parsed = suggest._extract_json(raw)
    prof = _profile_from(pid, name, parsed, len(thread))
    prof_dir.mkdir(parents=True, exist_ok=True)
    path = prof_dir / f"{pid}.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(prof, indent=1, ensure_ascii=False))
    tmp.replace(path)
    return prof


def _mark_active(pids_counts):
    """Stamp built people active in people.json so the Brief / Radar / Atlas
    light up for them."""
    root = crm_target()
    with _lock:
        try:
            doc = json.loads((root / "people.json").read_text())
        except (OSError, json.JSONDecodeError):
            return
        for p in doc["people"]:
            if p["id"] in pids_counts:
                p["profile_tier"] = "active"
                act = p.setdefault("activity", {})
                act["imsg_n"] = pids_counts[p["id"]]
        path = root / "people.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
        tmp.replace(path)
        crm.invalidate()


def start_dossiers(limit=25):
    """Kick the builder thread. Refused in fixture mode (nothing real to
    build from), under VIRA_PASSIVE (a test copy must not spend the model),
    and while a build is already running."""
    if os.environ.get("VIRA_PASSIVE"):
        raise RuntimeError("passive test instance — dossier builds run on "
                           "the live Vira only")
    if settings.fixture_mode():
        raise RuntimeError("connect your contacts first — dossiers are "
                           "built from your real data")
    with _build_lock:
        if _build["running"]:
            raise RuntimeError("a dossier build is already running")
        _build.update(running=True, done=0, total=0, current="",
                      built=[], errors=[],
                      started=time.strftime("%H:%M:%S"), finished=None)
    t = threading.Thread(target=_run_build, args=(int(limit),),
                         daemon=True, name="dossier-builder")
    t.start()
    return dict(_build)


def _run_build(limit):
    owner = str(settings.get("owner_name") or "")
    root = crm_target()
    prof_dir = root / "profiles"
    try:
        counts = _msg_counts()
        people = crm._load()["by_id"]
        ranked = [(pid, n) for pid, n in
                  sorted(counts.items(), key=lambda kv: -kv[1])
                  if pid in people and not (prof_dir / f"{pid}.json").exists()]
        ranked = ranked[:limit]
        with _build_lock:
            _build["total"] = len(ranked)
        built = {}
        for pid, n in ranked:
            name = people[pid].get("name") or "them"
            with _build_lock:
                _build["current"] = name
            try:
                prof = _build_one(pid, name, prof_dir, owner)
                if prof:
                    built[pid] = n
                    with _build_lock:
                        _build["built"].append(name)
            except Exception as e:  # noqa: BLE001 — skip, keep building
                with _build_lock:
                    _build["errors"].append(f"{name}: {str(e)[:120]}")
            with _build_lock:
                _build["done"] += 1
        if built:
            _mark_active(built)
    finally:
        with _build_lock:
            _build.update(running=False, current="",
                          finished=time.strftime("%H:%M:%S"))


# ---------- the Brain ----------

def _md_count(root, cap=3000):
    n = 0
    try:
        for _ in root.rglob("*.md"):
            n += 1
            if n >= cap:
                break
    except OSError:
        pass
    return n


def vault_setup(path, init=False):
    """Point the Brain at a vault — or seed a new one with qocha init."""
    p = Path(str(path or "").strip()).expanduser()
    if not str(path or "").strip():
        raise ValueError("a vault path is required")
    if init:
        p.mkdir(parents=True, exist_ok=True)
        qocha = Path(sys.executable).with_name("qocha")
        if not qocha.exists():
            raise RuntimeError("the qocha CLI is missing from the venv — "
                               "run .venv/bin/pip install -r requirements.txt")
        r = subprocess.run([str(qocha), "init", str(p)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[-300:])
    elif not p.is_dir():
        raise ValueError(f"{p} is not a directory")
    config_set(vault_root=str(p))
    return {"vault_root": str(p), "initialized": bool(init),
            "notes": _md_count(p)}


# ---------- the one status payload the Setup window reads ----------

def _chatdb_state():
    if not CHAT_DB.exists():
        return "missing"
    try:
        con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        con.execute("SELECT 1 FROM message LIMIT 1").fetchone()
        con.close()
        return "ok"
    except sqlite3.Error:
        return "no-access"


def status():
    root = crm_target()
    people = profiles = 0
    try:
        people = len(json.loads((root / "people.json").read_text())
                     .get("people", []))
    except (OSError, json.JSONDecodeError):
        pass
    if (root / "profiles").is_dir():
        profiles = len(list((root / "profiles").glob("p_*.json")))
    vraw = str(settings.get("vault_root") or "").strip()
    vroot = Path(vraw).expanduser() if vraw else None
    vault_ok = bool(vroot and vroot.is_dir())
    mail_accounts = 0
    try:
        acc = json.loads((settings.ROOT / "data" /
                          "mail-accounts.json").read_text())
        mail_accounts = len(acc if isinstance(acc, list)
                            else acc.get("accounts", []))
    except (OSError, json.JSONDecodeError):
        pass
    with _build_lock:
        build = dict(_build)
    return {
        "fixture_mode": settings.fixture_mode(),
        "crm": {"root": str(root), "people": people, "profiles": profiles},
        "feed": {"chat_db": _chatdb_state()},
        "contacts": {"apple_sources": len(_addressbook_dbs())},
        "vault": {"root": vraw, "connected": vault_ok,
                  "notes": _md_count(vroot) if vault_ok else 0},
        "mail": {"accounts": mail_accounts},
        "dossiers": build,
    }


def set_provider(pid, api_key=None, model=None):
    """Point Vira at a model provider, and file a pasted key in the secrets
    store (Keychain on a Mac, Credential Manager on Windows, locked file
    elsewhere — server/secrets.py).

    Backend follows from what is actually usable: a subscription login uses
    the CLI path, a key-only provider uses the API path. Nothing here spends
    a token — the returned record is a fresh probe so the UI can show the
    real result of the change rather than an optimistic one."""
    from . import models as provider
    if pid not in provider.PROVIDERS:
        raise ValueError(f"unknown provider: {pid}")
    if api_key:
        key = str(api_key).strip()
        if not key:
            raise ValueError("empty API key")
        secrets.set(settings.keychain_service("vira-model-key"), pid, key)
        provider._bin_cache.pop(pid, None)

    rec = provider.probe(pid)
    updates = {"ai_provider": pid,
               "ai_backend": "cli" if rec and rec["auth"] == provider.SIGNED_IN
                             else "api"}
    if model:
        updates["openai_cli_model" if pid == "openai" else "cli_model"] = model
    config_set(**updates)
    return {"provider": rec, "backend": updates["ai_backend"]}


# ---------- the guided-setup step machine ----------

DOSSIER_LIMIT = 25          # matches start_dossiers' default cap

# Ordered because setup is a sequence, and each entry names the ONE module
# it opens when it lands. The order encodes the owner's ruling (2026-07-21):
# the AI comes first because Vira is a harness — every other step hands
# Vira access to your data, this one is what makes it work at all.
STEP_ORDER = (
    ("ai",        "Connect your AI",     None),
    ("disk",      "Full Disk Access",    None),
    ("contacts",  "Import contacts",     "people"),
    ("dossiers",  "Build first dossiers", "brief"),
    ("brain",     "Wire the Brain",      "brain"),
    ("mail",      "Connect mail",        "feed"),
)


def _cost_line(people):
    """What a dossier run will cost, in the terms the connected backend
    actually bills in. A subscription login covers it; a pasted API key
    does not, and silently spending someone's money in their first five
    minutes is how a tool loses trust — so the number is shown BEFORE the
    click, and the build never starts on its own."""
    from . import models as provider
    mode = provider.auth_mode()
    n = min(people, DOSSIER_LIMIT)
    if mode == "subscription":
        return f"Included in your plan — up to {n} dossiers this run."
    if mode == "key":
        try:
            per = float(settings.raw().get("dossier_cost_estimate_usd") or 0.25)
        except (TypeError, ValueError):
            per = 0.25
        return (f"About ${per * n:.2f} on your API key "
                f"— roughly ${per:.2f} per person, up to {n} this run.")
    return "Connect your AI first to build dossiers."


def steps():
    """The wizard's state, DERIVED from the world — never stored.

    That is what makes re-entry free: a half-finished setup recomputes
    exactly where it stopped, with no progress file to keep in sync or to
    go stale against reality. Each step reports its state, the blocker by
    name when it has one, and what completing it unlocks."""
    from . import models as provider
    st = status()
    providers = provider.discover()
    ai_ok = any(p["connected"] for p in providers)
    disk_ok = st["feed"]["chat_db"] == "ok"
    people = st["crm"]["people"]
    build = st["dossiers"]

    def mk(sid, title, opens, state, detail, blocker="", unlocks="", **extra):
        return {"id": sid, "title": title, "state": state, "detail": detail,
                "blocker": blocker, "unlocks": unlocks, "opens": opens,
                **extra}

    out = []
    for sid, title, opens in STEP_ORDER:
        if sid == "ai":
            active = provider.active()
            out.append(mk(
                sid, title, opens,
                "done" if ai_ok else "todo",
                (f"{active['label']} — {active['detail']}" if active
                 else "No model backend connected yet."),
                unlocks="everything Vira writes for you",
                providers=providers,
                sessions=bool(active and active["can"]["sessions"])))
        elif sid == "disk":
            state = {"ok": "done", "no-access": "todo",
                     "missing": "todo"}[st["feed"]["chat_db"]]
            out.append(mk(
                sid, title, opens, state,
                ("Vira can read this Mac's Messages database."
                 if disk_ok else
                 "Grant Full Disk Access to Vira's Python so it can read "
                 "your contacts, messages, and calendar."),
                unlocks="contacts, messages, calendar, search"))
        elif sid == "contacts":
            out.append(mk(
                sid, title, opens,
                "done" if people else ("blocked" if not disk_ok else "todo"),
                (f"{people} {'person' if people == 1 else 'people'} in your CRM."
                 if people else "No contacts imported yet."),
                blocker="" if disk_ok or people else "needs Full Disk Access",
                unlocks="People, Radar, the Visual Network",
                sources=st["contacts"]["apple_sources"]))
        elif sid == "dossiers":
            missing = [n for n, ok in (("AI", ai_ok), ("contacts", bool(people)))
                       if not ok]
            state = ("running" if build.get("running")
                     else "done" if st["crm"]["profiles"]
                     else "blocked" if missing else "todo")
            out.append(mk(
                sid, title, opens, state,
                (f"Building {build.get('done', 0)}/{build.get('total', 0)}"
                 if build.get("running") else
                 f"{st['crm']['profiles']} on file."
                 if st["crm"]["profiles"] else _cost_line(people)),
                blocker=("needs " + " and ".join(missing)) if missing else "",
                unlocks="the Daily Brief, hooks, suggested replies",
                cost=_cost_line(people)))
        elif sid == "brain":
            out.append(mk(
                sid, title, opens,
                "done" if st["vault"]["connected"] else "todo",
                (f"{st['vault']['notes']} notes indexed."
                 if st["vault"]["connected"] else "No vault connected."),
                unlocks="Brain — grounded answers from your own notes"))
        else:  # mail
            out.append(mk(
                sid, title, opens,
                "done" if st["mail"]["accounts"] else "todo",
                (f"{st['mail']['accounts']} account(s) connected."
                 if st["mail"]["accounts"] else "No mail account connected."),
                unlocks="email in Incoming, drafts, receipts"))

    done = sum(1 for s in out if s["state"] == "done")
    return {"steps": out, "done": done, "total": len(out),
            "complete": done == len(out), "fixture_mode": st["fixture_mode"]}
