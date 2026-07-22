"""Daily Brief: the morning answer to "who and what deserves my attention?"

Replaces the cloud email briefing (dead since 2026-05-21) with a live Vira
window. Everything is deterministic local reads — the macOS Calendar store
(every calendar Calendar.app syncs), the CRM profiles (open loops, tiers,
activity), chat.db (unreplied iMessages), and the mail watcher feed. The
only AI is the optional TL;DR narrative, generated once per day and cached.

A family member's calendars are not synced into the local store by
default; enable their shared calendars under the Google account in
Calendar.app and they appear here automatically. Calendars named in the
`family_calendars` config list get the family tag.
"""
import datetime as dt
import json
import sqlite3
import time
from pathlib import Path

from . import briefstate
from . import channels
from . import data as crm
from . import imessage
from . import journal
from . import settings
from . import suggest
from . import triage

APPLE_EPOCH = 978307200  # 2001-01-01 in unix seconds
CAL_DB = Path.home() / "Library" / "Group Containers" / \
    "group.com.apple.calendar" / "Calendar.sqlitedb"
NARRATIVE_CACHE = Path(__file__).resolve().parent.parent / "data" / \
    "brief-narrative.json"

# noise calendars never shown; birthday calendars get their own section
SKIP_CALENDARS = {"Found in Mail", "Found in Natural Language",
                  "Scheduled Reminders", "US Holidays",
                  "Holidays in United States", "Default"}
BIRTHDAY_CALENDARS = {"Birthdays", "Facebook Birthdays"}
def _family_calendars():
    return set(settings.get("family_calendars"))

QUIET_DAYS = 21          # tier-1/2 contact with no touch this long = going quiet
UNREPLIED_DAYS = 14      # look-back for iMessage threads where you owe a reply
MAIL_FRESH_HOURS = 48    # recent inbound mail window


def _cal_connect():
    # mode=ro reads through the WAL (freshest); immutable is the fallback
    # for environments where the -shm can't be mapped read-only.
    uri = f"file:{CAL_DB}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.OperationalError:
        return sqlite3.connect(uri + "&immutable=1", uri=True, timeout=5)


# Why the store can't be read right now, or None. A missing store is every
# non-Mac install; an unreadable one is a Mac before Full Disk Access. Both
# used to 502 the whole brief — the calendar section must degrade to empty
# with the reason named, because the brief's other sections don't need it.
_cal_error = None


def _day_bounds(offset):
    start = (dt.datetime.now()
             .replace(hour=0, minute=0, second=0, microsecond=0)
             + dt.timedelta(days=offset))
    return start, start + dt.timedelta(days=1)


# Remote/virtual events (owner-curated title substrings; see also the M365
# isOnlineMeeting flag). A remote event does not block a same-time in-person
# commitment, so mixed remote/physical pairs are never flagged as conflicts.
# Deliberately NOT inferred from Meet links in local event descriptions —
# Google auto-attaches Meet links to plainly in-person events (verified on
# this calendar), so the link's presence proves nothing.
def _remote_titles():
    return [t.lower() for t in settings.get("brief_remote_events") or []
            if isinstance(t, str) and t.strip()]


def _is_remote(title, remote_titles):
    t = (title or "").lower()
    return any(x in t for x in remote_titles)


