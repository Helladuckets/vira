"""Vira server: CRM surface + live iMessage feed + reply suggestions +
Claude Code cockpit, served as one mobile-ready web app.

Run: .venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8377
"""
import asyncio
import json
import os
import queue
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (actions, aihealth, applications, atlas, backup, brief,
               briefstate, changelog,
               circuits,
               data as crm,
               designstudio,
               feedstate,
               reading,
               fixtures, ideas, imessage, jobboards, jobfiles, joblog,
               jobtitle, journal,
               judge,
               mail,
               media,
               mediaindex, mercury, modulemap, msgraph, notify, onboard,
               photos, radar,
               receipts,
               routines,
               search as msearch, send, session, settings, subs_visuals,
               subscriptions, suggest, triage, uistate, update, vault)

ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(title="Vira")


# Static assets ship with no Cache-Control by default, so browsers cache
# them heuristically — an open tab (or a revisited RECYCLED test port) can
# run week-old app.js and silently skip new behavior (bit us twice
# 2026-07-16: the live layout seeding, and the :8379 instance clobber).
# no-cache = revalidate every load; StaticFiles' ETag makes that a 304.
@app.middleware("http")
async def _static_no_cache(request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.endswith((".js", ".css", ".html")):
        resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


watcher = imessage.Watcher()
mail_watcher = mail.MailWatcher(watcher)
jobs = actions.Jobs()
indexer = mediaindex.Indexer()
mercury_poller = mercury.Poller()
receipts_sweeper = receipts.Sweeper()
vault_indexer = vault.VaultIndexer()
ai_health_watcher = aihealth.Watcher()
jobboards_poller = jobboards.Poller()


@app.on_event("startup")
async def _startup():
    if os.environ.get("VIRA_PASSIVE"):
        # Passive test instance (scripts/branch.sh serve, run-taurid.sh):
        # UI + API only, over its own data snapshot. No pollers, no
        # schedulers, no job supervisor — a test copy must never act on
        # the world. send.send_imessage carries the matching outbound block.
        print("VIRA_PASSIVE: background workers disabled")
        return
    # Jobs run as DETACHED runner processes that survive server restarts;
    # the supervisor re-attaches to any still running from a prior boot,
    # finalizes dead ones, sweeps the ledger, then polls job dirs for SSE
    # pokes (see server/session.py + server/runner.py).
    session.sessions.start_supervisor()
    watcher.start()
    mail_watcher.start()
    photos.start_background_build()
    indexer.start()
    backup.start()
    mercury_poller.start()
    receipts_sweeper.start()
    # The agentic OS: vault index (the brain), circuit driver (pipelines),
    # routine scheduler (standing loops). All resume from disk state.
    vault_indexer.start()
    circuits.driver.start()
    routines.scheduler.start()
    # The deterministic AI-backend health watcher: probes the model login on a
    # cadence and iMessages the owner on a green->red edge, so a Claude-auth
    # lapse surfaces out-of-band instead of as a silently dead cockpit job.
    ai_health_watcher.start()
    # Job boards: fetch-and-diff the registered career boards on a cadence,
    # iMessage the owner when a new eligible role appears (server/jobboards).
    jobboards_poller.start()
    # Contact Atlas: the materialized graph builds once in the background
    # when no cached view exists yet (refresh is on-demand / weekly after).
    if not atlas.GRAPH.exists():
        atlas.refresh()


# ---------- people ----------

@app.get("/api/people")
def api_people(q: str | None = None, limit: int = 60, sort: str = "recent"):
    people = crm.search_people(q, limit, sort)
    for p in people:
        p["has_photo"] = photos.photo_path(p["id"]) is not None
    return {"people": people}


@app.get("/api/person/{pid}")
def api_person(pid: str):
    detail = crm.get_person(pid)
    if not detail:
        raise HTTPException(404, "unknown person")
    detail["has_photo"] = photos.photo_path(pid) is not None
    return detail


@app.get("/api/person/{pid}/thread")
def api_thread(pid: str, limit: int = 40):
    # clamp: a negative limit flows straight into SQL LIMIT ? and returns
    # the entire message history (audit bounds finding)
    return {"messages": imessage.thread_for_person(pid,
                                                   max(1, min(limit, 500)))}


class HooksReq(BaseModel):
    hooks: list[dict]


@app.put("/api/person/{pid}/hooks")
def api_hooks_set(pid: str, req: HooksReq):
    try:
        prof = crm.save_profile_field(pid, "hooks", req.hooks)
    except KeyError:
        raise HTTPException(404, "unknown person")
    except crm.ProfileCorruptError as e:
        raise HTTPException(409, str(e))
    return {"hooks": prof.get("hooks", [])}


class LoopsReq(BaseModel):
    loops: list[dict]


@app.put("/api/person/{pid}/loops")
def api_loops_set(pid: str, req: LoopsReq):
    try:
        prof = crm.save_profile_field(pid, "open_loops", req.loops)
    except KeyError:
        raise HTTPException(404, "unknown person")
    except crm.ProfileCorruptError as e:
        raise HTTPException(409, str(e))
    return {"open_loops": prof.get("open_loops", [])}


# ---------- server-synced UI state (window layout, dock order) — rides
# the branch.sh data clone so test instances open in the live arrangement
# (see server/uistate.py for the local-wins sync model) ----------

@app.get("/api/ui-state")
def api_ui_state():
    return {**uistate.load(), "instance": uistate.instance_id()}


class UiStateReq(BaseModel):
    keys: dict[str, str]


@app.post("/api/ui-state")
def api_ui_state_save(req: UiStateReq):
    try:
        return uistate.save(req.keys)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------- ideas / on-hold backlog (Vira's cross-session roadmap; the
# source of truth /resume reads and /close-session syncs) ----------

@app.get("/api/ideas")
def api_ideas():
    return {"items": ideas.list_items(), "projects": ideas.list_projects()}


class IdeaAddReq(BaseModel):
    text: str
    status: str | None = "open"
    source: str | None = "manual"
    note: str | None = ""
    project: str | None = None


@app.post("/api/ideas")
def api_ideas_add(req: IdeaAddReq):
    try:
        return ideas.add(req.text, req.status or "open",
                         req.source or "manual", req.note or "",
                         req.project)
    except ValueError as e:
        raise HTTPException(400, str(e))


class IdeaUpdateReq(BaseModel):
    text: str | None = None
    status: str | None = None
    note: str | None = None
    project: str | None = None


@app.put("/api/ideas/{idea_id}")
def api_ideas_update(idea_id: str, req: IdeaUpdateReq):
    try:
        return ideas.update(idea_id, text=req.text, status=req.status,
                            note=req.note, project=req.project)
    except KeyError:
        raise HTTPException(404, "unknown idea")


class ProjectAddReq(BaseModel):
    name: str


@app.post("/api/ideas/projects")
def api_ideas_add_project(req: ProjectAddReq):
    try:
        return {"projects": ideas.add_project(req.name)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/ideas/{idea_id}")
def api_ideas_remove(idea_id: str):
    try:
        return ideas.remove(idea_id)
    except KeyError:
        raise HTTPException(404, "unknown idea")


@app.get("/api/changelog")
def api_changelog():
    return {"groups": changelog.groups()}


# ---------- applications (the job-application front door: fit-scored roles
# from the careers-teardown corpora, owner star/comment/status state, and an
# Apply that dispatches the application-package skill as an agent session
# working in the self-record) ----------

@app.get("/api/applications")
def api_applications(company: str | None = None, view: str = "universe"):
    return applications.compose(company, view)


class AppStateReq(BaseModel):
    starred: bool | None = None
    status: str | None = None
    comment: str | None = None


@app.post("/api/applications/{uid}/state")
def api_applications_state(uid: str, req: AppStateReq):
    try:
        return applications.update_state(uid, starred=req.starred,
                                         status=req.status,
                                         comment=req.comment)
    except ValueError as e:
        raise HTTPException(400, str(e))


class AppApplyReq(BaseModel):
    note: str | None = ""
    model: str | None = None


@app.post("/api/applications/{uid}/apply")
def api_applications_apply(uid: str, req: AppApplyReq):
    role = applications.find_role(uid)
    if role is None:
        raise HTTPException(404, "unknown role")
    prompt = applications.apply_prompt(role, req.note or "")
    try:
        jid = jobs.launch(prompt, str(applications.self_record()),
                          None, req.model, False, None, "interactive")
    except ValueError as e:
        raise HTTPException(429, str(e))
    applications.update_state(uid, job_id=jid)
    return {"job_id": jid}


class AppPromptReq(BaseModel):
    note: str | None = ""


@app.post("/api/applications/{uid}/apply-prompt")
def api_applications_apply_prompt(uid: str, req: AppPromptReq):
    """The composed dispatch prompt without launching anything — for
    copying into a separate session. No job, no state write."""
    role = applications.find_role(uid)
    if role is None:
        raise HTTPException(404, "unknown role")
    return {"prompt": applications.apply_prompt(role, req.note or ""),
            "cwd": str(applications.self_record())}


# ---------- job boards (registry + poller behind the Applications module:
# live board fetch/diff, new-role pings, on-demand refresh, score dispatch)

@app.get("/api/jobboards")
def api_jobboards():
    s = jobboards.status()
    s["poller"] = getattr(jobboards_poller, "status", "not running")
    return s


@app.post("/api/jobboards/refresh")
def api_jobboards_refresh():
    """The on-demand Refresh button: fetch + diff + notify, synchronously."""
    r = jobboards.poll_once()
    jobboards_poller.next_poll = (
        time.time() + float(settings.raw().get("boards_poll_minutes") or 15)
        * 60)
    return r


class BoardAddReq(BaseModel):
    company: str
    ats: str
    slug: str | None = ""
    query: str | None = ""
    location: str | None = ""
    note: str | None = ""


@app.post("/api/jobboards/board")
def api_jobboards_board(req: BoardAddReq):
    try:
        reg = jobboards.add_board(req.company, req.ats, req.slug or "",
                                  req.query or "", req.location or "",
                                  req.note or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return reg


class BoardScoreReq(BaseModel):
    model: str | None = None


@app.post("/api/jobboards/score")
def api_jobboards_score(req: BoardScoreReq):
    """Dispatch an agent session (cwd = the self-record) that deep-reads
    and two-scores the unscored eligible board roles into the universe."""
    prompt, n = jobboards.score_prompt()
    if not n:
        raise HTTPException(400, "nothing unscored — the universe is current")
    try:
        jid = jobs.launch(prompt, str(applications.self_record()),
                          None, req.model, False, None, "interactive")
    except ValueError as e:
        raise HTTPException(429, str(e))
    return {"job_id": jid, "roles": n}


# ---------- the system map (module registry + Modules atlas page) ----------

@app.get("/api/map")
def api_map():
    return modulemap.payload()


@app.post("/api/map/refresh")
def api_map_refresh():
    """Dispatch the map-refresh job now (same prompt the weekly routine
    composes) — watch it in the Jobs window."""
    jid = jobs.launch(modulemap.refresh_prompt(), cwd=str(ROOT),
                      mode="interactive", meta={"kind": "map-refresh"})
    return {"job_id": jid}


# ---------- subscriptions (ledger + renewal radar + launchpad) ----------

@app.get("/api/subs")
def api_subs():
    r = subscriptions.reconcile()
    r["poller"] = mercury_poller.status
    r["receipts"] = receipts_sweeper.status
    return r


@app.post("/api/subs/refresh")
def api_subs_refresh():
    try:
        n = mercury.poll_once()
    except Exception as e:  # noqa: BLE001 — surface poll failures verbatim
        raise HTTPException(502, f"mercury poll failed: {e}")
    r = subscriptions.reconcile()
    r["poller"] = mercury_poller.status
    r["ingested"] = n
    return r


class ReceiptsReq(BaseModel):
    merchant_id: str | None = None


@app.post("/api/subs/receipts")   # MUST precede the /api/subs/{mid} route
def api_subs_receipts(req: ReceiptsReq):
    """Run the receipts pass now — one merchant (card button) or all."""
    try:
        summary = receipts.sweep([req.merchant_id] if req.merchant_id else None)
    except Exception as e:  # noqa: BLE001 — surface sweep failures verbatim
        raise HTTPException(502, f"receipts sweep failed: {e}")
    r = subscriptions.reconcile()
    r["poller"] = mercury_poller.status
    r["receipts"] = receipts_sweeper.status
    r["sweep"] = summary
    return r


class SubsUpdateReq(BaseModel):
    status: str | None = None
    note: str | None = None
    url: str | None = None
    cadence_override: str | None = None   # "" clears the override
    clear_cadence_override: bool = False
    needs_review: bool | None = None
    pending_change: dict | None = None    # recorded cancel/downgrade/price change
    clear_pending_change: bool = False


@app.post("/api/subs/{mid}")
def api_subs_update(mid: str, req: SubsUpdateReq):
    kwargs = {"status": req.status, "note": req.note, "url": req.url,
              "needs_review": req.needs_review}
    if req.clear_cadence_override:
        kwargs["cadence_override"] = None
    elif req.cadence_override:
        kwargs["cadence_override"] = req.cadence_override
    if req.clear_pending_change:
        kwargs["pending_change"] = None
    elif req.pending_change is not None:
        kwargs["pending_change"] = req.pending_change
    try:
        return subscriptions.update_merchant(mid, **kwargs)
    except KeyError:
        raise HTTPException(404, "unknown merchant")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/subs/{mid}/evidence")
def api_subs_evidence(mid: str):
    return subscriptions.merchant_evidence(mid)


@app.get("/api/photo/{pid}")
def api_photo(pid: str):
    p = photos.photo_path(pid)
    if not p:
        raise HTTPException(404, "no photo")
    return FileResponse(p, media_type="image/jpeg")


# ---------- shared media (links / photos / documents, like the Messages
# conversation-info panel) ----------

@app.get("/api/person/{pid}/media")
def api_person_media(pid: str):
    if not crm._load()["by_id"].get(pid):
        raise HTTPException(404, "unknown person")
    if settings.fixture_mode():
        return fixtures.media(pid)
    return media.person_media(pid)


# ---------- semantic search over everything ever shared ----------

@app.get("/api/search")
def api_media_search(q: str | None = None, pid: str | None = None,
                     sender: str | None = None, kind: str | None = None,
                     direction: str | None = None, face: str | None = None,
                     limit: int = 60):
    kinds = [k for k in (kind or "").split(",") if k] or None
    return {"results": msearch.search(
        q=q or None, pid=pid or None, sender_pid=sender or None,
        kind=kinds, direction=direction or None, face_pid=face or None,
        limit=max(1, min(limit, 200)))}


@app.get("/api/search/status")
def api_search_status():
    return mediaindex.status()


class AskBody(BaseModel):
    question: str


@app.post("/api/search/ask")
def api_search_ask(body: AskBody):
    q = body.question.strip()
    if not q:
        raise HTTPException(400, "empty question")
    return msearch.ask(q)


@app.get("/api/search/faces")
def api_search_faces():
    """People with named faces in the index (search-by-face targets)."""
    counts = msearch.face_people()
    c = crm._load()["by_id"]
    return {"people": sorted(
        ({"id": pid, "name": c[pid]["name"], "photos": n}
         for pid, n in counts.items() if pid in c),
        key=lambda x: -x["photos"])}


class TagFaceBody(BaseModel):
    face_id: int
    person_id: str


@app.post("/api/search/tag-face")
def api_tag_face(body: TagFaceBody):
    if not crm._load()["by_id"].get(body.person_id):
        raise HTTPException(404, "unknown person")
    n = mediaindex.tag_face(body.face_id, body.person_id)
    msearch.invalidate()
    return {"rematched": n}


@app.get("/api/media/thumb/{att_id}")
def api_media_thumb(att_id: int):
    p = media.thumbnail(att_id)
    if not p:
        raise HTTPException(404, "no thumbnail")
    return FileResponse(p, media_type="image/jpeg",
                        headers={"cache-control": "max-age=86400"})


@app.get("/api/media/file/{att_id}")
def api_media_file(att_id: int):
    p, mime, name = media.preview_file(att_id)
    if not p:
        raise HTTPException(404, "attachment not on disk")
    return FileResponse(p, media_type=mime, filename=name,
                        content_disposition_type="inline")


@app.get("/api/media/context/{att_id}")
def api_media_context(att_id: int, pid: str,
                      before_rowid: int | None = None,
                      after_rowid: int | None = None,
                      ids: str | None = None):
    # ids-scoped (group) windows don't need a resolvable person — search
    # results open group items with a chat id and no 1:1 owner
    person = crm._load()["by_id"].get(pid)
    if not person and not ids:
        raise HTTPException(404, "unknown person")
    res = media.thread_window(pid, att_id,
                              before_rowid=before_rowid,
                              after_rowid=after_rowid,
                              chat_ids=_parse_chat_ids(ids) if ids else None)
    if not res:
        raise HTTPException(404, "attachment not in this conversation")
    res["person"] = {"id": pid,
                     "name": person["name"] if person else "Group",
                     "has_photo": bool(person)
                     and photos.photo_path(pid) is not None}
    return res


@app.get("/api/favicon")
def api_favicon(domain: str):
    p = media.favicon(domain)
    if not p:
        raise HTTPException(404, "no favicon")
    mt = "image/png" if p.suffix == ".png" else "image/x-icon"
    return FileResponse(p, media_type=mt,
                        headers={"cache-control": "max-age=604800"})


# ---------- feed ----------

@app.get("/api/feed")
def api_feed(limit: int = 50):
    if settings.fixture_mode():
        items = fixtures.feed_items(limit)
        feedstate.annotate(items)
        return {"items": items, "watcher_ok": True, "mail": mail_watcher.status}
    items = watcher.snapshot(limit)
    for it in items:  # photo cache builds in the background; check at read time
        it["has_photo"] = bool(it["person_id"] and photos.photo_path(it["person_id"]))
    feedstate.annotate(items)
    return {"items": items, "watcher_ok": getattr(watcher, "ok", False),
            "mail": mail_watcher.status}


class FeedStateReq(BaseModel):
    rowid: str | int
    read: bool | None = None
    hidden: bool | None = None


@app.post("/api/feed/state")
def api_feed_state(req: FeedStateReq):
    return feedstate.set_state(req.rowid, req.read, req.hidden)


class ReadAllReq(BaseModel):
    rowids: list[str | int]


@app.post("/api/feed/read-all")
def api_feed_read_all(req: ReadAllReq):
    return feedstate.read_all(req.rowids)


@app.get("/api/brief")
def api_brief():
    try:
        return brief.compose(watcher.snapshot(200))
    except Exception as e:  # noqa: BLE001 — surface the failure to the UI
        raise HTTPException(502, str(e)[:400])


@app.post("/api/brief/narrative")
def api_brief_narrative(force: bool = False):
    try:
        return brief.generate_narrative(watcher.snapshot(200), force=force)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e)[:400])


class BriefLoopReq(BaseModel):
    person_id: str
    what: str
    action: str            # "close" | "edit"
    new_what: str | None = None


@app.post("/api/brief/loop")
def api_brief_loop(req: BriefLoopReq):
    """Targeted loop action straight from a brief (or profile) row — no
    whole-array PUT, no opening the person page."""
    try:
        loop = crm.update_loop(req.person_id, req.what, req.action,
                               req.new_what)
    except KeyError:
        raise HTTPException(404, "unknown person")
    except crm.ProfileCorruptError as e:
        raise HTTPException(409, str(e))
    except LookupError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"loop": loop}


class BriefDismissReq(BaseModel):
    key: str
    restore: bool = False


@app.post("/api/brief/dismiss")
def api_brief_dismiss(req: BriefDismissReq):
    try:
        if req.restore:
            briefstate.restore(req.key)
        else:
            briefstate.dismiss(req.key)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True}


