"""Vira server: CRM surface + live iMessage feed + reply suggestions +
Claude Code cockpit, served as one mobile-ready web app.

Run: .venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8377
"""
import asyncio
import json
import mimetypes
import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (actions, admission, agentbackend, aihealth, applecontacts,
               applicationmap, applications,
               atlas,
               backup, brainchat, brief,
               briefstate, changelog,
               circuits,
               companion,
               contactcard,
               crmindex,
               data as crm,
               define,
               designstudio,
               evidence,
               feedstate,
               find,
               flows,
               frontdoor,
               reading,
               readinglist,
               docthumbs,
               readingroom,
               fixtures, groupchat, ideaimages, ideas, ideatags, imessage,
               jobboards,
               jobdesc,
               jobfiles,
               joblog,
               journal,
               judge,
               lessonwatch,
               loopwatch,
               mail,
               mailread,
               media,
               mediaindex, mercury, models, modulemap, modulestory, msgraph,
               notify, onboard,
               orphanwork,
               photos, pickfolder, plans, profilerefresh, radar, reconnect,
               textindex,
               receipts,
               research,
               imageatlasroutes,
               resolver,
               reviewqueue,
               roomvault,
               routines,
               routinesrc,
               search as msearch, secrets, send, sendpref, session, settings,
               genreroutes,
               skins,
               subs_visuals,
               subscriptions, suggest, triage, uistate, update, vault,
               doctags, walkthroughs,
               whatsapp)

ROOT = Path(__file__).resolve().parent.parent

# Python only learned .webmanifest in 3.13, and this ships on 3.10+ (the
# Windows install). Without it StaticFiles hands the PWA manifest out as
# application/octet-stream and Chrome drops it — no install, no icon.
mimetypes.add_type("application/manifest+json", ".webmanifest")

app = FastAPI(title="Vira")


# Static assets ship with no Cache-Control by default, so browsers cache
# them heuristically — an open tab (or a revisited RECYCLED test port) can
# run week-old app.js and silently skip new behavior (bit us twice
# 2026-07-16: the live layout seeding, and the :8379 instance clobber).
# no-cache = revalidate every load; StaticFiles' ETag makes that a 304.
#
# The icon types are on the list for the same reason, learned the hard way
# 2026-07-22: a phone was wearing a months-old favicon as its home-screen
# app icon. Safari had cached the icon heuristically (no Cache-Control, so
# it invents its own freshness window), and iOS copies whatever the browser
# hands it at Add-to-Home-Screen time and then never re-fetches. Ship a new
# icon without this and the tile keeps the stale one.
_REVALIDATE = (".js", ".css", ".html", ".svg", ".png", ".ico", ".webmanifest")


@app.exception_handler(admission.Full)
async def _cpu_gate_full(request: Request, exc: admission.Full):
    """The CPU gate turned a request away. 503 + Retry-After, and a body
    that says WHY — a stall the client cannot name is the failure mode this
    whole change exists to end (see server/admission.py)."""
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "5"},
        content={"error": "server busy", "detail": str(exc),
                 "waited_s": round(exc.waited, 2), "queue_depth": exc.depth,
                 "path": request.url.path})


@app.middleware("http")
async def _static_no_cache(request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.endswith(_REVALIDATE):
        resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


watcher = imessage.Watcher()
mail_watcher = mail.MailWatcher(watcher)
whatsapp_watcher = whatsapp.WhatsAppWatcher(watcher)
# The job registry IS the live-session registry — /api/jobs/{id} and
# /api/session/{id}/* address the same run (the actions.Jobs wrapper was
# deleted 2026-07-21; it delegated verbatim).
jobs = session.sessions
indexer = mediaindex.Indexer()
text_indexer = textindex.Indexer()      # message bodies + CRM vectors
mercury_poller = mercury.Poller()
receipts_sweeper = receipts.Sweeper()
vault_indexer = vault.VaultIndexer()
ai_health_watcher = aihealth.Watcher()
jobboards_poller = jobboards.Poller()
idea_indexer = ideatags.Indexer(                  # backlog tags + vectors
    settings.get("idea_tag_interval_min") or 10)
doc_indexer = doctags.Indexer(                    # document tags for the Reader
    settings.get("doc_tag_interval_min") or 10)
doc_thumb_sweeper = docthumbs.Sweeper()           # rendered faces for the grid


@app.on_event("startup")
async def _startup():
    # Before the passive check on purpose: a test instance stalls the same
    # way a live one does, and the watchdog neither acts on the world nor
    # costs anything while the loop is healthy.
    loopwatch.watcher.start()
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
    whatsapp_watcher.start()   # dormant until a WhatsApp pairing exists
    photos.start_background_build()
    indexer.start()
    text_indexer.start()
    idea_indexer.start()       # keeps the backlog's tags/vectors current
    doc_indexer.start()        # and the Reader's documents, one batch a tick
    doc_thumb_sweeper.start()  # captures document faces for the library grid
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
    # the contact card rides the first load — the profile's top pane must
    # never paint a derived name and then flicker to the owner's own
    detail["card"] = contactcard.compose(pid, detail)
    return detail


class CardReq(BaseModel):
    """An edited contact card. Only what changed needs to be sent; the
    server diffs against the current card to build the change list."""
    fields: dict = {}
    custom: list | None = None
    handles: dict = {}          # handle key -> {rank?, label?}
    added: list = []            # [{kind: email|phone, value: ...}]
    note: str | None = None     # the owner's own words, filed alongside


@app.get("/api/person/{pid}/card")
def api_card(pid: str):
    card = contactcard.compose(pid)
    if not card:
        raise HTTPException(404, "unknown person")
    return card


@app.post("/api/person/{pid}/card")
def api_card_save(pid: str, req: CardReq):
    try:
        out = contactcard.save(pid, req.model_dump())
    except KeyError:
        raise HTTPException(404, "unknown person")
    except ValueError as e:
        raise HTTPException(400, str(e))
    # The Apple spoke: the same edit lands on the Mac's Contacts card (and
    # from there the phone). Best-effort — a failed push never fails the
    # save; the result rides the response so the card can say what happened.
    if applecontacts.enabled():
        out["apple_push"] = applecontacts.push_person(pid)
    return out


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


class ProfileRefreshReq(BaseModel):
    mode: str = "current"        # current | explore


@app.post("/api/person/{pid}/refresh")
def api_profile_refresh(pid: str, req: ProfileRefreshReq):
    """The dossier-description refresh button. current = one model pass
    over what's already held; explore = a live agent session that digs
    (old mail, vault, media) and writes back through the guarded tool."""
    try:
        if req.mode == "explore":
            return profilerefresh.explore(pid)
        return profilerefresh.refresh_current(pid)
    except KeyError:
        raise HTTPException(404, "unknown person")
    except crm.ProfileCorruptError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


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
    """Items carry their derived tags (owner corrections applied) plus the
    vocabulary in use, so the Queue can search, filter and group in the
    browser with no second round-trip — the whole backlog is small enough
    that client-side filtering is both instant and incapable of the silent
    truncation a paged search endpoint invites."""
    with admission.cpu("ideas.list"):
        items = ideas.list_items()
        return {"items": _ideas_out(ideatags.annotate(items)),
                "projects": ideas.list_projects(),
                "project_paths": ideas.project_paths(),
                "vocab": ideatags.vocabulary(items),
                "tag_status": ideatags.status(items)}


def _ideas_out(rows):
    """Fill each idea's image manifest in with the on-disk path the browser
    needs to compose a dispatch prompt. Done here rather than inside
    ideatags.annotate: that is the tag layer, and resolving filesystem paths
    is not its job."""
    for r in rows:
        if r.get("images"):
            r["images"] = ideaimages.images_of(r)
    return rows


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
    tags_add: dict | None = None
    tags_drop: list[str] | None = None


@app.put("/api/ideas/{idea_id}")
def api_ideas_update(idea_id: str, req: IdeaUpdateReq):
    try:
        it = ideas.update(idea_id, text=req.text, status=req.status,
                          note=req.note, project=req.project,
                          tags_add=req.tags_add, tags_drop=req.tags_drop)
    except KeyError:
        raise HTTPException(404, "unknown idea")
    return _ideas_out(ideatags.annotate([it]))[0]


# ----- images attached to an idea (server/ideaimages.py) -----
# A screenshot of the bug, a photo of the thing, a mockup to build from.
# The bytes land under data/idea-images/; the manifest rides the idea, and
# every dispatch prompt carries the absolute path so the agent opens the
# real pixels rather than a description of them.

class IdeaImageReq(BaseModel):
    name: str | None = ""
    data: str = ""            # base64, bare or as a data: URL


@app.post("/api/ideas/{idea_id}/images")
def api_idea_image_add(idea_id: str, req: IdeaImageReq):
    try:
        ideaimages.attach(idea_id, req.name or "", req.data or "")
    except KeyError:
        raise HTTPException(404, "unknown idea")
    except ValueError as e:
        # The message is written for the owner and rendered verbatim.
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(500, f"could not store that image: {e}")
    it = next((i for i in ideas.list_items() if i["id"] == idea_id), None)
    return _ideas_out(ideatags.annotate([it]))[0]


@app.delete("/api/ideas/{idea_id}/images/{img_id}")
def api_idea_image_remove(idea_id: str, img_id: str):
    try:
        ideaimages.detach(idea_id, img_id)
    except KeyError:
        raise HTTPException(404, "unknown idea or image")
    it = next((i for i in ideas.list_items() if i["id"] == idea_id), None)
    return _ideas_out(ideatags.annotate([it]))[0]


@app.post("/api/ideas/{idea_id}/images/{img_id}/reread")
def api_idea_image_reread(idea_id: str, img_id: str):
    """Read the image again — for one attached while Ollama was down, or
    whose OCR came back empty. Synchronous: the owner asked for it and is
    waiting on the answer."""
    try:
        with admission.cpu("ideas.image.read"):
            ideaimages.reread(idea_id, img_id)
    except KeyError:
        raise HTTPException(404, "unknown idea or image")
    it = next((i for i in ideas.list_items() if i["id"] == idea_id), None)
    return _ideas_out(ideatags.annotate([it]))[0]


# nosniff on both readers: these are owner-uploaded bytes served back to a
# browser, and the type is re-sniffed off the file rather than trusted from
# the manifest.
@app.get("/api/ideas/{idea_id}/images/{img_id}/thumb")
def api_idea_image_thumb(idea_id: str, img_id: str):
    # Gated: a cache MISS decodes and resizes a full-size screenshot with
    # Pillow, on a request thread. A Queue holding several cards of fresh
    # 6K screenshots issues that many uncached thumb requests on first
    # paint at once — the GIL-starvation shape admission.py exists for, and
    # it would present as the whole server going dark with the gate idle.
    # A cache hit costs a stat and leaves the slot immediately.
    with admission.cpu("ideas.image.thumb"):
        p = ideaimages.thumbnail(idea_id, img_id)
    if p:
        return FileResponse(p, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400",
                                     "X-Content-Type-Options": "nosniff"})
    # No thumbnail is not an error — it means this machine could not render
    # one (HEIC without a plugin, no sips). Serve the original.
    return api_idea_image_file(idea_id, img_id)


