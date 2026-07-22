"""Native Vira tools for live agent sessions (the deep Vira connection).

A session Vira spawns knows nothing about its parent by default — a child
claude process inherits a prompt, a cwd, and env vars, not Vira's data
plane. This module closes that seam two ways:

- preamble(): appended to the SDK session system prompt (and prefixed to
  the legacy fallback prompt) so every session knows it runs inside Vira,
  what it can reach, and the house rule (never restart the server it
  lives inside).
- sdk_server(): an in-process SDK MCP server named "vira" exposing Vira's
  own data plane — calendar (local Calendar.sqlitedb + M365 Graph), the
  daily brief, CRM dossiers, mailbox search, iMessage threads, and the
  semantic media index — as first-class tools. Tool calls execute inside
  the Vira server process (the SDK routes them in-process; no subprocess,
  no localhost round-trip), so a session answers "do I have a doctor's
  appointment?" from the same code paths the Daily Brief renders.

Read-only by construction, with TWO deliberate exceptions: propose_idea
appends a status="proposed" item to the ideas backlog — a STAGING queue
that the owner must approve before anything runs — and update_module_map
replaces the system-map registry (data/modules.json) through a
server-side validator that schema-checks the payload and refuses
destructive replacements (the write path of the System-map refresh
routine). Every other tool renders text from existing loaders. That
containment is why the tools are auto-allowed in interactive sessions
(no Approve/Deny round-trip) — see session.Session.auto_allow.
"""
import asyncio
import datetime as dt
import email as email_lib
import imaplib
import json
import urllib.parse
from pathlib import Path

from . import brief, data as crm, imessage, mail, msgraph, settings

try:  # same guard as session.py — the app must boot without the SDK
    from claude_agent_sdk import create_sdk_mcp_server, tool
    SDK_AVAILABLE = True
except Exception:  # noqa: BLE001 — any import failure means no native tools
    SDK_AVAILABLE = False

ROOT = Path(__file__).resolve().parent.parent
TEXT_CAP = 12_000        # per-tool-result ceiling; tool output feeds a model
PREVIEW = 160            # per-line body/context preview


# ---------- the session preamble ----------

def preamble(native=True):
    """Context every Vira-spawned session gets about its parent. native=False
    is the legacy --print fallback, where the mcp__vira__* tools don't exist
    (no SDK) and only the HTTP API applies."""
    owner = settings.get("owner_name") or "the owner"
    tools_para = (
        "Native tools: the mcp__vira__* tools answer questions about "
        f"{owner}'s life directly from Vira's data plane — calendar (local "
        "macOS calendars + the M365 work calendar), the daily brief, CRM "
        "dossiers, mail search across connected mailboxes, iMessage "
        "threads, semantic search over everything ever shared in "
        f"iMessage, and {owner}'s knowledge vault (vault_search / "
        "vault_note — thousands of notes on companies, people, decisions; "
        "search it before claiming you don't know something about "
        f"{owner}'s world). list_ideas shows the ideas backlog and "
        "propose_idea STAGES a new idea for the owner's approval. They "
        "ARE your calendar/email/contacts/knowledge access — use them "
        "instead of reporting that no connector is available.\n\n"
        if native else "")
    return (
        f"You are running inside Vira, {owner}'s personal AI chief-of-staff "
        f"web app, as an agent session on {owner}'s Mac.\n\n"
        + tools_para +
        "Vira's HTTP API on http://localhost:8377 serves the same data as "
        "JSON when you need it raw: GET /api/brief (calendar + who's "
        "waiting), /api/people?q=<name>, /api/person/<id>, "
        "/api/search?q=<query>, /api/ideas.\n\n"
        "CRITICAL: you run as a child process INSIDE the Vira server. Never "
        "restart, stop, or kill the Vira server or its launchd service (no "
        "launchctl kickstart/bootout of nyc.durham.vira, no pkill of uvicorn "
        "or python) — that kills you mid-task. If a restart is needed, put "
        "it in your final report for the owner to run.")