def _occurrences(day_from, day_to):
    """Expanded calendar occurrences between two local datetimes. An
    unreachable store answers [] and records why in _cal_error — never an
    exception (the pre-FDA 502, and every non-Mac install)."""
    global _cal_error
    fams = _family_calendars()
    rts = _remote_titles()
    lo = day_from.timestamp() - APPLE_EPOCH
    hi = day_to.timestamp() - APPLE_EPOCH
    try:
        con = _cal_connect()
    except sqlite3.DatabaseError:
        _cal_error = ("no local calendar store on this machine"
                      if not CAL_DB.exists() else
                      "calendar store unreadable — grant Full Disk Access "
                      "in System Settings > Privacy & Security")
        return []
    try:
        rows = con.execute(
            """SELECT c.title, ci.summary, ci.all_day,
                      COALESCE(oc.occurrence_start_date, oc.occurrence_date),
                      COALESCE(oc.occurrence_end_date, oc.occurrence_date)
               FROM OccurrenceCache oc
               JOIN CalendarItem ci ON oc.event_id = ci.ROWID
               JOIN Calendar c ON ci.calendar_id = c.ROWID
               WHERE COALESCE(oc.occurrence_start_date, oc.occurrence_date) >= ?
                 AND COALESCE(oc.occurrence_start_date, oc.occurrence_date) < ?
               ORDER BY 4""", (lo, hi)).fetchall()
    except sqlite3.DatabaseError as e:
        _cal_error = f"calendar store unreadable: {str(e)[:120]}"
        return []
    finally:
        con.close()
    _cal_error = None
    out, seen = [], set()
    for cal, summary, all_day, start, end in rows:
        if not summary or cal in SKIP_CALENDARS:
            continue
        key = (summary, round(start))
        if key in seen:  # same event mirrored on two synced calendars
            continue
        seen.add(key)
        s = dt.datetime.fromtimestamp(start + APPLE_EPOCH)
        e = dt.datetime.fromtimestamp(end + APPLE_EPOCH)
        out.append({
            "title": summary,
            "calendar": cal,
            "family": cal in fams,
            "birthday": cal in BIRTHDAY_CALENDARS,
            "all_day": bool(all_day),
            "start": s.isoformat(),
            "end": e.isoformat(),
            "start_hm": settings.strf(s, "%-I:%M %p"),
            "end_hm": settings.strf(e, "%-I:%M %p"),
            "conflict": False,
            "remote": _is_remote(summary, rts),
        })
    return out


def _flag_conflicts(events):
    timed = [e for e in events if not e["all_day"] and not e["birthday"]]
    for i, a in enumerate(timed):
        for b in timed[i + 1:]:
            if bool(a.get("remote")) != bool(b.get("remote")):
                continue  # a remote call doesn't block an in-person commitment
            if a["start"] < b["end"] and b["start"] < a["end"]:
                a["conflict"] = b["conflict"] = True
    return events


_m365_cache = {"at": 0, "today": [], "tomorrow": [], "status": None}


def _graph_accounts():
    return [a["email"] for a in channels.graph_accounts()]


def _m365_events():
    """Work-calendar events from Graph, cached 5 minutes. Degrades to a
    status string until the account is reconnected with the calendar scope."""
    now = time.time()
    if now - _m365_cache["at"] < 300:
        return _m365_cache
    from . import msgraph
    _m365_cache.update(at=now, today=[], tomorrow=[], status=None)
    rts = _remote_titles()
    for email in _graph_accounts():
        try:
            for offset, key in ((0, "today"), (1, "tomorrow")):
                lo, hi = _day_bounds(offset)
                for ev in msgraph.calendar_events(
                        email, lo.isoformat(), hi.isoformat()):
                    s, e = ev["start"], ev["end"]
                    _m365_cache[key].append({
                        "title": ev["title"],
                        "calendar": "M365 " + email.split("@")[0],
                        "family": False,
                        "birthday": False,
                        "all_day": ev["all_day"],
                        "start": s,
                        "end": e,
                        "start_hm": settings.strf(
                            dt.datetime.fromisoformat(s),
                            "%-I:%M %p") if s else "",
                        "end_hm": settings.strf(
                            dt.datetime.fromisoformat(e),
                            "%-I:%M %p") if e else "",
                        "conflict": False,
                        "remote": (bool(ev.get("online"))
                                   or _is_remote(ev["title"], rts)),
                        "work": True,
                    })
            _m365_cache["status"] = "ok"
        except Exception as e:  # noqa: BLE001 — surface, never break the brief
            _m365_cache["status"] = str(e)[:160]
    return _m365_cache


def _merge_m365(events, extra):
    seen = {(e["title"], e["start"][:16]) for e in events}
    for ev in extra:
        if (ev["title"], ev["start"][:16]) in seen:
            continue  # mirrored on a synced local calendar already
        events.append(ev)
    events.sort(key=lambda e: e["start"])
    return events


def _calendar():
    m365 = _m365_events()
    today = _flag_conflicts(_merge_m365(
        _occurrences(*_day_bounds(0)), m365["today"]))
    tomorrow = _flag_conflicts(_merge_m365(
        _occurrences(*_day_bounds(1)), m365["tomorrow"]))
    week_start, _ = _day_bounds(0)
    week = _occurrences(week_start, week_start + dt.timedelta(days=7))
    birthdays = [{**e, "date": e["start"][:10]} for e in week if e["birthday"]]
    strip = lambda evs: [e for e in evs if not e["birthday"]]
    return {"today": strip(today), "tomorrow": strip(tomorrow),
            "birthdays": birthdays,
            "available": CAL_DB.exists() and _cal_error is None,
            "error": _cal_error,
            "m365": m365["status"]}