@app.get("/api/ideas/{idea_id}/images/{img_id}")
def api_idea_image_file(idea_id: str, img_id: str):
    p = ideaimages.file_path(idea_id, img_id)
    if not p:
        raise HTTPException(404, "unknown image")
    return FileResponse(p, media_type=ideaimages.mime_of(p),
                        headers={"Cache-Control": "public, max-age=86400",
                                 "X-Content-Type-Options": "nosniff"})


# ----- the derived layer: tags, similarity, and the fold-in question -----

class ReindexReq(BaseModel):
    batches: int | None = 1


@app.post("/api/ideas/reindex")
def api_ideas_reindex(req: ReindexReq):
    """Tag/embed on demand. Bounded by `batches` (one model call each) so
    a click can never turn into an unbounded spend."""
    n = max(0, min(int(req.batches or 1), 20))
    # Out-of-process, exactly like the background tick. A 20-batch reindex
    # is the single heaviest thing the Queue can ask for, and running it on
    # a worker thread put 20 model calls' worth of parsing and scoring
    # inside the server's own interpreter — the click that could freeze the
    # app for everyone (see ideatags.run_pass).
    out = ideatags.run_pass(batches=n)
    with admission.cpu("ideas.reindex.status"):
        items = ideas.list_items()
        out["status"] = ideatags.status(items)
        out["vocab"] = ideatags.vocabulary(items)
    return out


@app.get("/api/ideas/duplicates")
def api_ideas_duplicates(floor: float = ideatags.DUP_FLOOR):
    with admission.cpu("ideas.duplicates"):
        return {"pairs": ideatags.duplicates(floor=floor)}


@app.get("/api/ideas/{idea_id}/related")
def api_ideas_related(idea_id: str, limit: int = 15,
                      floor: float = ideatags.RELATED_FLOOR,
                      include_parked: bool = False):
    items = ideas.list_items()
    target = next((i for i in items if i["id"] == idea_id), None)
    if not target:
        raise HTTPException(404, "unknown idea")
    # The Ollama wait happens BEFORE the gate, the scoring inside it. A
    # request blocked on the network must not sit on a CPU slot.
    ideatags.ensure_vector(target)
    with admission.cpu("ideas.related"):
        return ideatags.related(idea_id, items=items,
                                limit=max(1, min(limit, 100)),
                                floor=floor, include_parked=include_parked,
                                embed=False)


class FoldReq(BaseModel):
    candidates: list[str] = []


@app.post("/api/ideas/{idea_id}/fold-analysis")
def api_ideas_fold(idea_id: str, req: FoldReq):
    """"There are 15 things like this — which belong in this task?" The
    answer is a recommendation with reasons; the owner's checkboxes still
    decide, because widening a dispatch is a scope call."""
    try:
        return ideatags.fold_analysis(idea_id, (req.candidates or [])[:40])
    except KeyError:
        raise HTTPException(404, "unknown idea")
    except Exception as e:  # noqa: BLE001 — a dead backend is a 503, not a 500
        raise HTTPException(503, f"analysis unavailable: {e}")


class ProjectAddReq(BaseModel):
    name: str


@app.post("/api/ideas/projects")
def api_ideas_add_project(req: ProjectAddReq):
    try:
        return {"projects": ideas.add_project(req.name)}
    except ValueError as e:
        raise HTTPException(400, str(e))


class ProjectPathReq(BaseModel):
    name: str
    path: str = ""      # empty clears the connection


@app.post("/api/ideas/projects/path")
def api_ideas_project_path(req: ProjectPathReq):
    try:
        return ideas.set_project_path(req.name, req.path)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/ideas/{idea_id}")
def api_ideas_remove(idea_id: str):
    try:
        out = ideas.remove(idea_id)
    except KeyError:
        raise HTTPException(404, "unknown idea")
    # Its attachments go with it. Purged here rather than inside
    # ideas.remove: ideas.py owns the store and must not import the image
    # module, so the dependency runs one way only.
    ideaimages.purge(idea_id)
    return out


# ----- saved plans (Plan-mode output: vault note + in-app viewer) -----

@app.get("/api/plans")
def api_plans():
    return {"plans": plans.list_plans()}


@app.get("/api/plans/{pid}")
def api_plan(pid: str):
    try:
        return plans.get_plan(pid)
    except KeyError:
        raise HTTPException(404, "unknown plan")


@app.delete("/api/plans/{pid}")
def api_plan_remove(pid: str):
    try:
        return plans.delete_plan(pid)
    except KeyError:
        raise HTTPException(404, "unknown plan")


@app.get("/api/changelog")
def api_changelog():
    return {"groups": changelog.groups()}


# ----- Evidence Ledger (build provenance -> interview case studies) -----

@app.get("/api/evidence")
def api_evidence():
    return {"cases": evidence.list_cases(), "episodes": evidence.mine(),
            "status": evidence.status()}


class EvidenceComposeReq(BaseModel):
    episode: str | None = None
    force: bool = False


@app.post("/api/evidence/compose")
def api_evidence_compose(req: EvidenceComposeReq):
    if req.episode:
        try:
            return evidence.compose_episode(req.episode, force=req.force)
        except KeyError:
            raise HTTPException(404, "unknown episode")
        except ValueError as e:
            raise HTTPException(409, str(e))
    threading.Thread(target=evidence.compose_new, daemon=True,
                     name="evidence-compose-sweep").start()
    return {"started": True}


class EvidenceEditReq(BaseModel):
    title: str | None = None
    problem: str | None = None
    direction: str | None = None
    outcome: str | None = None
    skills: list[str] | None = None
    status: str | None = None


@app.put("/api/evidence/{cid}")
def api_evidence_update(cid: str, req: EvidenceEditReq):
    try:
        return evidence.update_case(cid, req.model_dump())
    except KeyError:
        raise HTTPException(404, "unknown case")


@app.delete("/api/evidence/{cid}")
def api_evidence_delete(cid: str):
    try:
        return evidence.delete_case(cid)
    except KeyError:
        raise HTTPException(404, "unknown case")


@app.get("/api/evidence/export")
def api_evidence_export_all():
    return {"text": evidence.export_approved()}


# ---------- lesson recurrence (the corrections-ledger read-back: how often
# each standing rule has actually been broken — Work > RECORD > Rules) ----

@app.get("/api/lessons")
def api_lessons():
    return lessonwatch.report()


class LessonRefreshReq(BaseModel):
    adjudicate: bool = True


@app.post("/api/lessons/refresh")
def api_lessons_refresh(req: LessonRefreshReq):
    threading.Thread(target=lessonwatch.run_pass,
                     kwargs={"adjudicate": req.adjudicate},
                     daemon=True, name="lesson-recurrence-pass").start()
    return {"started": True}


class LessonDismissReq(BaseModel):
    dismissed: bool = True


@app.post("/api/lessons/{rule_id}/dismiss")
def api_lessons_dismiss(rule_id: str, req: LessonDismissReq):
    try:
        return lessonwatch.set_dismissed(rule_id, req.dismissed)
    except KeyError:
        raise HTTPException(404, "unknown rule")


@app.post("/api/lessons/{rule_id}/propose")
def api_lessons_propose(rule_id: str):
    try:
        return lessonwatch.force_propose(rule_id)
    except KeyError:
        raise HTTPException(404, "unknown rule")
    except ValueError as e:
        raise HTTPException(409, str(e))


class LessonBreakReq(BaseModel):
    rule_id: str
    evidence_id: str
    breaks: bool


@app.post("/api/lessons/break")
def api_lessons_break(req: LessonBreakReq):
    try:
        return lessonwatch.set_break(req.rule_id, req.evidence_id,
                                     req.breaks)
    except KeyError:
        raise HTTPException(404, "unknown rule or evidence")


@app.get("/api/evidence/{cid}/export")
def api_evidence_export_case(cid: str):
    try:
        return {"text": evidence.export_case(cid)}
    except KeyError:
        raise HTTPException(404, "unknown case")


# ---------- applications (the job-application front door: fit-scored roles
# from the careers-teardown corpora, owner star/comment/status state, and an
# Apply that dispatches the application-package skill as an agent session
# working in the self-record) ----------

@app.get("/api/applications")
def api_applications(company: str | None = None, view: str | None = None):
    return applications.compose(company, view)


class AppCompareReq(BaseModel):
    uids: list[str]