# ---------- shared rendering helpers ----------

def _txt(text):
    return {"content": [{"type": "text", "text": text[:TEXT_CAP]}]}


def _hm(iso):
    try:
        return settings.strf(dt.datetime.fromisoformat(iso), "%-I:%M %p")
    except (TypeError, ValueError):
        return ""


def _day_label(iso):
    try:
        return settings.strf(dt.datetime.fromisoformat(iso), "%a %b %-d")
    except (TypeError, ValueError):
        return "undated"


def _event_line(e):
    when = ("all day" if e.get("all_day")
            else f"{e.get('start_hm') or _hm(e.get('start'))}"
                 f"–{e.get('end_hm') or _hm(e.get('end'))}")
    marks = "".join([" [work]" if e.get("work") else "",
                     " [family]" if e.get("family") else "",
                     " [birthday]" if e.get("birthday") else "",
                     " [CONFLICT]" if e.get("conflict") else ""])
    return f"  {when:<18} {e.get('title', '?')}"\
           f"  ({e.get('calendar', '')}){marks}"


def _render_days(events):
    """Group event dicts (brief.py shape) by day, newest-first days last."""
    events = sorted(events, key=lambda e: e.get("start") or "")
    out, day = [], None
    for e in events:
        d = _day_label(e.get("start"))
        if d != day:
            day = d
            out.append(f"\n{day}")
        out.append(_event_line(e))
    return "\n".join(out).strip()


# ---------- calendar ----------

def _calendar_text(days):
    days = max(1, min(int(days or 7), 31))
    start, _ = brief._day_bounds(0)
    end = start + dt.timedelta(days=days)
    notes = []
    events = []
    if getattr(brief, "CAL_DB", Path("/nonexistent")).exists():
        events = brief._occurrences(start, end)
    else:
        notes.append("local calendar store unavailable")
    seen = {(e["title"], e["start"][:16]) for e in events}
    for addr in brief._graph_accounts():
        try:
            for ev in msgraph.calendar_events(
                    addr, start.isoformat(), end.isoformat()):
                key = (ev["title"], (ev["start"] or "")[:16])
                if key in seen:
                    continue  # mirrored on a synced local calendar
                seen.add(key)
                events.append({"title": ev["title"], "start": ev["start"],
                               "end": ev["end"], "all_day": ev["all_day"],
                               "calendar": "M365 " + addr.split("@")[0],
                               "work": True})
        except Exception as e:  # noqa: BLE001 — degrade, never fail the tool
            notes.append(f"M365 calendar ({addr}) unavailable: {str(e)[:120]}")
    head = (f"Calendar, next {days} day(s) "
            f"({settings.strf(start, '%a %b %-d')} to "
            f"{settings.strf(end - dt.timedelta(days=1), '%a %b %-d')}):")
    body = _render_days(events) or "No events found in this range."
    tail = ("\n\nnote: " + "; ".join(notes)) if notes else ""
    return f"{head}\n\n{body}{tail}"


async def _t_calendar(args):
    return _txt(await asyncio.to_thread(_calendar_text, args.get("days")))


# ---------- daily brief ----------

def _brief_text():
    b = brief.compose()
    cal = b.get("calendar", {})
    parts = [f"Daily brief — {b.get('date_label', '')}"]
    for key, label in (("today", "Today"), ("tomorrow", "Tomorrow")):
        evs = cal.get(key) or []
        parts.append(f"\n{label} ({len(evs)} event(s)):")
        parts.append(_render_days(evs) or "  nothing scheduled")
    if cal.get("birthdays"):
        parts.append("\nBirthdays this week: " + "; ".join(
            f"{e.get('title')} ({e.get('date')})" for e in cal["birthdays"]))
    # The remaining sections vary in shape; compact JSON is model-friendly
    # and never drifts from brief.py.
    rest = {k: b.get(k) for k in ("waiting", "loops", "quiet", "drafts",
                                  "subs", "triage")}
    parts.append("\nOther sections (JSON): "
                 + json.dumps(rest, default=str)[:6000])
    return "\n".join(parts)