def _is_company(person):
    """Company entities (class_hint "company" — automated senders like a
    bank's notification number) are contacts so their messages resolve to a
    name, but they are never someone the owner "owes a reply" or who is
    "going quiet". NOTE: class_hint "business" means a PERSON in a business
    relationship — only "company" marks a non-person."""
    return (person or {}).get("class_hint") == "company"


def _open_loops():
    c = crm._load()
    out = []
    today = dt.date.today()
    for pid, prof in c["profiles"].items():
        loops = prof.get("open_loops")
        if not isinstance(loops, list):
            continue
        person = c["by_id"].get(pid)
        for lp in loops:
            if not isinstance(lp, dict) or lp.get("status") == "closed":
                continue
            since = lp.get("since") or ""
            try:
                days = (today - dt.date.fromisoformat(since[:10])).days
            except ValueError:
                days = None
            out.append({
                "person_id": pid,
                "person_name": person["name"] if person else prof.get("name", pid),
                "what": lp.get("what", ""),
                "owed_by": lp.get("owed_by", ""),
                "channel": lp.get("channel", ""),
                "since": since,
                "days": days,
            })
    # what the owner owes first, stalest first
    out.sort(key=lambda x: (x["owed_by"] != "me", -(x["days"] or 0)))
    return out[:15]


_imsg_last_cache = {"at": 0, "by_handle": {}}


def _live_imsg_last():
    """handle -> last message ISO date, live from chat.db. The CRM activity
    fields are a snapshot from the last profile refresh and go stale (they
    can show a contact weeks quiet the day they texted), so quietness
    overlays this live read on top of them."""
    now = time.time()
    if now - _imsg_last_cache["at"] < 300:
        return _imsg_last_cache["by_handle"]
    con = imessage._connect()
    try:
        rows = con.execute(
            "SELECT h.id, MAX(m.date) FROM message m "
            "JOIN handle h ON m.handle_id = h.ROWID GROUP BY h.id").fetchall()
    finally:
        con.close()
    by_handle = {}
    for handle, date_ns in rows:
        when = imessage.apple_dt(date_ns)
        if when:
            by_handle[handle] = when.isoformat()
    _imsg_last_cache.update(at=now, by_handle=by_handle)
    return by_handle


def _going_quiet():
    c = crm._load()
    live = _live_imsg_last()
    out = []
    now = dt.datetime.now()
    for p in c["people"]:
        tier = p.get("profile_tier") or p.get("master_tier")
        if tier != "active" or _is_company(p):
            continue
        last = crm._last_contact(p)
        for h in p.get("handles", {}).get("imessage", []):
            last = max(last, live.get(h, ""))
        if not last:
            continue
        try:
            days = (now - dt.datetime.fromisoformat(last[:19])).days
        except ValueError:
            continue
        if days < QUIET_DAYS:
            continue
        out.append({
            "person_id": p["id"],
            "person_name": p["name"],
            "tier": tier,
            "last_contact": last[:10],
            "days": days,
            # re-arms: a fresh exchange mints a new last_contact -> new key
            "dismiss_key": f'quiet:{p["id"]}:{last[:10]}',
        })
    # freshly-quiet first: a 25-day lapse is saveable, a 400-day one is dormant
    out.sort(key=lambda x: x["days"])
    return out[:8]