@app.post("/api/applications/compare")
def api_applications_compare(req: AppCompareReq):
    try:
        return applications.compare_roles(req.uids)
    except KeyError as e:
        raise HTTPException(404, f"unknown role: {e.args[0]}")
    except ValueError as e:
        raise HTTPException(400, str(e))


class AppStateReq(BaseModel):
    starred: bool | None = None
    status: str | None = None
    comment: str | None = None


class AppMapNoteReq(BaseModel):
    concept_key: str
    lane: str
    text: str = ""


@app.post("/api/applications/{uid}/state")
def api_applications_state(uid: str, req: AppStateReq):
    try:
        return applications.update_state(uid, starred=req.starred,
                                         status=req.status,
                                         comment=req.comment)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/applications/{uid}/description")
def api_applications_description(uid: str, refresh: bool = False):
    """The posting itself, readable in Vira. Read-only and safe on a
    passive instance — see server/jobdesc.py."""
    role = applications.find_role(uid)
    if role is None:
        raise HTTPException(404, "unknown role")
    return jobdesc.describe(role, refresh=refresh)


@app.get("/api/applications/{uid}/evidence-map")
def api_applications_evidence_map(uid: str):
    """Role concepts joined to the current package and canonical self.

    The derived map never promotes a rendering or planning note into a claim
    source; the adjacent note endpoint owns the only map-specific write.
    """
    role = applications.find_role(uid)
    if role is None:
        raise HTTPException(404, "unknown role")
    return applicationmap.build(role)


@app.post("/api/applications/{uid}/evidence-map/note")
def api_applications_evidence_map_note(uid: str, req: AppMapNoteReq):
    role = applications.find_role(uid)
    if role is None:
        raise HTTPException(404, "unknown role")
    try:
        applicationmap.save_note(role, req.concept_key, req.lane, req.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return applicationmap.build(role)


@app.get("/api/applications/{uid}/evidence-map/export")
def api_applications_evidence_map_export(uid: str):
    role = applications.find_role(uid)
    if role is None:
        raise HTTPException(404, "unknown role")
    return applicationmap.export_markdown(role)


@app.get("/api/applications/{uid}/evidence-map/export.md")
def api_applications_evidence_map_download(uid: str):
    role = applications.find_role(uid)
    if role is None:
        raise HTTPException(404, "unknown role")
    out = applicationmap.export_markdown(role)
    return Response(
        out["text"], media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{out["filename"]}"'})


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
                          None, req.model, False, None, "manual")
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


@app.post("/api/jobboards/poll-now")
def api_jobboards_poll_now():
    """Opening the Applications module asks for a sweep if the last one has
    aged out. Returns immediately; the rows update when it lands."""
    return jobboards.arm_if_stale(jobboards_poller)


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
                          None, req.model, False, None, "manual")
    except ValueError as e:
        raise HTTPException(429, str(e))
    return {"job_id": jid, "roles": n}


# ---------- the system map (module registry + Modules atlas page) ----------

@app.get("/api/map")
def api_map():
    return modulemap.payload()


@app.get("/api/module/story/{win_id}")
def api_module_story(win_id: str):
    """The build story behind a window — right-click, "What is this?".
    Registry blurb + every library document tagged to that module."""
    s = modulestory.story(win_id)
    if not s:
        raise HTTPException(404, "no story for that module")
    return s


@app.post("/api/map/refresh")
def api_map_refresh():
    """Dispatch the map-refresh job now (same prompt the weekly routine
    composes) — watch it in the Jobs window."""
    jid = jobs.launch(modulemap.refresh_prompt(), cwd=str(ROOT),
                      mode="manual", meta={"kind": "map-refresh"})
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
    account_email: str | None = None      # login the sub bills to ("" clears)


@app.post("/api/subs/{mid}")
def api_subs_update(mid: str, req: SubsUpdateReq):
    kwargs = {"status": req.status, "note": req.note, "url": req.url,
              "needs_review": req.needs_review,
              "account_email": req.account_email}
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
    # no-cache = revalidate each time (cheap local 304s), so a refreshed
    # contact photo isn't pinned stale by the browser cache
    return FileResponse(p, media_type="image/jpeg",
                        headers={"cache-control": "no-cache"})


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


# ---------- Find: one search over all four databases ----------
# The GET runs rung 1 only (no model, safe to debounce on input); the
# POST is the deliberate ask that spends rung 2. See server/find.py.

@app.get("/api/find")
def api_find(q: str = "", limit: int = 20, db: str | None = None,
             since: str | None = None, until: str | None = None,
             kind: str | None = None, person: str | None = None):
    p = find.plan(q)
    if db in find.DATABASES:          # the UI's per-tab narrowing
        p["databases"] = [db]
        p["primary"] = db
    for key, val in (("since", since), ("until", until),
                     ("person", person)):
        if val:
            p["filters"][key] = val
    if kind:
        p["filters"]["kind"] = [k for k in kind.split(",") if k]
    if any((since, until, kind, person)):
        p["why"] = find._why(p)       # the chip must show what ran
    out = find.run(p, limit=max(1, min(limit, 100)))
    for g in out["groups"].values():
        g.pop("hits", None)           # engine-native payload; server-side
    return out


@app.post("/api/find/ask")
def api_find_ask(body: AskBody):
    q = body.question.strip()
    if not q:
        raise HTTPException(400, "empty question")
    return find.ask(q)


@app.get("/api/find/status")
def api_find_status():
    """One header for four corpora — what is indexed, and what is not."""
    def safe(fn):
        try:
            return fn()
        except Exception as e:      # noqa: BLE001 — a dormant corpus is
            return {"error": str(e)[:120]}      # a state, not a failure

    return {"media": safe(mediaindex.status), "notes": safe(vault.status),
            "people": safe(crmindex.status),
            "messages": safe(textindex.status)}


class FindChatReq(BaseModel):
    question: str
    session_id: str | None = None


@app.get("/api/find/chat")
def api_find_chat():
    """The locally persisted vault conversation, if one has started."""
    return {"session": brainchat.current()}


@app.post("/api/find/chat/new")
def api_find_chat_new():
    return {"session": brainchat.new()}


@app.post("/api/find/chat")
def api_find_chat_ask(body: FindChatReq):
    q = body.question.strip()
    if not q:
        raise HTTPException(400, "empty question")
    try:
        return {"session": brainchat.ask(q, body.session_id)}
    except brainchat.Conflict as e:
        raise HTTPException(409, str(e))
    except Exception as e:  # noqa: BLE001 — surface configured-model failures
        raise HTTPException(502, str(e)[:400])


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
        return {"items": items, "watcher_ok": True, "mail": mail_watcher.status,
                "whatsapp": whatsapp_watcher.status}
    items = watcher.snapshot(limit)
    for it in items:  # photo cache builds in the background; check at read time
        it["has_photo"] = bool(it["person_id"] and photos.photo_path(it["person_id"]))
    feedstate.annotate(items)
    return {"items": items, "watcher_ok": getattr(watcher, "ok", False),
            "mail": mail_watcher.status,
            "whatsapp": whatsapp_watcher.status}


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


@app.get("/api/review")
def api_review():
    """The needs-review picker's payload: every decision waiting on the
    owner, from every registered source. Never 502s on one bad source —
    reviewqueue.items() names the failure in `errors` and serves the rest."""
    return reviewqueue.items()


class ReviewActReq(BaseModel):
    id: str
    action: str            # "approve" | "drop"


@app.post("/api/review/act")
def api_review_act(req: ReviewActReq):
    """Rule on one queued item. The lessons source shells out to lessons.py,
    which owns LESSONS.md — Vira never writes that ledger itself."""
    try:
        return reviewqueue.act(req.id, req.action)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))
    except Exception as e:  # noqa: BLE001 — subprocess/store failure detail
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


# ---------- module front doors (server/frontdoor.py: the path from a
# dormant module to a live one — what a module IS, a demo clip, and the
# interview that sets it up, dispatched as a live session) ----------

@app.get("/api/frontdoor")
def api_frontdoor():
    return frontdoor.state()


class ResumeReq(BaseModel):
    filename: str | None = ""
    content_b64: str


@app.post("/api/frontdoor/resume")
def api_frontdoor_resume(req: ResumeReq):
    """Stage an uploaded resume for the Applications interview."""
    try:
        return frontdoor.stage_resume(req.filename, req.content_b64)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(500, f"could not save the file: {e}")


class FrontDoorSetupReq(BaseModel):
    answers: dict | None = None
    model: str | None = None


@app.post("/api/frontdoor/{module_id}/setup")
def api_frontdoor_setup(module_id: str, req: FrontDoorSetupReq):
    """Dispatch a module's setup as a live agent session. The session
    reports through the normal job panel, and the front door polls its
    own probe — a module goes live because its data landed, never
    because a run said so."""
    if module_id not in frontdoor.BY_ID:
        raise HTTPException(404, "unknown module")
    try:
        prompt, derived = frontdoor.setup_prompt(module_id, req.answers or {})
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        jid = jobs.launch(prompt, str(ROOT), None, req.model, False, None,
                          "manual")
    except ValueError as e:
        raise HTTPException(429, str(e))
    frontdoor.record_run(module_id, jid, req.answers or {})
    return {"job_id": jid, **derived}


@app.post("/api/frontdoor/{module_id}/setup-prompt")
def api_frontdoor_setup_prompt(module_id: str, req: FrontDoorSetupReq):
    """The composed prompt without launching anything — for copying into
    a separate session. No job, no state write."""
    if module_id not in frontdoor.BY_ID:
        raise HTTPException(404, "unknown module")
    try:
        prompt, derived = frontdoor.setup_prompt(module_id, req.answers or {})
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"prompt": prompt, "cwd": str(ROOT), **derived}


class DismissReq(BaseModel):
    undo: bool = False