class ReadingDoneReq(BaseModel):
    id: str | None = None
    done: bool = True
    merge: list[str] | None = None


@app.get("/api/reading/pages")
def api_reading_pages():
    """Personal reading-room pages on disk; the Reader launcher's list."""
    return {"pages": reading.list_pages()}


@app.get("/api/reading/{name}/done")
def api_reading_done_get(name: str):
    try:
        return {"done": reading.get_done(name)}
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/api/reading/{name}/done")
def api_reading_done_set(name: str, req: ReadingDoneReq):
    """Toggle one done-mark ({id, done}) or union-merge a legacy
    localStorage set ({merge: [ids]}). Returns the authoritative list."""
    try:
        if req.merge is not None:
            return {"done": reading.merge_done(name, req.merge)}
        return {"done": reading.set_done(name, req.id, req.done)}
    except ValueError as e:
        raise HTTPException(422, str(e))


class BriefNoteReq(BaseModel):
    text: str
    person_id: str | None = None
    context: str | None = None


@app.post("/api/brief/note")
def api_brief_note(req: BriefNoteReq):
    """Owner knowledge typed into the brief: saved to the journal instantly,
    integrated into the CRM by a background pass (see server/journal.py)."""
    try:
        entry = journal.add(req.text, req.person_id, context=req.context)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except KeyError:
        raise HTTPException(404, "unknown person")
    return {"entry": entry}