async def _t_daily_brief(args):  # noqa: ARG001 — SDK handlers take args
    return _txt(await asyncio.to_thread(_brief_text))


# ---------- CRM ----------

def _fmt_item(x):
    if isinstance(x, dict):
        return (x.get("text") or x.get("title") or x.get("summary")
                or json.dumps(x, default=str)[:200])
    return str(x)


def _crm_text(name):
    name = (name or "").strip()
    if not name:
        return "error: name is required"
    matches = crm.search_people(name, limit=5)
    if not matches:
        return f"No CRM match for {name!r}."
    top = matches[0]
    full = crm.get_person(top["id"]) or {}
    m, prof = full.get("master") or {}, full.get("profile") or {}
    lines = [f"{top['name']}  (tier {top.get('tier')}, "
             f"{top.get('relationship_class') or top.get('class_hint') or '?'})"]
    for k in ("full_name", "company", "title", "relationship"):
        if m.get(k):
            lines.append(f"  {k}: {m[k]}")
    act_bits = []
    if top.get("imsg_last"):
        act_bits.append(f"last iMessage {top['imsg_last'][:10]}")
    if top.get("imsg_n"):
        act_bits.append(f"{top['imsg_n']} iMessages")
    if top.get("email_n"):
        act_bits.append(f"{top['email_n']} emails")
    if act_bits:
        lines.append("  activity: " + ", ".join(act_bits))
    for key, label in (("summary", "Profile"), ("hooks", "Hooks"),
                       ("open_loops", "Open loops")):
        v = prof.get(key)
        if not v:
            continue
        if isinstance(v, list):
            lines.append(f"  {label}:")
            lines.extend(f"    - {_fmt_item(x)}" for x in v[:8])
        else:
            lines.append(f"  {label}: {_fmt_item(v)}")
    if len(matches) > 1:
        lines.append("Other matches: "
                     + ", ".join(p["name"] for p in matches[1:]))
    return "\n".join(lines)


async def _t_crm_lookup(args):
    return _txt(await asyncio.to_thread(_crm_text, args.get("name")))


# ---------- mail search ----------