@app.post("/api/frontdoor/{module_id}/dismiss")
def api_frontdoor_dismiss(module_id: str, req: DismissReq):
    if module_id not in frontdoor.BY_ID:
        raise HTTPException(404, "unknown module")
    return {"dismissed": frontdoor.dismiss(module_id, req.undo)}


@app.get("/api/reading/pages")
def api_reading_pages():
    """Personal reading-room pages on disk, with what a Reader card needs:
    subtitle, item count, done count, built date (all best-effort)."""
    pages = reading.page_details()
    try:
        by_room = {row.get("room"): row for row in research.catalog()
                   if row.get("status") == "ready" and row.get("room")}
        for page in pages:
            graph = by_room.get(page.get("name"))
            if graph:
                page["research"] = {
                    "slug": graph["id"],
                    "name": graph["name"],
                    "company": graph["company"],
                }
    except Exception:  # noqa: BLE001 -- Reader remains useful without a graph
        pass
    return {"pages": pages}


@app.get("/api/research")
def api_research_catalog():
    """Available canonical research graphs; no database body is loaded."""
    return {"projects": research.catalog()}


@app.get("/api/research/{graph_id}/claims/{claim_id}")
def api_research_claim(graph_id: str, claim_id: str):
    try:
        detail = research.claim_detail(claim_id, graph_id=graph_id)
    except research.ResearchGraphError as exc:
        raise HTTPException(404, str(exc)) from exc
    if detail is None:
        raise HTTPException(404, "no such research claim")
    return detail


@app.get("/api/research/{graph_id}/sources/{source_id}")
def api_research_source(graph_id: str, source_id: str):
    try:
        detail = research.source_detail(source_id, graph_id=graph_id)
    except research.ResearchGraphError as exc:
        raise HTTPException(404, str(exc)) from exc
    if detail is None:
        raise HTTPException(404, "no such research source")
    return detail


@app.get("/api/research/{graph_id}")
def api_research_overview(graph_id: str):
    try:
        return research.overview(graph_id)
    except research.ResearchGraphError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/reading/rooms/{name}")
def api_reading_room(name: str):
    """A native room's full store — meta + items — plus its done-marks, for
    the Reader's in-app renderer."""
    room = readingroom.load_room(name)
    if room is None:
        raise HTTPException(404, "no such reading room")
    try:
        done = reading.get_done(name)
    except ValueError:
        done = []
    # where each item's note actually is — DERIVED here rather than stored on
    # the item, so a renamed or deleted note can never leave a stale pointer
    try:
        roomvault.resolve(name, room.get("items") or [])
    except Exception:                                   # noqa: BLE001
        pass                                            # a room is worth more than its links
    return {"room": room, "done": done}


class RoomLinkReq(BaseModel):
    item_id: str
    path: str = ""


@app.post("/api/reading/rooms/{name}/link")
def api_reading_room_link(name: str, req: RoomLinkReq):
    """Attach a vault note to an item by hand — for a note the derivation
    cannot see because nothing wrote `room_item_id` into it. Empty path
    clears the override and falls back to whatever is derivable."""
    if not req.item_id.strip():
        raise HTTPException(400, "item_id required")
    path = req.path.strip()
    if path:
        # note_text is the validator: it carries the engine's own containment
        # check, so a path outside the vault is refused rather than stored
        try:
            vault.note_text(path)
        except Exception:                               # noqa: BLE001
            raise HTTPException(404, f"no such note: {path}")
    roomvault.set_link(name, req.item_id.strip(), path)
    return {"ok": True, "path": path}


@app.get("/reading/{slug}.html")
def reading_room_page(slug: str):
    """A room's standalone page. Native rooms render on demand from the
    store (the shareable EXPORT — never the source of truth); a legacy page
    still on disk is served as the file it is."""
    room_html = None
    try:
        room_html = readingroom.export_html(slug)
    except (KeyError, ValueError):
        pass
    if room_html is not None:
        return HTMLResponse(room_html,
                            headers={"Cache-Control": "no-cache"})
    if reading.NAME_RE.match(slug):
        p = ROOT / "static" / "reading" / f"{slug}.html"
        if p.is_file():
            return FileResponse(p, media_type="text/html")
    raise HTTPException(404, "no such reading room")


class RoomDefinitionReq(BaseModel):
    subject: str = ""
    why: str = ""
    # people accepts the legacy comma string OR the pill list
    # [{name, ref, qualifier}]; clean_definition normalizes either.
    people: str | list = ""
    sources: list = []            # [{label, feed, kind}]
    watch: list = []              # standing-watch strings
    modes: list = []
    depth: str = ""
    notes: str = ""
    title: str | None = None      # None = keep; the room must stay nameable
    subtitle: str | None = None


@app.put("/api/reading/rooms/{name}/definition")
def api_reading_room_definition(name: str, req: RoomDefinitionReq):
    """Save a room's definition — the owner-visible spec of what it tracks
    and why — plus, when sent, the title and the line under it. Refreshes
    follow the definition; forking starts from it."""
    try:
        out = {"definition": readingroom.set_definition(
            name, req.dict(exclude={"title", "subtitle"}))}
        if req.title is not None or req.subtitle is not None:
            out["meta"] = readingroom.set_meta(name, req.title, req.subtitle)
        return out
    except KeyError:
        raise HTTPException(404, "no such reading room")
    except readingroom.BuildError as e:
        raise HTTPException(422, str(e))


class VaultPersonReq(BaseModel):
    name: str
    qualifier: str = ""


@app.get("/api/vault/people")
def api_vault_people(q: str = ""):
    """Typeahead over the vault's type:person pages — the grounding layer
    for a room's people pills. Names only, never page bodies."""
    from . import vaultpeople
    return {"people": vaultpeople.search(q)}


@app.post("/api/vault/people")
def api_vault_people_create(req: VaultPersonReq):
    """Mint a stub person page for a name the index cannot resolve —
    curating a room grows the people graph as a side effect."""
    from . import vaultpeople
    try:
        return vaultpeople.create_stub(req.name, req.qualifier)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))


class SourceResolveReq(BaseModel):
    text: str