@app.get("/api/brief/journal/export")
def api_brief_journal_export():
    """Every unapplied instruction across the journal, encoded as one
    copy-paste prompt for a full-access Claude session."""
    return journal.export_prompt()


@app.get("/api/brief/journal")
def api_brief_journal(limit: int = 12):
    # the brief bar polls a just-saved note (default 12); the Journal window
    # asks for the full history — clamp to the store's retention ceiling.
    limit = max(1, min(limit, journal.MAX_ENTRIES))
    return {"entries": journal.recent(limit)}


@app.get("/api/person/{pid}/groups")
def api_groups(pid: str):
    groups = imessage.groups_for_person(pid)
    counts = media.counts_for_chats(
        [cid for g in groups for cid in g["chat_ids"]])
    for g in groups:
        tot = {"photos": 0, "links": 0, "docs": 0}
        for cid in g["chat_ids"]:
            for k in tot:
                tot[k] += counts.get(cid, {}).get(k, 0)
        g["media"] = tot
    return {"groups": groups}


def _parse_chat_ids(ids: str):
    try:
        chat_ids = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "ids must be comma-separated integers")
    if not chat_ids:
        raise HTTPException(400, "no chat ids")
    return chat_ids[:60]


@app.get("/api/group/media")
def api_group_media(ids: str):
    return media.media_for_chats(_parse_chat_ids(ids))


