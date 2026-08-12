"""HTTP surface for the resume viewport.

Kept apart from resumeview.py so the engine stays a pure importable module —
every route here is a thin wrapper over one call, plus the role lookup the
Applications module already owns. Paths hang off /api/applications/{uid}/
because a document only exists in the context of the role it was built for.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import applications, resumeview

router = APIRouter()


class TermReq(BaseModel):
    term: str = ""
    note: str = ""
    scope: str = "global"
    kind: str = ""


class LineNoteReq(BaseModel):
    block_id: str = ""
    note: str = ""
    quote: str = ""
    kind: str = ""


class AskReq(BaseModel):
    kind: str = "resume"
    question: str = ""
    block_id: str = ""


class ClaimReq(BaseModel):
    block_id: str = ""
    question: str = ""
    answer: str = ""
    citations: list[str] = []
    quote: str = ""
    kind: str = ""


class FeedbackReq(BaseModel):
    scope: str = "role"
    text: str = ""
    context: str = ""


def _role(uid):
    role = applications.find_role(uid)
    if not role:
        raise HTTPException(404, "unknown role")
    return role


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except resumeview.ViewError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/api/applications/{uid}/document")
def api_document(uid: str, kind: str = "resume"):
    """The document plus its rail in ONE payload: the rail's staleness is a
    fact about this render, so computing it anywhere else would let the two
    disagree about which blocks exist."""
    role = _role(uid)
    doc = _guard(resumeview.document, role, kind)
    doc["annotations"] = resumeview.annotations(uid, doc["blocks"], kind)
    doc["uid"] = uid
    doc["title"] = role.get("title", "")
    doc["company"] = role.get("company", "")
    return doc


@router.get("/api/applications/{uid}/document/file")
def api_document_file(uid: str, name: str = ""):
    path = resumeview.source_path(_role(uid), "resume", name)
    if path is None:
        raise HTTPException(404, "no such file in this package")
    return FileResponse(str(path), headers={"X-Content-Type-Options": "nosniff"})


@router.post("/api/applications/{uid}/term")
def api_term(uid: str, req: TermReq):
    _role(uid)
    return _guard(resumeview.set_term, uid, req.term, req.note, req.scope,
                  req.kind)


@router.delete("/api/applications/{uid}/term/{key}")
def api_term_clear(uid: str, key: str, kind: str = ""):
    _role(uid)
    return _guard(resumeview.clear_term, uid, key, kind)


@router.post("/api/applications/{uid}/line-note")
def api_line_note(uid: str, req: LineNoteReq):
    _role(uid)
    return _guard(resumeview.set_line_note, uid, req.block_id, req.note,
                  req.quote, req.kind)


@router.post("/api/applications/{uid}/ask")
def api_ask(uid: str, req: AskReq):
    role = _role(uid)
    return _guard(resumeview.ask, role, req.kind, req.question, req.block_id)


@router.post("/api/applications/{uid}/claim")
def api_claim(uid: str, req: ClaimReq):
    _role(uid)
    return _guard(resumeview.set_claim, uid, req.block_id, req.question,
                  req.answer, req.citations, req.quote, req.kind)


@router.delete("/api/applications/{uid}/claim/{block_id}")
def api_claim_clear(uid: str, block_id: str, kind: str = ""):
    _role(uid)
    return _guard(resumeview.clear_claim, uid, block_id, kind)


@router.post("/api/applications/{uid}/feedback")
def api_feedback(uid: str, req: FeedbackReq):
    _role(uid)
    return _guard(resumeview.feedback, uid, req.scope, req.text, req.context)