@app.post("/api/reading/source-resolve")
def api_reading_source_resolve(req: SourceResolveReq):
    """What the owner typed -> an enumerable feed pill, resolved once at
    save time (a @handle costs one page fetch here, never again)."""
    try:
        return readingroom.resolve_source(req.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:                              # noqa: BLE001
        raise HTTPException(502, f"could not reach that source: {e}")


@app.get("/api/reading/rooms/{name}/update-prompt")
def api_reading_room_update_prompt(name: str):
    """The refresh prompt for pasting into another session — no job launched.
    The copy path also serves passive test instances, which cannot dispatch."""
    try:
        return {"prompt": readingroom.update_prompt(name), "cwd": str(ROOT)}
    except (KeyError, ValueError):
        raise HTTPException(404, "no such reading room")


@app.post("/api/reading/rooms/{name}/update")
def api_reading_room_update(name: str):
    """Dispatch a session that re-researches the room's subject and rebuilds
    the same slug — item ids are URL-stable, so done-marks survive. This is
    what makes a room a live tracker rather than a frozen sweep."""
    if os.environ.get("VIRA_PASSIVE"):
        raise HTTPException(403, "passive instance — copy the prompt into a "
                                 "session instead (update-prompt)")
    try:
        prompt = readingroom.update_prompt(name)
    except (KeyError, ValueError):
        raise HTTPException(404, "no such reading room")
    jid = jobs.launch(prompt, cwd=str(ROOT), mode="manual",
                      meta={"kind": "room-update", "room": name})
    return {"job_id": jid}


class ReadingCompleteReq(BaseModel):
    done: bool = True


@app.get("/api/reading/list")
def api_reading_list():
    """The Reader's queue: everything worth reading that is not read yet.

    Completed entries are deliberately absent from `queue` — marking a document
    read takes it off the list, because the document still lives wherever its
    producer put it. `completed` is the FULL read list, uncapped: it feeds the
    docs view's Read filter, and a filter that silently truncated 425 read
    documents to a tail would violate the no-silent-truncation rule (the old
    limit=400 was already under the live count).

    Tags and film metadata are joined HERE rather than stored on the entry:
    both are derived (a re-tag pass rewrites tags; recapturing a film changes
    its thumb), and a copy on the row would be a copy that goes stale. Same
    reason roomvault.resolve annotates at this layer."""
    q = docthumbs.annotate(doctags.annotate(readinglist.queue()))
    done = docthumbs.annotate(doctags.annotate(
        readinglist.completed(limit=None)))
    films = {f["url"]: f for f in walkthroughs.films()}

    def join(rows):
        for r in rows:
            f = films.get(r.get("locator"))
            if f:
                r["film"] = {"thumb": f["thumb"], "motion": f["motion"],
                             "project": f["project"], "subject": f["subject"],
                             "description": f["description"]}
        return rows

    return {"queue": join(q), "completed": join(done),
            "counts": readinglist.counts(),
            "vocab": doctags.vocabulary(q + done),
            "tagging": doctags.status(q + done)}


@app.post("/api/reading/list/tag")
def api_reading_list_tag(batches: int = 1):
    """Tag pending documents. Bounded — a click can never become an unbounded
    spend, the /api/ideas/reindex rule."""
    return doctags.refresh(batches)


@app.post("/api/reading/list/backfill")
def api_reading_list_backfill():
    """Register everything already on disk. Idempotent."""
    return readinglist.backfill()


@app.post("/api/reading/list/{item_id}/complete")
def api_reading_list_complete(item_id: str, req: ReadingCompleteReq):
    try:
        return {"item": readinglist.complete(item_id, req.done)}
    except KeyError:
        raise HTTPException(404, "no such reading-list entry")


@app.delete("/api/reading/list/{item_id}")
def api_reading_list_forget(item_id: str):
    """Drop the pointer. The document itself is never touched."""
    try:
        readinglist.forget(item_id)
    except KeyError:
        raise HTTPException(404, "no such reading-list entry")
    return {"ok": True}


@app.get("/api/reading/list/{item_id}")
def api_reading_list_item(item_id: str):
    """One entry plus its sections, so the client knows how to open it and
    whether to show section progress."""
    it = readinglist.get(item_id)
    if not it:
        raise HTTPException(404, "no such reading-list entry")
    out = dict(it)
    out["missing"] = readinglist._missing(it)
    out["sections"] = readinglist.sections(it)
    out["progress"] = readinglist.progress(it)
    return out


@app.get("/api/reading/thumb/{item_id}")
def api_reading_thumb(item_id: str):
    """A document's rendered face (server/docthumbs.py). The id is validated
    against the rl_ scheme before it touches the filesystem."""
    p = docthumbs.by_id(item_id)
    if not p:
        raise HTTPException(404, "no thumbnail for that document")
    return FileResponse(p, media_type="image/png",
                        headers={"X-Content-Type-Options": "nosniff"})


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


class JournalResolveReq(BaseModel):
    entry_id: str
    instruction: str


@app.post("/api/brief/journal/resolve")
def api_brief_journal_resolve(req: JournalResolveReq):
    """Mark one 'needs a session' instruction done — it drops off the Queue
    lane and the export, staying on its journal entry as a completed record."""
    ok = journal.resolve_unapplied(req.entry_id, req.instruction)
    if not ok:
        raise HTTPException(404, "instruction not found or already resolved")
    return {"resolved": True}


@app.post("/api/brief/journal/resolve-all")
def api_brief_journal_resolve_all():
    """Clear every open 'needs a session' instruction at once."""
    return {"count": journal.resolve_all_unapplied()}


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


@app.get("/api/group/profile")
def api_group_profile(ids: str = "", chat: int | None = None):
    """The group panel payload: the merged group, member dossiers, who-talks
    activity, interconnection edges, related-group diffs, media counts.
    Address by `chat` (one rowid, e.g. off a feed item) or `ids` (a merged
    leg list off a person page's group row)."""
    if chat is not None:
        return groupchat.profile(chat=chat)
    return groupchat.profile(chat_ids=_parse_chat_ids(ids))


class GroupBriefReq(BaseModel):
    ids: list[int]
    force: bool = False


@app.post("/api/group/brief")
def api_group_brief(req: GroupBriefReq):
    """The one AI pass over the group — cached until a newer message lands."""
    if not req.ids:
        raise HTTPException(400, "no chat ids")
    try:
        return groupchat.brief(req.ids[:60], force=req.force)
    except Exception as e:  # noqa: BLE001 — model/backend failure, honestly
        raise HTTPException(502, f"brief failed: {e}")


class GroupSendReq(BaseModel):
    ids: list[int]
    text: str


@app.post("/api/group/send")
def api_group_send(req: GroupSendReq):
    """Send into the group's active leg (chat-guid addressed)."""
    if not req.ids:
        raise HTTPException(400, "no chat ids")
    try:
        return groupchat.send(req.ids[:60], req.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


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


# ---------- Android companion (pairing, message ingest, pings) ----------
# The phone-facing endpoints authenticate every request with the paired
# device's token (X-Vira-Device + Authorization: Bearer). Writes refuse on
# passive instances — companion.assert_active, the send.py precedent.

def _companion_auth(x_vira_device: str | None, authorization: str | None):
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    dev = companion.auth(x_vira_device or "", token)
    if not dev:
        raise HTTPException(401, "unknown device or bad token")
    return dev


@app.post("/api/companion/pair/start")
def api_companion_pair_start(request: Request):
    """Owner-side: mint a pairing and return the QR. The URL advertises
    the port this hub actually answers on."""
    try:
        port = request.url.port or 8377
        return companion.pair_start(url=companion.hub_url(port))
    except RuntimeError as e:
        raise HTTPException(403, str(e))


class CompanionPairReq(BaseModel):
    device_id: str
    token: str
    name: str | None = ""
    platform: str | None = ""


@app.post("/api/companion/pair")
def api_companion_pair(req: CompanionPairReq):
    """Phone-side: claim the pairing the QR carried."""
    try:
        return companion.pair_complete(req.device_id, req.token,
                                       req.name or "", req.platform or "")
    except PermissionError as e:
        raise HTTPException(401, str(e))
    except RuntimeError as e:
        raise HTTPException(403, str(e))


class CompanionBatchReq(BaseModel):
    messages: list[dict]


@app.post("/api/companion/messages")
def api_companion_messages(req: CompanionBatchReq,
                           x_vira_device: str | None = Header(None),
                           authorization: str | None = Header(None)):
    dev = _companion_auth(x_vira_device, authorization)
    if len(req.messages) > 500:
        raise HTTPException(413, "batch too large (max 500)")
    try:
        return companion.ingest(dev["id"], req.messages, watcher=watcher)
    except RuntimeError as e:
        raise HTTPException(403, str(e))


@app.get("/api/companion/pings")
def api_companion_pings(after: int = 0, wait: int = 25,
                        x_vira_device: str | None = Header(None),
                        authorization: str | None = Header(None)):
    """Phone-side long-poll (sync def — blocks a worker thread, never the
    event loop). Returns immediately when anything is newer than `after`."""
    _companion_auth(x_vira_device, authorization)
    return {"pings": companion.wait_for_pings(after, wait)}


@app.get("/api/companion/status")
def api_companion_status():
    """Owner UI: paired devices (no tokens) + ingest counters."""
    return {"devices": companion.devices(), **companion.stats(),
            "hub_url": companion.hub_url()}


@app.delete("/api/companion/device/{device_id}")
def api_companion_unpair(device_id: str):
    try:
        return companion.unpair(device_id)
    except RuntimeError as e:
        raise HTTPException(403, str(e))


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
    channel: str | None = None  # "imessage" | "sms"; None = auto/proactive


@app.post("/api/send")
def api_send(req: SendReq):
    try:
        r = send.send_message(req.text, req.person_id, req.handle, req.channel)
        return {"sent": True, "handle": r["handle"], "channel": r["channel"],
                "downgraded": r["downgraded"], "note": r["note"]}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001 — surface Messages/permission errors
        raise HTTPException(502, str(e)[:500])


class SendChannelReq(BaseModel):
    channel: str | None = None  # "imessage" | "sms" | null/"auto" to clear


@app.get("/api/person/{pid}/send-channel")
def api_get_send_channel(pid: str):
    return {"pref": sendpref.get(pid)}


@app.post("/api/person/{pid}/send-channel")
def api_set_send_channel(pid: str, req: SendChannelReq):
    try:
        return {"pref": sendpref.set_channel(pid, req.channel, source="owner")}
    except ValueError as e:
        raise HTTPException(400, str(e))


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


class ImapAddReq(BaseModel):
    email: str
    host: str
    password: str


@app.post("/api/mail/imap/add")
def api_mail_imap_add(req: ImapAddReq):
    """Add a Gmail/IMAP mailbox from the Setup window. Refused on passive
    test instances — a clone must not write a real password into the
    machine-wide secrets store."""
    if os.environ.get("VIRA_PASSIVE"):
        raise HTTPException(400, "passive test instance — mail isn't added here")
    try:
        return mail.add_imap_account(req.email, req.host, req.password)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


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


@app.get("/api/mail/message")
def api_mail_message(account: str, rowid: str = "", mid: str = ""):
    """The full email behind a feed card — body, recipients, threading
    ids — so clicking one reads like opening the mail, not the caption."""
    try:
        return mailread.get_message(account, rowid or None, mid or None)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001 — surface IMAP/Graph errors
        raise HTTPException(502, str(e)[:500])


class MailReplyReq(BaseModel):
    account: str
    text: str
    to: str | None = None
    subject: str | None = None
    message_id: str | None = None
    references: str | None = None
    graph_id: str | None = None


@app.post("/api/mail/reply")
def api_mail_reply(req: MailReplyReq):
    """Send a real threaded email reply. Gmail goes out over SMTP now;
    M365 sends via Graph once Mail.Send is consented, and until then the
    reply lands as an Outlook draft with the response saying so."""
    if os.environ.get("VIRA_PASSIVE"):
        raise HTTPException(403, "passive test instance — outbound email "
                                 "is blocked here")
    try:
        return mailread.send_reply(
            req.account, req.text, to=req.to, subject=req.subject,
            message_id=req.message_id, references=req.references,
            graph_id=req.graph_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001 — surface send failures
        raise HTTPException(502, str(e)[:500])


# ---------- whatsapp: linked-device connect + poll ----------

@app.get("/api/whatsapp/status")
def api_whatsapp_status():
    return {"linked": whatsapp.linked(),
            "installed": whatsapp.installed(),
            "passive": bool(os.environ.get("VIRA_PASSIVE")),
            "watcher": whatsapp_watcher.status,
            "sidecar": whatsapp.sidecar_status()}


@app.get("/api/whatsapp/qr")
def api_whatsapp_qr():
    return whatsapp.qr()


@app.post("/api/whatsapp/pair")
def api_whatsapp_pair():
    """Start (or find) the sidecar so its pairing QR becomes available.
    Refused on passive instances — the sidecar links a device to the
    owner's account, so a test copy may only read one started by hand."""
    try:
        return {"sidecar": whatsapp.ensure_sidecar()}
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.post("/api/whatsapp/poll")
def api_whatsapp_poll():
    """One explicit ingest pass. On live the watcher does this on its own;
    this route serves the settings card's check-now and passive test
    instances (reads the local sidecar only — no world action)."""
    try:
        return whatsapp.ingest(watcher)
    except RuntimeError as e:
        raise HTTPException(502, str(e)[:300])


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


class ResolveReq(BaseModel):
    handle: str
    person_id: str | None = None
    memory: str | None = None


@app.post("/api/triage/resolve")
def api_triage_resolve(req: ResolveReq):
    """AI-assisted name resolution for an unknown handle — read-only: it
    gathers evidence and proposes a name, but writes nothing. The Add flow
    still owns the people.json write."""
    try:
        return resolver.resolve(req.handle, req.person_id, req.memory or "")
    except Exception as e:  # noqa: BLE001 — model call/parse is best-effort
        raise HTTPException(502, f"couldn't resolve: {str(e)[:200]}")


class AddPersonReq(BaseModel):
    name: str
    handles: list[str] = []
    class_hint: str | None = None
    note: str | None = None
    person_id: str | None = None  # set = rename an existing placeholder entry
    fact: str | None = None       # durable provenance (e.g. referral origin)


@app.post("/api/crm/add")
def api_crm_add(req: AddPersonReq):
    try:
        person = triage.add_person(req.name, req.handles, req.class_hint,
                                   req.note, req.person_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if req.fact:
        # source:"vira" fact — survives profile re-synthesis. Best-effort:
        # the person is already added, so a fact-write hiccup never fails it.
        try:
            crm.add_fact(person["id"], req.fact)
        except Exception:  # noqa: BLE001
            pass
    return {"added": True, "person": person}


# ---------- onboarding (Setup window: importers, dossiers, the Brain) ----


class OnboardCsvReq(BaseModel):
    csv: str


class OnboardDossiersReq(BaseModel):
    limit: int = 25


class OnboardVaultReq(BaseModel):
    path: str
    init: bool = False


class OnboardAiReq(BaseModel):
    provider: str                      # anthropic | openai
    api_key: str | None = None         # pasted key -> Keychain, never config
    model: str | None = None


class OnboardLoginReq(BaseModel):
    provider: str
    code: str | None = None            # the pasted OAuth code (code route)


@app.get("/api/onboard")
def api_onboard():
    return onboard.status()


@app.get("/api/onboard/steps")
def api_onboard_steps():
    """The guided-setup state machine, derived fresh from the world."""
    return onboard.steps()


class DemoResetReq(BaseModel):
    update: bool = True


@app.post("/api/demo/reset")
def api_demo_reset(req: DemoResetReq | None = None):
    """Put a SANDBOX instance back to a brand-new user, on the latest code.

    A first run happens once per install, which makes the one screen a
    stranger sees hardest to look at twice — reviewing it meant going back to
    a shell and re-running a script. This is that, as a button on the badge.

    Two things a stranger's first boot has that a re-walk did not: the CURRENT
    code, and no leftovers. Both are handled here, in that order, because they
    fail differently.

    The PULL happens in-process, so a refusal is reported in the browser —
    `update.pull()` already refuses a dirty tree and raises DepsError when the
    code moved but its dependencies did not. That one is fatal: restarting
    onto new code with old deps is exactly what update.apply() refuses to do,
    so the reset stops with the checkout updated and the server untouched. An
    ordinary refusal (no network, no remote) is reported as a note and the
    reset carries on — a walk-through blocked by an offline laptop would be a
    worse failure than a slightly stale one.

    The WIPE is queued for the relaunch loop rather than done here. data/ holds
    open sqlite handles belonging to this process; deleting it underneath them
    and then serving from the wreckage is not a virgin install, it is a broken
    one. So the loop does it in the gap between two runs (see the SUPERVISOR
    note in scripts/sandbox.sh), and this endpoint exits into it.

    Without a loop there is nothing to exit into, so the reset degrades to the
    old shallow behaviour — the ui-state keys — and SAYS SO rather than
    implying it did more.

    Refused anywhere but a sandbox. The live instance and a branch test
    instance both carry real arrangements and a real connected account, and
    an endpoint that wipes onboarding must not be reachable there at all.
    """
    if not settings.sandboxed():
        raise HTTPException(status_code=403,
                            detail="reset is a sandbox-only control")
    want_update = True if req is None else req.update
    loop = settings.sandbox_loop()

    updated = None
    if want_update:
        try:
            updated = update.pull()
        except update.DepsError as e:
            raise HTTPException(status_code=500, detail=str(e))
        except (ValueError, OSError) as e:                     # noqa: BLE001
            updated = {"updated": False, "note": f"update skipped: {e}"}

    # Onboarding state that does NOT live in data/: a pasted key went to the
    # secrets ladder, which on a Mac is the machine Keychain (namespaced by
    # VIRA_KEYCHAIN_PREFIX, so only this sandbox's keys are reachable). A key
    # surviving the wipe would leave the app connected while its config said
    # nothing — the exact half-reset this is fixing.
    # (`secrets.delete` is best-effort and returns nothing, so what was
    # actually there is read first — the response says which keys it cleared.)
    cleared = []
    svc = settings.keychain_service("vira-model-key")
    for pid in list(getattr(models, "PROVIDERS", {})):
        try:
            if secrets.get(svc, pid):
                cleared.append(pid)
            secrets.delete(svc, pid)
        except Exception:                                      # noqa: BLE001
            pass
    # Demo sign-ins live in memory, so a fresh user has to un-connect too —
    # otherwise the welcome takes its already-set-up path and the four
    # provider tiles never render.
    dropped = []
    try:
        dropped = sorted(models._demo_connected)
        models._demo_connected.clear()
    except Exception:                                          # noqa: BLE001
        pass

    forgot = uistate.forget(["vira-firstrun-done", "vira-layouts",
                             "vira-layout", "vira-setup-opened"])
    if not loop:
        return {"ok": True, "restarting": False, "forgot": forgot,
                "disconnected": dropped, "keys_cleared": cleared,
                "update": updated,
                "note": ("no relaunch loop is supervising this sandbox, so "
                         "data/ was left in place — restart it with "
                         "scripts/sandbox.sh replay for a full reset")}
    Path(loop).write_text("wipe\n", encoding="utf-8")
    threading.Timer(0.8, lambda: os._exit(0)).start()   # let the response flush
    return {"ok": True, "restarting": True, "forgot": forgot,
            "disconnected": dropped, "keys_cleared": cleared,
            "update": updated}


@app.post("/api/onboard/ai")
def api_onboard_ai(req: OnboardAiReq):
    """Select the model provider, and optionally store an API key for it.

    The key goes to the Keychain, never to data/config.json — config is
    plaintext, and a stranger setting Vira up from the browser has no shell
    profile to put an env var in."""
    try:
        return onboard.set_provider(req.provider, req.api_key, req.model)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


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


@app.post("/api/onboard/fda-assist")
def api_onboard_fda_assist():
    try:
        return onboard.fda_assist()
    except RuntimeError as e:      # passive: refuse, don't pop windows
        raise HTTPException(403, str(e))
    except ValueError as e:        # off-Mac: the grant does not exist
        raise HTTPException(400, str(e))


class PickFolderReq(BaseModel):
    prompt: str = "Choose a folder"
    # Whether the BROWSER is on this machine. The client decides (a hostname
    # that is not localhost is the phone over Tailscale), because the server
    # cannot tell a same-machine request from a tailnet one by address alone
    # — and popping a panel on the wrong desk is the failure this avoids.
    local: bool = True


@app.post("/api/pick-folder")
def api_pick_folder(req: PickFolderReq):
    # Never raises — an unavailable picker is a normal answer the UI renders
    # beside the text field it falls back to.
    return pickfolder.pick(req.prompt, local=req.local)


# ---------- the driven sign-in (no terminal) ----------

@app.post("/api/onboard/login")
def api_onboard_login(req: OnboardLoginReq):
    try:
        return models.login_start(req.provider)
    except RuntimeError as e:      # passive test instance
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/onboard/login/code")
def api_onboard_login_code(req: OnboardLoginReq):
    try:
        return models.login_code(req.provider, req.code or "")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/onboard/login/cancel")
def api_onboard_login_cancel(req: OnboardLoginReq):
    return models.login_cancel(req.provider)


@app.get("/api/onboard/login/{pid}")
def api_onboard_login_status(pid: str):
    return models.login_status(pid)


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
    # Finalize this run's output as a plan: save the markdown to the vault
    # and render the HTML dossier. It says nothing about permissions — see
    # plans.SHAPE and read_only below.
    publish_plan: bool = False
    # Deny every write. Independent of publish_plan since 2026-08-04: the
    # Queue's Plan button asks for BOTH and says so here, rather than
    # inheriting a permission rung from what it does with the output.
    read_only: bool = False
    idea_id: str | None = None
    # The permission ladder, safest first (session.MODES), named to match
    # Claude Code's own --permission-mode values: "manual" (every risky call
    # gated) | "acceptEdits" (edits land, commands gated) |
    # "bypassPermissions" (everything runs; only the read-only and
    # branch-first denials still fire). Retired spellings still resolve via
    # session.norm_mode. Absent -> derived from permission_mode, else the
    # config default (session_default_mode, "bypassPermissions" out of the
    # box). Every rung is steerable — the mode decides what the gate stops,
    # never whether the owner can talk to the session.
    mode: str | None = None
    # Which engine drives the session ("anthropic" | "openai"); absent, the
    # model names it, else the configured session-capable go-to. See
    # server/agentbackend.py.
    provider: str | None = None


@app.post("/api/actions/run")
def api_run(req: RunReq):
    try:
        jid = jobs.launch(req.prompt, req.cwd, req.permission_mode, req.model,
                          req.publish_plan, req.idea_id, req.mode,
                          read_only=req.read_only, provider=req.provider)
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
        row["title"] = joblog.name(rec, it)
        row["command"] = rec.get("command") or joblog.command(rec, it)
    return rows


@app.get("/api/jobs")
def api_jobs():
    return {"jobs": _ensure_names(jobs.recent())}


@app.get("/api/sessions/pending")
def api_sessions_pending():
    """Every decision a live session is blocked on, oldest first — the feed
    behind the app-wide decision cards. Titles come from the same ledger
    naming as every other job surface, so the card names the session the
    way the Live tab and the terminal bar do."""
    rows = jobs.pending_all()
    named = _ensure_names([{"id": r["job_id"]} for r in rows])
    for row, name in zip(rows, named):
        row["title"] = name["title"]
        row["command"] = name["command"]
    return {"pending": rows}


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
        # A replayed job must still name the engine that answered it — the
        # live snapshot carries `provider`, so the ledger replay has to as
        # well or the terminal banner regrades a finished best-effort
        # session as gated the moment it leaves the live registry. Rows
        # written before the ledger persisted it fall back to the model's
        # own provider, the same heuristic the launch used to pick it.
        "provider": (r.get("provider")
                     or agentbackend.provider_of_model(r.get("model"))
                     or "anthropic"),
        # Same reasoning as `provider` above, for the same reason the
        # ledger records it: a replayed job must name the generation that
        # actually answered, not the alias that was asked for.
        "model": r.get("model"), "model_used": r.get("model_used", ""),
        "publish_plan": r.get("publish_plan"),
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


class AnswerReq(BaseModel):
    req_id: str
    answer: str          # the option the owner clicked, or free text


@app.post("/api/session/{sid}/answer")
async def api_session_answer(sid: str, req: AnswerReq):
    """Answer a decision card the session is blocked on."""
    try:
        session.sessions.answer(sid, req.req_id, req.answer)
    except KeyError as e:
        raise HTTPException(404, f"unknown session or question: {e}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"answered": True}


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


# The Forge product API. Circuits and routines below remain compatibility
# routes and execution authorities; /api/flows is the cohesive editor-facing
# view over both stores.

@app.get("/api/flows")
def api_flows():
    return {"flows": flows.list_flows()}


@app.get("/api/flows/kit")
def api_flows_kit():
    return {"items": flows.kit_catalog()}


@app.get("/api/flows/kit/source")
def api_flows_kit_source(ref: str):
    try:
        return flows.kit_source(ref)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError:
        raise HTTPException(404, "Kit source not found")


@app.get("/api/flows/native/{routine_id}/source")
def api_flows_native_source(routine_id: str):
    try:
        return flows.native_source(routine_id)
    except KeyError:
        raise HTTPException(404, "native Flow source not found")


@app.get("/api/flows/{flow_id}")
def api_flow(flow_id: str):
    flow = flows.get_flow(flow_id)
    if not flow:
        raise HTTPException(404, "unknown flow")
    return flow


class FlowReq(BaseModel):
    id: str | None = None
    name: str
    description: str | None = ""
    kind: str | None = "flow"
    nodes: list[dict]
    edges: list[dict]
    contexts: list[dict] | None = None


@app.post("/api/flows")
def api_flow_create(req: FlowReq):
    try:
        return flows.save_flow(req.model_dump(), save_as=True)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/flows/{flow_id}")
def api_flow_update(flow_id: str, req: FlowReq):
    payload = req.model_dump()
    payload["id"] = flow_id
    try:
        return flows.save_flow(payload)
    except KeyError:
        raise HTTPException(404, "unknown flow")
    except ValueError as e:
        raise HTTPException(400, str(e))


class FlowRunReq(BaseModel):
    input: str = ""
    cwd: str | None = None
    notify: bool = False
    output: str | None = ""
    # Set when the Flow was loaded from a Queue idea, so the run closes that
    # idea out the way a Plan/Implement dispatch does.
    idea_id: str | None = None


@app.post("/api/flows/{flow_id}/run")
def api_flow_run(flow_id: str, req: FlowRunReq):
    try:
        return flows.run_flow(flow_id, req.input, cwd=req.cwd,
                              notify=req.notify, output=req.output or "",
                              idea_id=req.idea_id)
    except KeyError:
        raise HTTPException(404, "unknown flow")
    except ValueError as e:
        raise HTTPException(400, str(e))


class IdeaFlowReq(BaseModel):
    template: str = flows.DEFAULT_IDEA_TEMPLATE


@app.post("/api/ideas/{idea_id}/flow")
def api_idea_flow(idea_id: str, req: IdeaFlowReq):
    """Load a Queue idea into the Forge as its own editable Flow."""
    try:
        return flows.flow_for_idea(idea_id, req.template)
    except KeyError:
        raise HTTPException(404, "unknown idea")
    except ValueError as e:
        raise HTTPException(400, str(e))


class FlowApprovalReq(BaseModel):
    approved: bool
    note: str | None = ""


@app.post("/api/flows/runs/{run_id}/approval/{stage_id}")
def api_flow_approval(run_id: str, stage_id: str, req: FlowApprovalReq):
    try:
        return _run_with_result(circuits.decide_approval(
            run_id, stage_id, req.approved, req.note or ""))
    except KeyError:
        raise HTTPException(404, "unknown run or approval")
    except ValueError as e:
        raise HTTPException(409, str(e))

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


@app.get("/api/vault/resolve")
def api_vault_resolve(ref: str):
    """Resolve one [[wikilink]] the way Obsidian does — by exact stem."""
    hit = vault.resolve_ref(ref)
    if hit is None:
        raise HTTPException(404, "no note named " + ref)
    return hit


@app.get("/api/vault/stems")
def api_vault_stems():
    """Every note name, so a rendered page can mark its dead links without
    one request per link — an index page carries thousands."""
    return {"stems": vault.known_stems()}


# ------------------------------------------------------------- definitions

@app.get("/api/define")
def api_define(term: str):
    with admission.cpu("define"):
        try:
            return define.lookup(term)
        except define.DefineError as e:
            raise HTTPException(400, str(e))


@app.get("/api/define/status")
def api_define_status():
    return define.status()


class SourceReq(BaseModel):
    term: str


@app.post("/api/define/source")
def api_define_source(req: SourceReq):
    """Rung 4: the only rung allowed to write citations, because it is the
    only one that actually goes and reads them."""
    term = define.clean_term(req.term)
    if not term:
        raise HTTPException(400, "that selection is not a term")
    if os.environ.get("VIRA_PASSIVE"):
        raise HTTPException(403, "passive instance: sourcing writes the "
                                 "live vault")
    try:
        jid = jobs.launch(define.source_prompt(term), cwd=str(ROOT),
                          mode="acceptEdits",
                          meta={"define_term": term})
    except ValueError as e:
        raise HTTPException(429, str(e))
    return {"job_id": jid}


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


class CircuitStagesReq(BaseModel):
    # stage id -> the tray's editable fields (model / extra / mode /
    # read_only / min_grade / max_retries); see circuits.apply_overrides.
    stages: dict[str, dict]


@app.post("/api/circuits/{cid}/stages")
def api_circuit_stages(cid: str, req: CircuitStagesReq):
    """Bake the Run tray's stage edits into the definition — "save as
    default", so the next run starts where this one was tuned to."""
    try:
        return circuits.update_stages(cid, req.stages)
    except KeyError:
        raise HTTPException(404, "unknown circuit")
    except ValueError as e:
        raise HTTPException(400, str(e))


class CircuitRunReq(BaseModel):
    input: str
    cwd: str | None = None
    notify: bool = False
    stages: dict[str, dict] | None = None      # per-run stage tray edits


@app.post("/api/circuits/{cid}/run")
def api_circuits_run(cid: str, req: CircuitRunReq):
    try:
        return circuits.start_run(cid, req.input, cwd=req.cwd,
                                  notify=req.notify, overrides=req.stages)
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


@app.get("/api/routines/{rid}/hood")
def api_routine_hood(rid: str):
    r = routines.get_routine(rid)
    if not r:
        raise HTTPException(404, "unknown routine")
    return routinesrc.hood(r)


class HoodEditReq(BaseModel):
    part: str
    text: str


@app.post("/api/routines/{rid}/hood")
def api_routine_hood_edit(rid: str, req: HoodEditReq):
    r = routines.get_routine(rid)
    if not r:
        raise HTTPException(404, "unknown routine")
    try:
        return routinesrc.apply_part(r, req.part, req.text)
    except routinesrc.EditError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


class HoodRevertReq(BaseModel):
    module: str
    to: str = "backup"        # backup | shipped


@app.post("/api/routines/{rid}/hood/revert")
def api_routine_hood_revert(rid: str, req: HoodRevertReq):
    if not routines.get_routine(rid):
        raise HTTPException(404, "unknown routine")
    try:
        if req.to == "shipped":
            return routinesrc.restore_shipped(req.module)
        return routinesrc.revert(req.module)
    except routinesrc.EditError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/restart")
def api_restart():
    """Restart the server after a source edit. Refuses exactly where
    update.apply() refuses — an unsupervised process that exits has
    nothing to bring it back."""
    if os.environ.get("VIRA_PASSIVE") or os.environ.get("VIRA_SANDBOX"):
        raise HTTPException(403, "this instance does not restart itself")
    kind, name = update.supervisor()
    if not name:
        raise HTTPException(
            400, f"no supervisor configured ({kind}) — restart from a "
                 "terminal instead")
    threading.Thread(target=update._restart, daemon=True).start()
    return {"restarting": True, "supervisor": name}


@app.get("/api/radar")
def api_radar():
    return radar.compose()


@app.post("/api/radar/groupings/refresh")
def api_radar_refresh():
    import threading as _t
    _t.Thread(target=radar.refresh_groupings, daemon=True,
              name="vira-groupings-refresh").start()
    return {"refreshing": True}


class DismissGroupingReq(BaseModel):
    key: str
    restore: bool = False


@app.post("/api/radar/dismiss")
def api_radar_dismiss(req: DismissGroupingReq):
    radar.dismiss(req.key, restore=req.restore)
    return {"ok": True}


@app.post("/api/reconnect/refresh")
def api_reconnect_refresh():
    import threading as _t
    _t.Thread(target=reconnect.refresh, daemon=True,
              name="vira-pivot-scout").start()
    return {"refreshing": True}


@app.post("/api/reconnect/dismiss")
def api_reconnect_dismiss(req: DismissGroupingReq):
    reconnect.dismiss(req.key, restore=req.restore)
    return {"ok": True}


# ---------- orphan-work sweeper (unlanded worktrees/branches, Work > Live) ----------

class OrphanKeyReq(BaseModel):
    key: str


class OrphanDiscardReq(BaseModel):
    key: str
    force: bool = False


def _orphan_item(key):
    return next((it for it in orphanwork.compose()["items"] if it["key"] == key),
               None)


@app.get("/api/orphanwork")
def api_orphanwork():
    return orphanwork.compose()


@app.post("/api/orphanwork/refresh")
def api_orphanwork_refresh():
    orphanwork.refresh()
    return orphanwork.compose()


@app.post("/api/orphanwork/dismiss")
def api_orphanwork_dismiss(req: DismissGroupingReq):
    orphanwork.dismiss(req.key, restore=req.restore)
    return {"ok": True}


@app.post("/api/orphanwork/resume")
def api_orphanwork_resume(req: OrphanKeyReq):
    """Dispatch a session back into the item's own worktree. Refused on a
    passive instance — it shares the live repo's worktrees, so it must
    never dispatch a real resume."""
    if os.environ.get("VIRA_PASSIVE"):
        raise HTTPException(403, "passive instance — copy the resume prompt "
                                 "into a session instead (resume-prompt)")
    it = _orphan_item(req.key)
    if it is None:
        raise HTTPException(404, "no such orphan-work item")
    try:
        jid = orphanwork.resume(it)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"job_id": jid}


@app.get("/api/orphanwork/resume-prompt")
def api_orphanwork_resume_prompt(key: str):
    """The composed resume prompt with no side effects — for a passive
    instance, or anyone who wants to paste it into another session."""
    it = _orphan_item(key)
    if it is None:
        raise HTTPException(404, "no such orphan-work item")
    return {"prompt": orphanwork.resume_prompt(it),
            "cwd": it.get("worktree") or str(ROOT)}


@app.post("/api/orphanwork/merge")
def api_orphanwork_merge(req: OrphanKeyReq):
    """scripts/branch.sh owns preflight and refusals; passive is blocked
    because a test instance shares the live repo — a merge from there would
    mutate the real checkout."""
    if os.environ.get("VIRA_PASSIVE"):
        raise HTTPException(403, "passive instance — merge from the live "
                                 "checkout instead")
    it = _orphan_item(req.key)
    if it is None:
        raise HTTPException(404, "no such orphan-work item")
    if it.get("kind") == "unpushed":
        raise HTTPException(409, "main has nothing to merge — it needs a push")
    slug = it["branch"].split("/", 1)[-1]
    ok, detail = orphanwork.merge(slug)
    if not ok:
        raise HTTPException(409, detail)
    return {"started": True}


@app.post("/api/orphanwork/discard")
def api_orphanwork_discard(req: OrphanDiscardReq):
    if os.environ.get("VIRA_PASSIVE"):
        raise HTTPException(403, "passive instance — discard from the live "
                                 "checkout instead")
    it = _orphan_item(req.key)
    if it is None:
        raise HTTPException(404, "no such orphan-work item")
    if it.get("kind") == "unpushed":
        raise HTTPException(409, "main can't be discarded")
    slug = it["branch"].split("/", 1)[-1]
    ok, detail = orphanwork.discard(slug, force=req.force)
    if not ok:
        raise HTTPException(409, detail)
    return {"started": True}


@app.post("/api/orphanwork/land")
def api_orphanwork_land(req: OrphanKeyReq):
    """Finish-and-merge in one gesture: a dirty row gets a finishing
    session dispatched into its worktree first, then the server merges +
    pushes when it completes; a clean committed row merges directly.
    Passive blocked — both halves act on the real repo."""
    if os.environ.get("VIRA_PASSIVE"):
        raise HTTPException(403, "passive instance — land from the live "
                                 "checkout instead")
    it = _orphan_item(req.key)
    if it is None:
        raise HTTPException(404, "no such orphan-work item")
    if it.get("kind") == "unpushed":
        raise HTTPException(409, "main needs a push, not a landing")
    try:
        jid = orphanwork.land(it)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"started": True, "job_id": jid}


@app.post("/api/orphanwork/land-all")
def api_orphanwork_land_all():
    """One serial pass over every row — see orphanwork.land_all."""
    if os.environ.get("VIRA_PASSIVE"):
        raise HTTPException(403, "passive instance — land from the live "
                                 "checkout instead")
    n = orphanwork.land_all()
    return {"started": n > 0, "count": n}


# ---------- contact atlas (the face-graph of interconnection) ----------

@app.get("/api/atlas")
def api_atlas(vault: bool = False):
    """The cached materialized graph — never rebuilt per request.
    `?vault=1` merges the wiki overlay (people beyond the CRM)."""
    return atlas.compose(vault=vault)


class AtlasRefreshReq(BaseModel):
    narrate: bool = False


@app.post("/api/atlas/refresh")
def api_atlas_refresh(req: AtlasRefreshReq | None = None):
    atlas.refresh(narrate=bool(req and req.narrate))
    return {"refreshing": True}


@app.get("/api/atlas/node/{pid}")
def api_atlas_node(pid: str, vault: bool = False):
    detail = atlas.node_detail(pid, vault=vault)
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
                # Attached screenshots ride along, or approving-and-building
                # an idea would hand the circuit the words without the
                # evidence the owner attached to them.
                "plan-build-judge",
                (item["text"] + ideaimages.prompt_block(item)).strip(),
                cwd=req.cwd,
                notify=True, source=f"idea:{idea_id}", idea_id=idea_id)
            ideas.stamp_note(idea_id,
                             f"approved and building (run {run['id'][:10]})")
            out["run"] = run
        except (KeyError, ValueError) as e:
            raise HTTPException(400, f"approved, but build failed: {e}")
    return out


@app.post("/api/ideas/{idea_id}/defer")
def api_idea_defer(idea_id: str):
    """Defer a Vira-proposed idea: proposed -> deferred. Not now, but kept
    — it leaves the queue for Record > Deferred & Dropped, and the muse
    keeps seeing it so tomorrow's proposals do not repeat it."""
    try:
        from datetime import date as _date
        return ideas.stamp_note(idea_id,
                                f"deferred by the owner "
                                f"{_date.today().isoformat()}",
                                status="deferred")
    except KeyError:
        raise HTTPException(404, "unknown idea")


@app.post("/api/ideas/{idea_id}/decline")
def api_idea_decline(idea_id: str):
    try:
        from datetime import date as _date
        return ideas.stamp_note(idea_id,
                                f"declined by the owner "
                                f"{_date.today().isoformat()}",
                                status="dropped")
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
    cfg["sandbox"] = settings.sandboxed()
    # Demo mode stubs the calls that reach the real OS, so what is on screen
    # is partly simulated. That MUST be visible in the badge — an unlabelled
    # simulation is worse than no simulation.
    cfg["demo"] = settings.demo()
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


@app.get("/api/health/loop")
def api_health_loop():
    """Event-loop responsiveness: current lag, every stall recorded since
    boot with the thread stacks caught mid-stall, the CPU admission gate's
    counters, and the background tagger's state.

    This endpoint is the answer to a specific hole. On 2026-07-27 the
    server went dark for stretches of 15 to 90 seconds and left NOTHING
    behind — uvicorn's access log carries no timestamps, so the gap in it
    was unreadable, and diagnosing it took a live reproduction days later.
    A stall now names itself, in the log and here."""
    return {**loopwatch.watcher.snapshot(),
            "idea_indexer": idea_indexer.state()}


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
    openai_cli_model: str | None = None
    openai_api_model: str | None = None
    google_api_model: str | None = None
    xai_api_model: str | None = None
    # The curated picker roster (Cursor-style); [] restores "everything".
    model_roster: list[str] | None = None


@app.post("/api/config")
def api_config_set(req: ConfigReq):
    return suggest.save_config({k: v for k, v in req.model_dump().items()
                                if v is not None})


@app.get("/api/models")
def api_models(refresh: bool = False):
    """The model catalog every picker in the app is built from — per
    provider, per backend, live from the API key where one exists. Cached
    server-side (the probe shells out), so polling it is cheap."""
    return models.options(refresh=refresh)


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

# ---------- Skins (genre-compiled jumping-off points the studio can wear) --
# GET /api/skins lists them; POST /api/skins/{id}/apply rewrites style.css
# :root + skin-active.css, then commits (unless passive). The picker sits at
# the top of the Design Studio module.
app.include_router(skins.router)

# ---------- Genre Studio (build a skin from reference images) --------------
# The instrument behind the picker: references -> aspects -> gain -> knobs ->
# manifest -> skin, with every stage exposed. Engine in genrestudio.py.
app.include_router(genreroutes.router)

# ---------- Image Atlas (the vault's images as a 3D galaxy) ----------------
# chaska adapter: serves the bundled viewer + the vault-sidecar export under
# /imageatlas/, answers query embeddings through localmodels' shared SigLIP.
# Dormant (honest 404/503s) when chaska is absent or no vault is configured.
app.include_router(imageatlasroutes.router)

# ---------- Session walkthroughs (the build films, served in place) --------
# <lab_root>/walkthroughs/ at /walkthroughs/ — the /design precedent. Served
# rather than copied because readinglist's contract is the soft pointer and
# 103MB of film would otherwise have two homes that drift. Dormant without a
# lab_root, which is every install but the owner's.
_wt_root = walkthroughs.root()
if _wt_root:
    app.mount("/walkthroughs", StaticFiles(directory=_wt_root, html=True),
              name="walkthroughs")

app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