@app.get("/api/group/thread")
def api_group_thread(ids: str, limit: int = 60, before: int | None = None):
    try:
        chat_ids = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "ids must be comma-separated integers")
    if not chat_ids:
        raise HTTPException(400, "no chat ids")
    return {"messages": imessage.group_thread(chat_ids[:60],
                                              max(1, min(limit, 500)),
                                              before)}


@app.get("/api/stream")
async def api_stream():
    # One SSE channel, two producers: the iMessage watcher (unnamed `data:`
    # frames, consumed by the feed's onmessage) and the live-session registry
    # (`event: session` frames — permission requests, transcript pokes,
    # status changes — consumed by the session panel; named events don't
    # reach onmessage, so the feed handler is untouched).
    q: queue.Queue = queue.Queue()
    watcher.subscribe(q)
    session.sessions.subscribe(q)

    async def gen():
        ticks = 0
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    while True:  # drain bursts fully each tick
                        item = q.get_nowait()
                        if isinstance(item, dict) and item.get("_sse") == "session":
                            payload = {k: v for k, v in item.items()
                                       if k != "_sse"}
                            yield ("event: session\ndata: "
                                   f"{json.dumps(payload)}\n\n")
                        else:
                            yield f"data: {json.dumps(item)}\n\n"
                except queue.Empty:
                    pass
                await asyncio.sleep(0.25)
                ticks += 1
                if ticks % 20 == 0:
                    yield ": keepalive\n\n"
        finally:
            watcher.unsubscribe(q)
            session.sessions.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------- suggestions ----------