def _accounts():
    try:
        return json.loads((ROOT / "data" / "mail-accounts.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _mail_graph(addr, query, limit):
    # ONE pair of quotes around the whole KQL expression — a quoted term
    # inside an already-quoted $search value is a Graph 400 (receipts.py).
    q = ("/me/messages?$search=" + urllib.parse.quote(f'"{query}"')
         + f"&$top={limit}"
         + "&$select=subject,from,receivedDateTime,bodyPreview")
    out = []
    for h in msgraph._graph_request(addr, q).get("value", [])[:limit]:
        sender = (h.get("from", {}).get("emailAddress", {}) or {})\
            .get("address", "")
        out.append(f"  {(h.get('receivedDateTime') or '')[:10]} · {sender} · "
                   f"{h.get('subject', '')} — "
                   f"{(h.get('bodyPreview') or '')[:PREVIEW]}")
    return out


def _mail_imap(acct, query, limit):
    addr, host = acct.get("email"), acct.get("host", "")
    password = mail.keychain_password(addr)
    if not password:
        return ["  (no keychain password)"]
    con = imaplib.IMAP4_SSL(host)
    out = []
    try:
        con.login(addr, password)
        gmail = "gmail" in host
        con.select('"[Gmail]/All Mail"' if gmail else "INBOX", readonly=True)
        if gmail:
            typ, data_ = con.search(
                None, "X-GM-RAW", f'"{query.replace(chr(34), "")}"')
        else:
            typ, data_ = con.search(None, "TEXT", f'"{query}"')
        ids = data_[0].split() if typ == "OK" and data_ and data_[0] else []
        for uid in reversed(ids[-limit:]):
            typ, msg_data = con.fetch(uid, "(RFC822)")
            if typ != "OK" or not msg_data or msg_data[0] is None:
                continue
            msg = email_lib.message_from_bytes(msg_data[0][1])
            when = email_lib.utils.parsedate_to_datetime(msg.get("Date"))
            out.append(f"  {when.date().isoformat() if when else '?'} · "
                       f"{msg.get('From', '')} · "
                       f"{mail._decode_header(msg.get('Subject'))} — "
                       f"{mail._body_preview(msg, limit=PREVIEW)}")
    finally:
        try:
            con.logout()
        except Exception:  # noqa: BLE001
            pass
    return out


def _mail_text(query, limit):
    query = (query or "").strip()
    if not query:
        return "error: query is required"
    limit = max(1, min(int(limit or 6), 20))
    accounts = _accounts()
    if not accounts:
        return "No mail accounts are connected to Vira."
    parts = []
    for acct in accounts:
        addr = acct.get("email", "?")
        try:
            hits = (_mail_graph(addr, query, limit)
                    if acct.get("type") == "graph"
                    else _mail_imap(acct, query, limit))
            parts.append(f"{addr}:\n"
                         + ("\n".join(hits) if hits else "  no matches"))
        except Exception as e:  # noqa: BLE001 — one account never kills all
            parts.append(f"{addr}: unavailable ({str(e)[:120]})")
    return f"Mail search {query!r}:\n\n" + "\n\n".join(parts)


async def _t_mail_search(args):
    return _txt(await asyncio.to_thread(
        _mail_text, args.get("query"), args.get("limit")))


# ---------- iMessage thread ----------

def _thread_text(name, limit):
    matches = crm.search_people((name or "").strip(), limit=3)
    if not matches:
        return f"No CRM match for {name!r}."
    top = matches[0]
    limit = max(1, min(int(limit or 25), 60))
    msgs = imessage.thread_for_person(top["id"], limit)
    if not msgs:
        return f"No direct iMessage thread with {top['name']}."
    lines = [f"iMessage thread with {top['name']} "
             f"(last {len(msgs)} messages):"]
    for msg in msgs:
        when = msg.get("when")
        stamp = (settings.strf(dt.datetime.fromisoformat(when), "%b %-d %-I:%M %p")
                 if when else "?")
        who = "Me" if msg.get("from_me") else top["name"]
        lines.append(f"  [{stamp}] {who}: {msg.get('text', '')[:300]}")
    return "\n".join(lines)


async def _t_imessage_thread(args):
    return _txt(await asyncio.to_thread(
        _thread_text, args.get("name"), args.get("limit")))


# ---------- semantic media search ----------

def _media_text(query, person, limit):
    from . import search as msearch  # deferred: first call loads models
    query = (query or "").strip()
    if not query:
        return "error: query is required"
    limit = max(1, min(int(limit or 10), 30))
    pid = None
    if person:
        matches = crm.search_people(person.strip(), limit=1)
        if not matches:
            return f"No CRM match for {person!r} to scope the search."
        pid = matches[0]["id"]
    results = msearch.search(q=query, pid=pid, limit=limit)
    if not results:
        return f"No matches for {query!r}."
    lines = [f"Media search {query!r} ({len(results)} hit(s)):"]
    for r in results:
        ctx = r.get("context") or {}
        ctx_txt = f' — "{ctx.get("text", "")[:PREVIEW]}"' if ctx else ""
        lines.append(f"  [{r.get('kind')}] {r.get('name') or r.get('title')}"
                     f" · from {r.get('sender') or '?'}"
                     f" · thread: {r.get('person') or '?'}"
                     f" · {(r.get('when') or '')[:10]}{ctx_txt}")
    return "\n".join(lines)


async def _t_media_search(args):
    return _txt(await asyncio.to_thread(
        _media_text, args.get("query"), args.get("person"),
        args.get("limit")))


# ---------- find: one query over all four databases ----------

def _find_text(query, limit):
    """The agent-facing twin of the Find window. An agent picking between
    four retrieval tools has the same problem the owner had with two
    search boxes — this is the one that sorts for itself."""
    from . import find
    query = (query or "").strip()
    if not query:
        return "error: query is required"
    limit = max(1, min(int(limit or 8), 25))
    out = find.find(query, limit=limit)
    plan = out["plan"]
    head = [f"Find {query!r} — plan: {plan['why'] or 'no filters'}"
            f" (terms: {plan['text'] or '-'})"]
    for db in plan["databases"]:
        g = out["groups"].get(db) or {}
        rows = g.get("rows") or []
        if not rows:
            continue
        head.append(f"{db} ({g.get('count', len(rows))}):")
        for r in rows:
            when = (r.get("when") or "")[:10]
            if db == "notes":
                head.append(f"  {r['path']} · {r.get('heading') or ''}"
                            f" · {when} — {(r.get('snippet') or '')[:PREVIEW]}")
            elif db == "people":
                head.append(f"  {r['name']} ({r['id']})"
                            f" — {(r.get('snippet') or '')[:PREVIEW]}")
            elif db == "messages":
                head.append(f"  [{r.get('source')}] {r.get('sender') or '?'}"
                            f" · {when} — {(r.get('text') or '')[:PREVIEW]}")
            else:
                head.append(f"  [{r.get('kind')}] "
                            f"{r.get('name') or r.get('title')}"
                            f" · from {r.get('sender') or '?'} · {when}")
    return "\n".join(head) if len(head) > 1 else f"No matches for {query!r}."


async def _t_find(args):
    return _txt(await asyncio.to_thread(
        _find_text, args.get("query"), args.get("limit")))


# ---------- the knowledge vault ----------

def _vault_search_text(query, limit):
    from . import vault
    query = (query or "").strip()
    if not query:
        return "error: query is required"
    limit = max(1, min(int(limit or 8), 20))
    hits = vault.search(query, limit=limit)
    if not hits:
        st = vault.status()
        if not st.get("available"):
            return "The knowledge vault is not available on this machine."
        return f"No vault matches for {query!r}."
    lines = [f"Vault search {query!r} ({len(hits)} hit(s)):"]
    for h in hits:
        lines.append(f"\n[{h['path']}] {h['heading']}")
        lines.append("  " + h["text"][:500].replace("\n", "\n  "))
    lines.append("\nUse vault_note with a path above for the full note.")
    return "\n".join(lines)


async def _t_vault_search(args):
    return _txt(await asyncio.to_thread(
        _vault_search_text, args.get("query"), args.get("limit")))


def _vault_note_text(path):
    from . import vault
    try:
        return f"[{path}]\n\n" + vault.note_text((path or "").strip())
    except (ValueError, OSError) as e:
        return f"error: {e}"


async def _t_vault_note(args):
    return _txt(await asyncio.to_thread(_vault_note_text, args.get("path")))


# ---------- the ideas backlog ----------

def _list_ideas_text(status):
    from . import ideas
    items = ideas.list_items()
    status = (status or "").strip().lower()
    if status:
        items = [i for i in items if i["status"] == status]
    if not items:
        return "No ideas match."
    lines = [f"Ideas backlog ({len(items)} item(s)):"]
    for i in items[:60]:
        lines.append(f"  [{i['status']}] ({i.get('project', '?')}) "
                     f"{i['text'][:180]}")
    return "\n".join(lines)


async def _t_list_ideas(args):
    return _txt(await asyncio.to_thread(_list_ideas_text,
                                        args.get("status")))


def _propose_idea_text(text, project, why):
    from . import ideas
    text = (text or "").strip()
    if not text:
        return "error: idea text is required"
    dupes = [i for i in ideas.list_items()
             if i["status"] in ("proposed", "open", "on-hold")
             and i["text"].strip().lower() == text.lower()]
    if dupes:
        return "Not staged — an identical idea is already on the backlog."
    item = ideas.add(text, status="proposed", source="muse",
                     note=(why or "").strip()[:400], project=project)
    return (f"Staged for the owner's approval: [{item['id']}] "
            f"({item['project']}) {item['text'][:160]}")


async def _t_propose_idea(args):
    return _txt(await asyncio.to_thread(
        _propose_idea_text, args.get("text"), args.get("project"),
        args.get("why")))


def _update_module_map_text(modules_json):
    from . import modulemap
    try:
        mods = json.loads(modules_json or "")
    except json.JSONDecodeError as e:
        return f"error: modules_json is not valid JSON ({e})"
    try:
        return modulemap.replace_modules(mods)
    except ValueError as e:
        return f"error: {e}"


async def _t_update_module_map(args):
    return _txt(await asyncio.to_thread(
        _update_module_map_text, args.get("modules_json")))


# ---------- first-run setup writes (server/frontdoor.py) ----------
# Both are dispatched only by a module's front door, and both exist so the
# setup session never touches config or the served page tree by hand.

def _create_reading_room_text(slug, title, subtitle, items_json):
    from . import readingroom
    try:
        items = json.loads(items_json or "")
    except json.JSONDecodeError as e:
        return (f"error: items_json is not valid JSON ({e}). Pass the whole "
                "item array as a single JSON string.")
    try:
        res = readingroom.build(slug, title, subtitle or "", items)
    except readingroom.BuildError as e:
        return f"error: {e}"
    except OSError as e:
        return f"error: could not write the room ({e})"
    return readingroom.summary_line(res)


async def _t_create_reading_room(args):
    return _txt(await asyncio.to_thread(
        _create_reading_room_text, args.get("slug"), args.get("title"),
        args.get("subtitle"), args.get("items_json")))


def _configure_applications_text(config_json):
    from . import frontdoor
    try:
        res = frontdoor.configure_applications(config_json)
    except frontdoor.ConfigError as e:
        return f"error: {e}"
    return frontdoor.configure_summary(res)


async def _t_configure_applications(args):
    return _txt(await asyncio.to_thread(
        _configure_applications_text, args.get("config_json")))


# ---------- the SDK server ----------

# (name, description, input schema, handler). Schemas use the SDK's simple
# name->type form; handlers tolerate missing optional keys.
TOOL_SPECS = [
    ("calendar",
     "The owner's calendar for the next N days: local macOS calendars "
     "(personal + family + birthdays) merged with the M365 work calendar. "
     "Use for any appointment/schedule/availability question.",
     {"days": int}, _t_calendar),
    ("daily_brief",
     "The owner's full daily brief: today/tomorrow calendar, who is "
     "waiting on a reply, open relationship loops, contacts going quiet, "
     "subscription renewals, queued drafts, triage count.",
     {}, _t_daily_brief),
    ("crm_lookup",
     "CRM dossier for a person by name: role, company, relationship, "
     "conversation hooks, open loops, contact activity.",
     {"name": str}, _t_crm_lookup),
    ("mail_search",
     "Search the owner's connected mailboxes (M365 work + personal Gmail) "
     "for messages matching a query. Returns date, sender, subject, "
     "preview.",
     {"query": str, "limit": int}, _t_mail_search),
    ("imessage_thread",
     "Recent direct iMessage conversation with a person by name, both "
     "directions, newest last.",
     {"name": str, "limit": int}, _t_imessage_thread),
    ("find",
     "ONE search over all four of the owner's databases at once: vault "
     "notes, shared media (photos/videos/docs/links), CRM people, and the "
     "text of iMessage and mail. Reads dates, names, 'most recent', "
     "filenames and quoted phrases out of the query and applies them as "
     "filters. Prefer this over the single-corpus tools unless you know "
     "exactly which database holds the answer.",
     {"query": str, "limit": int}, _t_find),
    ("media_search",
     "Semantic search over everything ever shared with the owner in "
     "iMessage (photos, videos, documents, links, voice memos) — by "
     "content, OCR text, captions. Optionally scoped to one person. First "
     "call may take ~15s (model load).",
     {"query": str, "person": str, "limit": int}, _t_media_search),
    ("vault_search",
     "Search the owner's knowledge vault (thousands of Obsidian notes on "
     "companies, deals, people, decisions, sessions). Returns excerpt "
     "chunks with note paths — follow up with vault_note for a full note.",
     {"query": str, "limit": int}, _t_vault_search),
    ("vault_note",
     "Read one full note from the owner's knowledge vault by its path "
     "(as returned by vault_search).",
     {"path": str}, _t_vault_note),
    ("list_ideas",
     "The owner's ideas backlog (cross-project). Optional status filter: "
     "proposed | open | on-hold | done | dropped.",
     {"status": str}, _t_list_ideas),
    ("propose_idea",
     "STAGE a new idea on the owner's backlog as status 'proposed' — it "
     "runs only if the owner approves it. Use for genuinely new, concrete, "
     "buildable ideas; include the project it belongs to and a short "
     "'why now' rationale.",
     {"text": str, "project": str, "why": str}, _t_propose_idea),
    ("update_module_map",
     "Replace Vira's system-map registry (the Modules atlas page's data) "
     "with an updated FULL module list. Pass the complete JSON array as "
     "modules_json — every module, not a diff. Validated server-side: "
     "stable kebab-case ids, layer in source/store/engine/surface, "
     "name+what required; a payload that drops too many existing modules "
     "is refused. Use only when refreshing the system map.",
     {"modules_json": str}, _t_update_module_map),
    ("create_reading_room",
     "Build a reading room — a researched consumption queue — and write "
     "it as a live page in the owner's Reader. Pass the COMPLETE item "
     "array as items_json (a JSON string). Each item: title (required), "
     "url, date YYYY-MM-DD, type, mode watch|listen|read, prio P1|P2|P3, "
     "people [], venue, note, why, status MISSING|PARTIAL|HAVE, vault, "
     "pay. The server validates, dedupes on a stable id, renders the page "
     "and writes it — never write reading-room HTML yourself. Rebuilding "
     "an existing slug is a repass and preserves the owner's done-marks.",
     {"slug": str, "title": str, "subtitle": str, "items_json": str},
     _t_create_reading_room),
    ("configure_applications",
     "Apply first-run setup for the Applications module. Pass config_json "
     "as a JSON string: {record_dir, locations: [str], remote_ok: bool, "
     "boards: [{company, ats, slug, query, location, note}]}. ats is "
     "greenhouse|ashby|lever|microsoft|google|manual. The server creates "
     "the record and universe directories, writes the config keys, "
     "registers every board, and starts the first poll — never edit "
     "data/config.json or the boards registry by hand. An EMPTY locations "
     "list means unfiltered; never guess a city.",
     {"config_json": str}, _t_configure_applications),
]

TOOL_NAMES = [f"mcp__vira__{name}" for name, *_ in TOOL_SPECS]

# The tools on this server that MUTATE. Every other spec renders text from
# an existing loader, which is what makes the whole server auto-allowed in
# interactive sessions (runner.Runner.auto_allow). Read-only sessions —
# judges, circuit read stages — must be denied these, so the list lives
# here beside the tools rather than as a hand-maintained copy in
# session.py that the next write tool would quietly fall out of.
# propose_idea is deliberately absent: it STAGES to a queue the owner must
# approve, which is why it was safe to ship as a read-adjacent tool.
WRITE_TOOLS = {
    "mcp__vira__update_module_map",
    "mcp__vira__create_reading_room",
    "mcp__vira__configure_applications",
}

_server = None


def sdk_server():
    """The in-process MCP server config for ClaudeAgentOptions.mcp_servers,
    or None when the SDK is unavailable (legacy fallback path)."""
    global _server
    if not SDK_AVAILABLE:
        return None
    if _server is None:
        _server = create_sdk_mcp_server(
            name="vira",
            tools=[tool(n, d, s)(h) for n, d, s, h in TOOL_SPECS])
    return _server