def _unreplied_imessages():
    """1:1 threads whose latest message is inbound — the owner owes the reply."""
    cutoff_ns = int((time.time() - UNREPLIED_DAYS * 86400 - APPLE_EPOCH) * 1e9)
    con = imessage._connect()
    try:
        rows = con.execute(
            """SELECT h.id, m.date, m.is_from_me, m.text, m.attributedBody
               FROM chat c
               JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
               JOIN message m ON m.ROWID = cmj.message_id
               LEFT JOIN handle h ON m.handle_id = h.ROWID
               WHERE c.style = 45 AND m.item_type = 0
                 AND m.date = (SELECT MAX(m2.date) FROM message m2
                               JOIN chat_message_join c2 ON c2.message_id = m2.ROWID
                               WHERE c2.chat_id = c.ROWID AND m2.item_type = 0)
                 AND m.date > ?
                 AND m.is_from_me = 0""", (cutoff_ns,)).fetchall()
    finally:
        con.close()
    out, seen = [], set()
    for handle, date_ns, _fm, text, blob in rows:
        pid = crm.resolve_handle(handle)
        if not pid or pid in seen:   # unknown senders live in Triage instead
            continue
        seen.add(pid)
        person = crm._load()["by_id"].get(pid)
        if _is_company(person):
            continue
        when = imessage.apple_dt(date_ns)
        preview = imessage.msg_text(text, blob) or ""
        hours = (time.time() - when.timestamp()) / 3600 if when else None
        when_iso = when.isoformat() if when else None
        out.append({
            "person_id": pid,
            "person_name": person["name"] if person else handle,
            "channel": "imessage",
            "preview": preview[:160],
            "when": when_iso,
            "hours": round(hours, 1) if hours is not None else None,
            # re-arms: a newer inbound message mints a new key
            "dismiss_key": f'wait-im:{pid}:{(when_iso or "")[:16]}',
        })
    out.sort(key=lambda x: x["when"] or "", reverse=True)
    return out[:12]


def _recent_mail(feed_items):
    """Inbound email from known CRM people in the last 48h (reply state
    isn't knowable cheaply over IMAP, so this is 'recent', not 'unreplied')."""
    cutoff = (dt.datetime.now().astimezone()
              - dt.timedelta(hours=MAIL_FRESH_HOURS)).isoformat()
    out = []
    for it in reversed(feed_items or []):
        if it.get("channel") != "email" or not it.get("person_id"):
            continue
        if (it.get("when") or "") < cutoff:
            continue
        if _is_company(crm._load()["by_id"].get(it["person_id"])):
            continue
        out.append({
            "person_id": it["person_id"],
            "person_name": it.get("person_name"),
            "channel": "email",
            "preview": (it.get("subject") or it.get("text") or "")[:160],
            "when": it.get("when"),
            "account": it.get("account"),
            "dismiss_key": f'wait-em:{it["person_id"]}:{(it.get("when") or "")[:16]}',
        })
    return out[:8]


_drafts_cache = {"at": 0, "items": [], "status": None}


def _drafts_queued():
    """Ready-to-send drafts sitting in the Graph mailbox(es), cached 5 min.
    Gmail/IMAP drafts aren't listed (a full IMAP folder walk per brief load
    is not worth it); the Graph account is where suggestion drafts land."""
    now = time.time()
    if now - _drafts_cache["at"] < 300:
        return _drafts_cache
    from . import msgraph
    _drafts_cache.update(at=now, items=[], status=None)
    for email in _graph_accounts():
        try:
            _drafts_cache["items"].extend(msgraph.list_drafts(email))
            _drafts_cache["status"] = "ok"
        except Exception as e:  # noqa: BLE001
            _drafts_cache["status"] = str(e)[:160]
    return _drafts_cache


def _triage_summary():
    cands = triage.candidates()
    worthy = [c for c in cands if c.get("contact_worthy") == "yes"]
    return {
        "count": len(cands),
        "contact_worthy": len(worthy),
        "top": [{"handle": c["handle"], "name": c.get("name") or "",
                 "person_id": c.get("person_id"),
                 "evidence": (c.get("evidence") or "")[:120]}
                for c in worthy[:3]],
    }