class SuggestReq(BaseModel):
    person_id: str
    channel: str = "imessage"
    extra: str = ""
    mode: str = "replies"


@app.post("/api/suggest")
def api_suggest(req: SuggestReq):
    try:
        return suggest.suggest(req.person_id, req.channel, req.extra, req.mode)
    except KeyError:
        raise HTTPException(404, "unknown person")
    except Exception as e:  # noqa: BLE001 — surface the failure to the UI
        raise HTTPException(502, str(e)[:500])


# ---------- send ----------

class SendReq(BaseModel):
    person_id: str | None = None
    handle: str | None = None
    text: str


@app.post("/api/send")
def api_send(req: SendReq):
    try:
        used = send.send_imessage(req.text, req.person_id, req.handle)
        return {"sent": True, "handle": used}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001 — surface Messages/permission errors
        raise HTTPException(502, str(e)[:500])


# ---------- mail: M365 connect + drafts ----------

class GraphStartReq(BaseModel):
    email: str


@app.post("/api/mail/graph/start")
def api_graph_start(req: GraphStartReq):
    try:
        return msgraph.start_device_flow(req.email.strip().lower())
    except Exception as e:  # noqa: BLE001 — surface the failure to the UI
        raise HTTPException(502, str(e)[:400])


@app.get("/api/mail/graph/status")
def api_graph_status(email: str):
    return msgraph.flow_status(email.strip().lower())


class DraftReq(BaseModel):
    to: str
    subject: str = ""
    body: str
    account: str | None = None
    in_reply_to: str | None = None
    references: str | None = None


@app.post("/api/mail/draft")
def api_mail_draft(req: DraftReq):
    try:
        return mail.create_draft(req.account, req.to, req.subject, req.body,
                                 req.in_reply_to, req.references)
    except Exception as e:  # noqa: BLE001 — surface IMAP/Graph errors
        raise HTTPException(502, str(e)[:500])


# ---------- unknown-sender triage ----------

@app.get("/api/triage")
def api_triage():
    return {"candidates": triage.candidates()}


@app.get("/api/triage/lookup")
def api_triage_lookup(handle: str):
    return {"verdict": triage.verdict_for(handle)}


class DismissReq(BaseModel):
    handle: str


@app.post("/api/triage/dismiss")
def api_triage_dismiss(req: DismissReq):
    return triage.dismiss(req.handle)


class AddPersonReq(BaseModel):
    name: str
    handles: list[str] = []
    class_hint: str | None = None
    note: str | None = None
    person_id: str | None = None  # set = rename an existing placeholder entry


@app.post("/api/crm/add")
def api_crm_add(req: AddPersonReq):
    try:
        return {"added": True,
                "person": triage.add_person(req.name, req.handles,
                                            req.class_hint, req.note,
                                            req.person_id)}
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------- onboarding (Setup window: importers, dossiers, the Brain) ----


class OnboardCsvReq(BaseModel):
    csv: str


class OnboardDossiersReq(BaseModel):
    limit: int = 25


class OnboardVaultReq(BaseModel):
    path: str
    init: bool = False


@app.get("/api/onboard")
def api_onboard():
    return onboard.status()


@app.post("/api/onboard/apple")
def api_onboard_apple():
    try:
        return onboard.import_apple()
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/onboard/csv")
def api_onboard_csv(req: OnboardCsvReq):
    try:
        return onboard.import_google_csv(req.csv)
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/onboard/dossiers")
def api_onboard_dossiers(req: OnboardDossiersReq):
    try:
        return onboard.start_dossiers(max(1, min(req.limit, 100)))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.post("/api/onboard/vault")
def api_onboard_vault(req: OnboardVaultReq):
    try:
        return onboard.vault_setup(req.path, req.init)
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))


# ---------- notifications (iMessage push on high-value inbound) ----------

@app.get("/api/notify")
def api_notify():
    return {"config": notify.config(), "recent": notify.recent()}


class NotifyCfgReq(BaseModel):
    enabled: bool | None = None
    handle: str | None = None


@app.post("/api/notify/config")
def api_notify_config(req: NotifyCfgReq):
    return notify.save_config(req.model_dump(exclude_none=True))


class NotifyTestReq(BaseModel):
    handle: str | None = None


@app.post("/api/notify/test")
def api_notify_test(req: NotifyTestReq):
    try:
        return notify.send_test(req.handle)
    except Exception as e:  # noqa: BLE001 — surface Messages errors to the UI
        raise HTTPException(502, str(e)[:400])


# ---------- claude cockpit ----------

@app.get("/api/actions")
def api_actions():
    return {"actions": actions.scan_library()}


class RunReq(BaseModel):
    prompt: str
    cwd: str | None = None
    permission_mode: str | None = None
    model: str | None = None
    publish_plan: bool = False
    idea_id: str | None = None
    # "interactive" (gated, steerable) | "autopilot" (bypassPermissions).
    # Absent -> derived from permission_mode, else the config default
    # (session_default_mode, "interactive" out of the box).
    mode: str | None = None


@app.post("/api/actions/run")
def api_run(req: RunReq):
    try:
        jid = jobs.launch(req.prompt, req.cwd, req.permission_mode, req.model,
                          req.publish_plan, req.idea_id, req.mode)
    except ValueError as e:
        raise HTTPException(429, str(e))
    return {"job_id": jid}


