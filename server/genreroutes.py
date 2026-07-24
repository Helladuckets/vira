"""HTTP surface for the Genre Studio.

Kept apart from genrestudio.py so the engine stays a pure, importable,
testable module — every route here is a thin wrapper over one engine call.
"""
from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import genregen, genrestudio as gs

router = APIRouter(prefix="/api/genre")


class NewReq(BaseModel):
    name: str = ""


class RefReq(BaseModel):
    data_url: str
    name: str = ""


class GenReq(BaseModel):
    aspect: str = "4:3"


class PatchReq(BaseModel):
    name: str | None = None
    knobs: dict | None = None
    sel: dict | None = None
    picks: dict | None = None
    overrides: dict | None = None
    weights: dict[str, float] | None = None      # {ref_id: gain}
    gen_prompt: str | None = None                # owner's words; "" = auto


@router.get("")
def list_all():
    return {"patches": gs.list_patches(), "vision": gs.vision_available(),
            "max_refs": gs.MAX_REFS}


@router.post("")
def create(req: NewReq):
    return gs.new_patch(req.name)


@router.get("/{gid}")
def get_state(gid: str):
    try:
        return gs.state(gid)
    except FileNotFoundError:
        raise HTTPException(404, "no such genre patch")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/{gid}")
def delete(gid: str):
    try:
        gs.delete_patch(gid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/{gid}/patch")
def patch(gid: str, req: PatchReq):
    """Every dial writes through here — knob positions, per-cell selection,
    manual conflict picks, direct edits of the resolved column, and per-
    reference gain. Partial: only the fields present are touched."""
    def fn(p):
        if req.name is not None:
            p["name"] = req.name[:80]
        if req.knobs:
            p.setdefault("knobs", {}).update(
                {k: v for k, v in req.knobs.items() if k in gs.KNOB_DEFAULTS})
        if req.sel is not None:
            for rid, cells in req.sel.items():
                p.setdefault("sel", {}).setdefault(rid, {}).update(cells)
        if req.picks is not None:
            p.setdefault("picks", {}).update(req.picks)
            for k, v in list(req.picks.items()):
                if v is None:
                    p["picks"].pop(k, None)
        if req.overrides is not None:
            p.setdefault("overrides", {}).update(req.overrides)
            for k, v in list(req.overrides.items()):
                if v is None:
                    p["overrides"].pop(k, None)
        if req.weights:
            for r in p.get("refs") or []:
                if r["id"] in req.weights:
                    r["weight"] = max(0.0, min(1.0, float(req.weights[r["id"]])))
        if req.gen_prompt is not None:
            p["gen_prompt"] = req.gen_prompt[:4000]
    try:
        update_patch_or_404(gid, fn)
        return gs.state(gid)
    except ValueError as e:
        raise HTTPException(400, str(e))


def update_patch_or_404(gid: str, fn):
    try:
        return gs.update_patch(gid, fn)
    except FileNotFoundError:
        raise HTTPException(404, "no such genre patch")


@router.post("/{gid}/reference")
def add_reference(gid: str, req: RefReq):
    try:
        ref = gs.add_reference(gid, req.data_url, req.name)
    except FileNotFoundError:
        raise HTTPException(404, "no such genre patch")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "ref": ref, "state": gs.state(gid)}


@router.delete("/{gid}/reference/{rid}")
def drop_reference(gid: str, rid: str):
    def fn(p):
        p["refs"] = [r for r in p.get("refs") or [] if r.get("id") != rid]
        p.get("sel", {}).pop(rid, None)
    update_patch_or_404(gid, fn)
    return gs.state(gid)


@router.get("/{gid}/reference/{rid}/image")
def ref_image(gid: str, rid: str):
    try:
        patch = gs.load_patch(gid)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "no such genre patch")
    ref = next((r for r in patch.get("refs") or [] if r.get("id") == rid), None)
    if not ref:
        raise HTTPException(404, "no such reference")
    path = Path(gs._patch_dir(gid)) / ref["file"]
    if not path.is_file():
        raise HTTPException(404, "image missing")
    return FileResponse(path)


@router.post("/{gid}/reference/{rid}/enrich")
def enrich(gid: str, rid: str):
    """Rung 2 — the optional vision pass for one reference."""
    try:
        out = gs.enrich_reference(gid, rid)
    except FileNotFoundError:
        raise HTTPException(404, "not found")
    if not out.get("ok"):
        return {"ok": False, "error": out.get("error"), "state": gs.state(gid)}
    return {"ok": True, "state": gs.state(gid)}


@router.post("/{gid}/enrich-all")
def enrich_all(gid: str):
    """Fire rung 2 across every reference in the background; the client polls
    state. One model call per image, so this is the expensive button."""
    try:
        patch = gs.load_patch(gid)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "no such genre patch")
    ids = [r["id"] for r in patch.get("refs") or []]

    def work():
        for rid in ids:
            try:
                gs.enrich_reference(gid, rid)
            except Exception:                     # one bad image never stops the rest
                continue
    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "queued": len(ids)}


@router.post("/{gid}/generate")
def generate_image(gid: str, req: GenReq):
    """The combined column's button: compose the recipe into a prompt (or use
    the owner's own words) and render a NEW reference image. Synchronous — the
    UI holds a busy state; the take lands in the patch's generation history."""
    try:
        patch = gs.load_patch(gid)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "no such genre patch")
    prompt = genregen.compose_prompt(patch)
    try:
        png = genregen.generate(prompt, aspect=req.aspect)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    entry = gs.record_generation(gid, prompt, png)
    return {"ok": True, "generation": entry, "prompt": prompt,
            "state": gs.state(gid)}


@router.get("/{gid}/generation/{genid}/image")
def generation_image(gid: str, genid: str):
    try:
        patch = gs.load_patch(gid)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "no such genre patch")
    entry = next((g for g in patch.get("generations") or []
                  if g.get("id") == genid), None)
    if not entry:
        raise HTTPException(404, "no such generation")
    path = Path(gs._patch_dir(gid)) / entry["file"]
    if not path.is_file():
        raise HTTPException(404, "image missing")
    return FileResponse(path)


@router.post("/{gid}/install")
def install(gid: str):
    """Write the patch out as a real skin so it joins the picker."""
    try:
        return gs.install(gid)
    except FileNotFoundError:
        raise HTTPException(404, "no such genre patch")
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
