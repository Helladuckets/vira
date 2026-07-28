"""HTTP surface for the Genre Studio.

Kept apart from genrestudio.py so the engine stays a pure, importable, testable
module - every route here is a thin wrapper over one engine call.

There is no install route any more. The studio's deliverable is a genre.json
(GET /{gid}/manifest); turning one into a skin is a downstream consumer's job,
and this surface no longer knows skins exist.
"""
from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
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


class CheckReq(BaseModel):
    on: bool = True


class PatchReq(BaseModel):
    name: str | None = None
    sel: dict | None = None                      # {ref_id: {row: [fragments]}}
    own: dict | None = None                      # {row: [fragments typed by hand]}
    accent: str | None = None                    # a marked swatch; "" clears
    gen_prompt: str | None = None                # owner's words; "" = auto


def _state_or_404(gid: str):
    try:
        return gs.state(gid)
    except FileNotFoundError:
        raise HTTPException(404, "no such genre patch")
    except ValueError as e:
        raise HTTPException(400, str(e))


def update_patch_or_404(gid: str, fn):
    try:
        return gs.update_patch(gid, fn)
    except FileNotFoundError:
        raise HTTPException(404, "no such genre patch")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("")
def list_all():
    return {"patches": gs.list_patches(), "vision": gs.vision_available(),
            "max_refs": gs.MAX_REFS}


@router.post("")
def create(req: NewReq):
    return gs.new_patch(req.name)


@router.get("/{gid}")
def get_state(gid: str):
    return _state_or_404(gid)


@router.delete("/{gid}")
def delete(gid: str):
    try:
        gs.delete_patch(gid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


def _clean_cell(val) -> list:
    """Fragments arrive from the browser, so they are bounded here rather than
    trusted - the same caps the vision pass is held to."""
    out = []
    for v in (val if isinstance(val, list) else []):
        if isinstance(v, (str, int, float)):
            s = str(v).strip()[:gs.MAX_FRAGMENT]
            if s and s not in out:
                out.append(s)
    return out[:gs.MAX_PER_ROW * gs.MAX_REFS]


@router.post("/{gid}/patch")
def patch(gid: str, req: PatchReq):
    """Every edit writes through here. Partial: only the fields present are
    touched, and inside `sel` only the cells named."""
    def fn(p):
        if req.name is not None:
            p["name"] = req.name[:80]
        if req.sel is not None:
            for rid, cells in req.sel.items():
                if not isinstance(cells, dict):
                    continue
                cell = p.setdefault("sel", {}).setdefault(rid, {})
                for key, val in cells.items():
                    cell[key] = _clean_cell(val)
        if req.own is not None:
            for key, val in req.own.items():
                p.setdefault("own", {})[key] = _clean_cell(val)
        if req.accent is not None:
            p["accent"] = req.accent if gs.is_hex(req.accent) else None
        if req.gen_prompt is not None:
            p["gen_prompt"] = req.gen_prompt[:4000]
    update_patch_or_404(gid, fn)
    return _state_or_404(gid)


@router.post("/{gid}/reference")
def add_reference(gid: str, req: RefReq):
    try:
        ref = gs.add_reference(gid, req.data_url, req.name)
    except FileNotFoundError:
        raise HTTPException(404, "no such genre patch")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "ref": ref, "state": _state_or_404(gid)}


@router.delete("/{gid}/reference/{rid}")
def drop_reference(gid: str, rid: str):
    def fn(p):
        p["refs"] = [r for r in p.get("refs") or [] if r.get("id") != rid]
        p.get("sel", {}).pop(rid, None)
    update_patch_or_404(gid, fn)
    return _state_or_404(gid)


@router.post("/{gid}/reference/{rid}/check")
def check_reference(gid: str, rid: str, req: CheckReq):
    """Take everything this image offers, or drop all of it - the sculpting
    gesture that keeps pre-checked fragments cheap to undo."""
    update_patch_or_404(gid, lambda p: gs.check_all(p, rid, req.on))
    return _state_or_404(gid)


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


@router.post("/{gid}/reference/{rid}/read")
def read_one(gid: str, rid: str):
    """Rung 2 - reconstruct one image's prompt and split it into fragments."""
    try:
        out = gs.read_reference(gid, rid)
    except FileNotFoundError:
        raise HTTPException(404, "not found")
    if not out.get("ok"):
        return {"ok": False, "error": out.get("error"), "state": _state_or_404(gid)}
    return {"ok": True, "state": _state_or_404(gid)}


@router.post("/{gid}/read-all")
def read_all(gid: str):
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
                gs.read_reference(gid, rid)
            except Exception:                     # one bad image never stops the rest
                continue
    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "queued": len(ids)}


@router.post("/{gid}/generate")
def generate_image(gid: str, req: GenReq):
    """The combined column's button: compose the recipe into a prompt (or use
    the owner's own words) and render a new image. Synchronous - the UI holds a
    busy state; the take lands in the patch's history."""
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
            "state": _state_or_404(gid)}


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


@router.post("/{gid}/generation/{genid}/promote")
def promote(gid: str, genid: str):
    """Close the loop - a take becomes a reference and is decomposed in turn."""
    try:
        ref = gs.promote_take(gid, genid)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e) or "no such take")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "ref": ref, "state": _state_or_404(gid)}


@router.get("/{gid}/manifest")
def manifest(gid: str):
    """The deliverable, as a downloadable genre.json."""
    try:
        m = gs.export_manifest(gs.load_patch(gid))
    except FileNotFoundError:
        raise HTTPException(404, "no such genre patch")
    except ValueError as e:
        raise HTTPException(400, str(e))
    slug = gs.slugify(m["genre"])
    return JSONResponse(m, headers={
        "Content-Disposition": f'attachment; filename="{slug}.genre.json"'})