def _ensure_names(rows, records=None):
    """Attach `title` (canonical editable name) and `command` (first-command
    line) to job rows for the client. The live list/single snapshots don't
    all carry idea_id/meta, so names come from the fuller ledger record;
    history rows already ARE ledger records. Idea text is resolved once."""
    recs = records
    if recs is None:
        recs = {r["id"]: r for r in joblog.list_records()}
    idea_map = None
    for row in rows:
        rec = recs.get(row.get("id")) or row
        it = None
        if rec.get("idea_id"):
            if idea_map is None:
                idea_map = {x["id"]: x["text"] for x in ideas.list_items()}
            it = idea_map.get(rec["idea_id"])
        row["title"] = jobtitle.name(rec, it)
        row["command"] = rec.get("command") or jobtitle.command(rec, it)
    return rows


@app.get("/api/jobs")
def api_jobs():
    return {"jobs": _ensure_names(jobs.recent())}


@app.get("/api/jobs/history")
def api_jobs_history(limit: int = 100):
    """The durable ledger (data/jobs-log.json), newest-first — every job
    ever launched, with outcome, session id, and transcript path. Feeds the
    Jobs window's History tab."""
    rows = joblog.recent(limit)
    return {"jobs": _ensure_names(rows, {r["id"]: r for r in rows})}


class TitleReq(BaseModel):
    title: str


@app.put("/api/jobs/{jid}/title")
def api_job_set_title(jid: str, req: TitleReq):
    """Rename a job. The title is the job's one canonical name — the
    terminal title bar, the Jobs list, the change log, and the retro all
    read it. An empty string clears the edit back to the derived default."""
    rec = joblog.set_title(jid, req.title)
    if not rec:
        raise HTTPException(404, "unknown job")
    return _ensure_names([{"id": jid}], {jid: rec})[0]


def _job_from_disk(jid):
    """Snapshot for a job no longer in the live registry: the ledger record
    plus the job dir's transcript, shaped like a live snapshot so the same
    terminal renders it (read-only)."""
    r = joblog.get_record(jid)
    if not r:
        return None

    def _epoch(iso):
        if not iso:
            return None
        try:
            return datetime.fromisoformat(iso).timestamp()
        except ValueError:
            return None

    jdir = jobfiles.job_dir(jid)
    output = jobfiles.tail_output(jdir, session.OUTPUT_CAP)
    return {
        "id": r["id"], "prompt": r["prompt"], "cwd": r["cwd"],
        "status": r["status"], "output": output or (r.get("result") or ""),
        "started": _epoch(r.get("started")),
        "finished": _epoch(r.get("finished")),
        "permission_mode": r.get("permission_mode"),
        "model": r.get("model"), "publish_plan": r.get("publish_plan"),
        "idea_id": r.get("idea_id"), "session_id": r.get("session_id", ""),
        "mode": r.get("mode"), "awaiting": None, "live": False,
        "pending": [], "transcript": r.get("transcript", ""),
    }


@app.get("/api/jobs/{jid}")
def api_job(jid: str):
    j = jobs.get(jid) or _job_from_disk(jid)
    if not j:
        raise HTTPException(404, "unknown job")
    return _ensure_names([j])[0]


# ---------- live sessions (steering + permission gating) ----------
# Controls are file-based now: each call appends a command line to the
# job's control.jsonl and the detached runner tails it. Same registry as
# /api/jobs — the id is interchangeable.

class SayReq(BaseModel):
    text: str


@app.post("/api/session/{sid}/say")
async def api_session_say(sid: str, req: SayReq):
    try:
        session.sessions.say(sid, req.text)
    except KeyError:
        raise HTTPException(404, "unknown session")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"queued": True}


class PermissionReq(BaseModel):
    req_id: str
    allow: bool
    scope: str = "once"          # "once" | "session"
    reason: str | None = None    # optional deny reason, fed back to the agent


@app.post("/api/session/{sid}/permission")
async def api_session_permission(sid: str, req: PermissionReq):
    try:
        session.sessions.permission(sid, req.req_id, req.allow,
                                    req.scope, req.reason)
    except KeyError as e:
        raise HTTPException(404, f"unknown session or request: {e}")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"resolved": True}


@app.post("/api/session/{sid}/interrupt")
def api_session_interrupt(sid: str):
    try:
        session.sessions.interrupt(sid)
    except KeyError:
        raise HTTPException(404, "unknown session")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"interrupted": True}


@app.post("/api/session/{sid}/close")
def api_session_close(sid: str):
    try:
        session.sessions.close(sid)
    except KeyError:
        raise HTTPException(404, "unknown session")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"closed": True}


# ---------- the agentic OS: vault / circuits / routines / radar / judge ----

@app.get("/api/vault/status")
def api_vault_status():
    return vault.status()


@app.get("/api/vault/search")
def api_vault_search(q: str, limit: int = 10):
    return {"hits": vault.search(q, limit=max(1, min(limit, 30)))}


@app.get("/api/vault/note")
def api_vault_note(path: str):
    try:
        return {"path": path, "text": vault.note_text(path)}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OSError:
        raise HTTPException(404, "note not found")


class AskReq(BaseModel):
    question: str


@app.post("/api/vault/ask")
def api_vault_ask(req: AskReq):
    q = (req.question or "").strip()
    if not q:
        raise HTTPException(400, "empty question")
    try:
        return vault.ask(q)
    except Exception as e:  # noqa: BLE001 — surface backend failures
        raise HTTPException(502, str(e)[:400])


@app.get("/api/vault/person/{pid}")
def api_vault_person(pid: str):
    detail = crm.get_person(pid)
    if not detail:
        raise HTTPException(404, "unknown person")
    return {"notes": vault.person_notes(detail["person"]["name"])}


@app.get("/api/circuits")
def api_circuits():
    return {"circuits": circuits.list_circuits()}