def _subs_section():
    """Renewals and money — a deterministic slice of the subscriptions
    reconcile: renewals inside 14 days, plus merchants needing attention
    (flags or unresolved evidence). None while the ledger is empty, so the
    brief renders cleanly before the module is configured."""
    try:
        from . import subscriptions
        r = subscriptions.reconcile()
    except Exception:  # noqa: BLE001 — the brief never breaks on a section
        return None
    if not r.get("data_through"):
        return None
    today = dt.date.today()
    out = {"run_rate": r["kpis"]["monthly_run_rate"],
           "annualized": r["kpis"]["annualized"],
           "data_through": r["data_through"],
           "renewals": [], "attention": []}
    for m in r["merchants"]:
        if m["status"] in ("canceled", "ignored") or not m["charges"]:
            continue
        if m["next_renewal"]:
            days = (dt.date.fromisoformat(m["next_renewal"]) - today).days
            if 0 <= days <= 14:
                out["renewals"].append({
                    "merchant": m["display_name"], "in_days": days,
                    "date": m["next_renewal"], "monthly": m["monthly"],
                    "cadence": m["cadence"],
                    "source": m.get("renewal_source")})
        unresolved = len([e for e in m["evidence_needed"]
                          if e["kind"] != "anomaly_explained"])
        flags = [f for f in m["flags"] if f in
                 ("possibly_canceled", "cadence_conflict", "needs_review",
                  "cancel_confirmed", "change_pending", "change_not_applied",
                  "change_unexpected")]
        if flags or unresolved:
            entry = {"merchant": m["display_name"], "flags": flags,
                     "evidence": unresolved}
            pc = m.get("pending_change")
            if pc and pc.get("verification") in ("pending", "failed", "review"):
                entry["change"] = pc["detail"]
            out["attention"].append(entry)
    out["renewals"].sort(key=lambda x: x["in_days"])
    out["attention"] = out["attention"][:8]
    return out


def _not_dismissed(rows):
    gone = briefstate.dismissed_keys()
    return [r for r in rows if r.get("dismiss_key") not in gone]


def _journal_recent():
    """Recent owner notes for the brief UI and the narrative: what the owner
    told Vira lately, with what Vira did about each."""
    out = []
    for e in journal.recent(10):
        out.append({"id": e["id"], "text": e["text"],
                    "person_name": e.get("person_name"),
                    "created": e.get("created"),
                    "status": e.get("status"),
                    "result": e.get("result")})
    return out


def _radar_top():
    """Top of the who-to-talk-to ranking (server/radar.py), for the brief's
    'Who to talk to' section. Lazy import — radar reuses this module's
    loaders."""
    try:
        from . import radar
        return radar.priority_people(limit=3)
    except Exception:  # noqa: BLE001 — the brief never fails on radar
        return []


def compose(feed_items=None):
    now = dt.datetime.now()
    return {
        "generated_at": now.isoformat(),
        "date_label": settings.strf(now, "%A, %B %-d, %Y"),
        "calendar": _calendar(),
        "waiting": {
            "imessage": _not_dismissed(_unreplied_imessages()),
            "email": _not_dismissed(_recent_mail(feed_items)),
        },
        "loops": _open_loops(),
        "quiet": _not_dismissed(_going_quiet()),
        "radar": _radar_top(),
        "drafts": {k: _drafts_queued()[k] for k in ("items", "status")},
        "subs": _subs_section(),
        "triage": _triage_summary(),
        "journal": _journal_recent(),
        "narrative": cached_narrative(),
    }


# ---------- narrative (the one AI touch, cached per day) ----------

NARRATIVE_PROMPT = """You are writing the TL;DR for {owner}'s daily brief.

Below is today's brief data as JSON: calendar (their day and the family's),
iMessages waiting on a reply from them, recent inbound work/personal email,
open relationship loops (owed_by "me" = {owner} owes it), contacts going
quiet, unknown-sender triage counts, and journal — notes {owner} recently
typed into the brief themselves (their own knowledge; treat these as true
and current, they may supersede older data above).

Write 2-4 plain sentences, addressed to {owner}, telling them what actually
matters today: the shape of their day, who is waiting on them, and the one
or two relationship moves worth making. Use only facts present in the data
— never invent names, times, or events. No markdown, no lists, no emojis.

{data}
"""


def cached_narrative():
    try:
        cache = json.loads(NARRATIVE_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if cache.get("date") != dt.date.today().isoformat():
        return None
    return {"text": cache.get("text"), "generated_at": cache.get("at")}


def generate_narrative(feed_items=None, force=False):
    if not force:
        hit = cached_narrative()
        if hit and hit.get("text"):
            return hit
    data = compose(feed_items)
    data.pop("narrative", None)
    slim = json.dumps(data, ensure_ascii=False)[:14000]
    owner = settings.get("owner_name") or "the owner"
    text = suggest.complete(NARRATIVE_PROMPT.format(owner=owner,
                                                    data=slim)).strip()
    entry = {"date": dt.date.today().isoformat(), "text": text,
             "at": dt.datetime.now().isoformat()}
    NARRATIVE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    NARRATIVE_CACHE.write_text(json.dumps(entry, ensure_ascii=False, indent=1))
    return {"text": text, "generated_at": entry["at"]}