class CircuitReq(BaseModel):
    id: str | None = None
    name: str
    description: str | None = ""
    stages: list[dict]


@app.post("/api/circuits")
def api_circuits_save(req: CircuitReq):
    try:
        return circuits.save_circuit(req.dict())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/circuits/{cid}")
def api_circuits_delete(cid: str):
    try:
        circuits.delete_circuit(cid)
    except KeyError:
        raise HTTPException(404, "unknown circuit")
    return {"deleted": cid}


class CircuitRunReq(BaseModel):
    input: str
    cwd: str | None = None
    notify: bool = False


@app.post("/api/circuits/{cid}/run")
def api_circuits_run(cid: str, req: CircuitRunReq):
    try:
        return circuits.start_run(cid, req.input, cwd=req.cwd,
                                  notify=req.notify)
    except KeyError:
        raise HTTPException(404, "unknown circuit")
    except ValueError as e:
        raise HTTPException(400, str(e))


def _run_with_result(run):
    """Attach the surfaced final result (last stage's report + built path)
    so the run row can show the outcome without opening a stage terminal."""
    run = dict(run)
    run["result"] = circuits.run_result(run)
    return run


@app.get("/api/circuits/runs")
def api_circuit_runs(limit: int = 40):
    return {"runs": [_run_with_result(r) for r in circuits.list_runs(limit)]}


@app.get("/api/circuits/runs/{rid}")
def api_circuit_run(rid: str):
    run = circuits.get_run(rid)
    if not run:
        raise HTTPException(404, "unknown run")
    return _run_with_result(run)


@app.post("/api/circuits/runs/{rid}/cancel")
def api_circuit_run_cancel(rid: str):
    try:
        return circuits.cancel_run(rid)
    except KeyError:
        raise HTTPException(404, "unknown run")
    except ValueError as e:
        raise HTTPException(409, str(e))


class RevealReq(BaseModel):
    path: str


@app.post("/api/reveal")
def api_reveal(req: RevealReq):
    """Open a circuit's built path in Finder on this Mac. Restricted to
    paths that are an actual circuit-run working directory, so the endpoint
    can only surface folders Vira itself worked in."""
    import subprocess
    raw = (req.path or "").strip()
    if not raw:
        raise HTTPException(400, "no path")
    known = {r.get("cwd") for r in circuits.list_runs(200) if r.get("cwd")}
    target = Path(raw).expanduser()
    if raw not in known and str(target) not in known:
        raise HTTPException(403, "not a known circuit working directory")
    if not target.exists():
        raise HTTPException(404, "path no longer exists")
    args = ["open", str(target)] if target.is_dir() \
        else ["open", "-R", str(target)]
    try:
        subprocess.run(args, check=True, timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            OSError) as e:
        raise HTTPException(500, f"could not open: {e}")
    return {"ok": True, "path": str(target)}


@app.get("/api/routines")
def api_routines():
    return {"routines": routines.list_routines()}


class RoutineReq(BaseModel):
    name: str | None = None
    kind: str | None = None
    prompt: str | None = None
    circuit_id: str | None = None
    model: str | None = None
    mode: str | None = None
    cwd: str | None = None
    description: str | None = None
    every_hours: float | None = None
    daily_at: str | None = None
    enabled: bool | None = None
    notify: bool | None = None


@app.post("/api/routines")
def api_routines_add(req: RoutineReq):
    try:
        return routines.save_routine(req.dict())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/routines/{rid}")
def api_routines_update(rid: str, req: RoutineReq):
    try:
        return routines.save_routine(req.dict(exclude_unset=True), rid=rid)
    except KeyError:
        raise HTTPException(404, "unknown routine")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/routines/{rid}")
def api_routines_delete(rid: str):
    try:
        routines.delete_routine(rid)
    except KeyError:
        raise HTTPException(404, "unknown routine")
    return {"deleted": rid}


@app.post("/api/routines/{rid}/run")
def api_routines_run(rid: str):
    r = routines.get_routine(rid)
    if not r:
        raise HTTPException(404, "unknown routine")
    try:
        return routines.dispatch(r)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/radar")
def api_radar():
    return radar.compose()


@app.post("/api/radar/intros/refresh")
def api_radar_refresh():
    import threading as _t
    _t.Thread(target=radar.refresh_intros, daemon=True,
              name="vira-intros-refresh").start()
    return {"refreshing": True}


class DismissIntroReq(BaseModel):
    key: str
    restore: bool = False


@app.post("/api/radar/dismiss")
def api_radar_dismiss(req: DismissIntroReq):
    radar.dismiss_intro(req.key, restore=req.restore)
    return {"ok": True}


# ---------- contact atlas (the face-graph of interconnection) ----------

@app.get("/api/atlas")
def api_atlas():
    """The cached materialized graph — never rebuilt per request."""
    return atlas.compose()


class AtlasRefreshReq(BaseModel):
    narrate: bool = False


@app.post("/api/atlas/refresh")
def api_atlas_refresh(req: AtlasRefreshReq | None = None):
    atlas.refresh(narrate=bool(req and req.narrate))
    return {"refreshing": True}


@app.get("/api/atlas/path")
def api_atlas_path(a: str, b: str):
    res = atlas.path_between(a, b)
    if res is None:
        raise HTTPException(409, "atlas not built yet")
    return res


@app.get("/api/atlas/node/{pid}")
def api_atlas_node(pid: str):
    detail = atlas.node_detail(pid)
    if not detail:
        raise HTTPException(404, "not in the atlas")
    return detail


class GroupLabelReq(BaseModel):
    label: str


class GroupAssignReq(BaseModel):
    pid: str
    group: str = ""          # gid or derived cid; "" = ungroup


@app.post("/api/atlas/groups")
def api_atlas_group_create(req: GroupLabelReq):
    try:
        return atlas.group_create(req.label)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/atlas/groups/{gid}/rename")
def api_atlas_group_rename(gid: str, req: GroupLabelReq):
    try:
        return atlas.group_rename(gid, req.label)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/atlas/groups/{gid}/dissolve")
def api_atlas_group_dissolve(gid: str):
    try:
        return atlas.group_dissolve(gid)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/atlas/groups/assign")
def api_atlas_group_assign(req: GroupAssignReq):
    try:
        return atlas.group_assign(req.pid, req.group)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/person/{pid}/atlas-groups")
def api_person_atlas_groups(pid: str):
    """The person page's Groups row — current group + the movable set."""
    return atlas.person_groups(pid)


@app.get("/api/atlas/face/{pid}")
def api_atlas_face(pid: str):
    """Best face for a node: AddressBook contact photo, else the
    best-scoring media-index crop (cached). 404 = letter-tile fallback."""
    p = photos.photo_path(pid)
    if not p:
        p = atlas.face_crop(pid)
    if not p:
        raise HTTPException(404, "no face on file")
    return FileResponse(p, media_type="image/jpeg",
                        headers={"cache-control": "max-age=86400"})


class JudgeReq(BaseModel):
    model: str | None = None


@app.post("/api/judge/{jid}")
def api_judge(jid: str, req: JudgeReq | None = None):
    try:
        judge_jid = judge.launch_judge(
            jid, model=(req.model if req else None))
    except KeyError:
        raise HTTPException(404, "unknown job")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"judge_job_id": judge_jid}


class IdeaApproveReq(BaseModel):
    build: bool = False
    cwd: str | None = None


@app.post("/api/ideas/{idea_id}/approve")
def api_idea_approve(idea_id: str, req: IdeaApproveReq):
    """Approve a Vira-proposed idea: proposed -> open; with build=true the
    plan-build-judge circuit dispatches on it immediately (the permissioned
    autonomy loop closing)."""
    try:
        item = ideas.update(idea_id, status="open")
    except KeyError:
        raise HTTPException(404, "unknown idea")
    out = {"idea": item}
    if req.build:
        try:
            run = circuits.start_run(
                "plan-build-judge", item["text"], cwd=req.cwd,
                notify=True, source=f"idea:{idea_id}", idea_id=idea_id)
            ideas.update(idea_id,
                         note=f"approved and building (run {run['id'][:10]})")
            out["run"] = run
        except (KeyError, ValueError) as e:
            raise HTTPException(400, f"approved, but build failed: {e}")
    return out


@app.post("/api/ideas/{idea_id}/decline")
def api_idea_decline(idea_id: str):
    try:
        from datetime import date as _date
        return ideas.update(idea_id, status="dropped",
                            note=f"declined by the owner "
                                 f"{_date.today().isoformat()}")
    except KeyError:
        raise HTTPException(404, "unknown idea")


# ---------- updates (pull + restart when the remote is ahead) ----------

@app.get("/api/update")
def api_update(fetch: bool = False):
    return update.status(fetch=fetch)


@app.post("/api/update/apply")
def api_update_apply():
    try:
        return update.apply()
    except ValueError as e:
        raise HTTPException(409, str(e))
    except Exception as e:  # noqa: BLE001 — surface git errors to the UI
        raise HTTPException(502, str(e)[:400])


# ---------- config ----------

@app.get("/api/config")
def api_config():
    cfg = suggest.config()
    cfg["api_key_present"] = bool(__import__("os").environ.get(cfg["api_key_env"]))
    cfg["owner_name"] = settings.raw().get("owner_name", "")
    cfg["graph_email"] = settings.raw().get("graph_email", "")
    cfg["fixture_mode"] = settings.fixture_mode()
    # Passive test instances (scripts/branch.sh serve) look identical to
    # live in the header — the client renders a TEST badge off this flag.
    cfg["passive"] = bool(os.environ.get("VIRA_PASSIVE"))
    # A sandbox install (scripts/sandbox.sh) is NOT passive — it is a real
    # first boot, just against a fake HOME and a namespaced Keychain. It
    # would otherwise badge itself LIVE, which is exactly the mistake the
    # badge exists to prevent, so it gets its own marker.
    cfg["sandbox"] = bool(os.environ.get("VIRA_SANDBOX"))
    # Deterministic AI-backend health, for the header banner. Compact: the
    # client shows a bar only when state == "red".
    cfg["ai_health"] = aihealth.summary()
    return cfg


# ---------- AI-backend health (the deterministic self-check) ----------

@app.get("/api/health/ai")
def api_health_ai():
    """Latest deterministic health probe + recent state transitions. No model
    call — safe to poll cheaply from the client."""
    return {"latest": aihealth.last_state(), "history": aihealth.history()}


@app.post("/api/health/ai/recheck")
def api_health_recheck():
    """Force a probe now (Settings button). Alerts the owner if it finds red."""
    res = aihealth.probe(write=True)
    aihealth.maybe_alert(res)
    return res


class ConfigReq(BaseModel):
    ai_backend: str | None = None
    cli_model: str | None = None
    api_model: str | None = None


@app.post("/api/config")
def api_config_set(req: ConfigReq):
    return suggest.save_config({k: v for k, v in req.model_dump().items()
                                if v is not None})


# ---------- TC-IL morning picker (subs-visuals: status / files / apply) ----
# Replaces the old bare "/subs-picker" static mount: the router serves only
# the PENDING batch, injects the Submit-to-Vira toolbar into picker.html at
# serve time, and dispatches the headless /subs-visuals-apply job on submit.

subs_visuals.configure(jobs)
app.include_router(subs_visuals.router)

# ---------- Design Studio (the design-foundation repo, served in place) ----
# /design/ = the specimen book; /design/studio.html = the IDE frame. The
# save endpoint (designstudio.router) rewrites theme tokens, commits, and
# pushes in that repo. Dormant when the repo directory is missing.

app.include_router(designstudio.router)
_design_root = designstudio.root()
if _design_root.is_dir():
    app.mount("/design", StaticFiles(directory=_design_root, html=True),
              name="design")

app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
