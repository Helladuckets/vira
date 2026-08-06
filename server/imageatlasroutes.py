"""HTTP surface for the Image Atlas (chaska adapter).

Kept apart from imageatlas.py so the adapter stays a pure, importable,
testable module — every route here is a thin wrapper over one call. The
/imageatlas/* paths mirror the contract chaska's own local server speaks,
so the bundled viewer works identically under either host: it fetches
everything RELATIVE (api/..., data/..., vendor/...), which resolves under
/imageatlas/ because the viewer is served at the trailing-slash path.
"""
from __future__ import annotations

import base64
import io
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import atlasops, imageatlas

router = APIRouter()


class BuildReq(BaseModel):
    limit: int | None = None
    vault: str = imageatlas.PRIMARY


class EmbedReq(BaseModel):
    text: str = ""
    image: str = ""


class ConfigPutReq(BaseModel):
    content: object = None


class VaultCreateReq(BaseModel):
    name: str
    root: str = ""


class OpsPlanReq(BaseModel):
    src: str = imageatlas.PRIMARY
    paths: list[str]
    dest: str = ""
    new_vault: dict | None = None


class OpsActReq(BaseModel):
    plan_id: str


def _vid(v: str) -> str:
    v = (v or imageatlas.PRIMARY).strip()
    if len(v) > 41 or not v.replace("-", "").isalnum():
        raise HTTPException(400, "bad vault id")
    return v


# ---------------------------------------------------------------- module ---

@router.get("/api/imageatlas/status")
def api_status():
    return imageatlas.status()


@router.post("/api/imageatlas/build")
def api_build(req: BuildReq):
    try:
        return imageatlas.start_build(limit=req.limit, vault=_vid(req.vault))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


# ---------------------------------------------------------------- viewer ---

@router.get("/imageatlas")
def viewer_redirect():
    return RedirectResponse("/imageatlas/", status_code=307)


def _file(p, cache: bool = False):
    if p is None or not p.is_file():
        raise HTTPException(404, "not found")
    headers = {"X-Content-Type-Options": "nosniff"}
    if cache:
        headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return FileResponse(p, headers=headers)


@router.get("/imageatlas/")
def viewer_index():
    if imageatlas.VIEWER_DIR is None:
        raise HTTPException(503, "chaska is not installed")
    return _file(imageatlas.VIEWER_DIR / "index.html")


@router.get("/imageatlas/atlases.json")
def viewer_atlases():
    """One entry per registered vault. The primary keeps base ./data/ (the
    original single-vault contract); the rest serve under ./v/<id>/. An
    unbuilt vault still lists — marked, so the switcher can say why it
    cannot open yet instead of hiding it."""
    rows = imageatlas.vault_rows()
    atlases = []
    for r in rows:
        base = "./data/" if r["primary"] else f"./v/{r['id']}/"
        atlases.append({
            "key": r["id"], "name": r["name"], "base": base,
            "vault": True, "built": r["built"],
            "weight": (f"{r['count']:,} images" if r["built"] else "not built yet"),
            "blurb": ("Built locally from this vault." if r["built"]
                      else "Registered — build its atlas to open it."),
        })
    if not atlases:
        atlases = [{"key": imageatlas.PRIMARY, "name": "Image atlas",
                    "base": "./data/", "vault": True, "built": False}]
    return {"default": imageatlas.PRIMARY, "atlases": atlases}


@router.get("/imageatlas/vendor/{path:path}")
def viewer_vendor(path: str):
    if imageatlas.VIEWER_DIR is None:
        raise HTTPException(503, "chaska is not installed")
    return _file(imageatlas.contained(imageatlas.VIEWER_DIR / "vendor", path),
                 cache=True)


@router.get("/imageatlas/data/{path:path}")
def viewer_data(path: str):
    base = imageatlas.export_dir()
    if base is None:
        raise HTTPException(404, "image atlas is dormant")
    p = imageatlas.contained(base, path)
    return _file(p, cache=p is not None and p.suffix.lower() == ".webp")


@router.get("/imageatlas/v/{vid}/{path:path}")
def viewer_vault_data(vid: str, path: str):
    base = imageatlas.export_dir(_vid(vid))
    if base is None:
        raise HTTPException(404, "no atlas for that vault")
    p = imageatlas.contained(base, path)
    return _file(p, cache=p is not None and p.suffix.lower() == ".webp")


@router.get("/imageatlas/api/me")
def viewer_me():
    return {"admin": True}


# ------------------------------------------------------------ vault ops ----

@router.get("/imageatlas/api/vaults")
def api_vaults():
    return {"vaults": imageatlas.vault_rows(), "ops": True,
            "passive": bool(os.environ.get("VIRA_PASSIVE"))}


@router.post("/imageatlas/api/vaults/create")
def api_vault_create(req: VaultCreateReq):
    try:
        return imageatlas.register_vault(req.name, req.root, create=True)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/imageatlas/api/ops")
def api_ops_recent():
    return {"plans": atlasops.recent()}


@router.post("/imageatlas/api/ops/plan")
def api_ops_plan(req: OpsPlanReq):
    try:
        return atlasops.plan_move(_vid(req.src), req.paths,
                                  dest_vid=_vid(req.dest) if req.dest else "",
                                  new_vault=req.new_vault)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/imageatlas/api/ops/apply")
def api_ops_apply(req: OpsActReq):
    try:
        return atlasops.apply_plan(req.plan_id)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/imageatlas/api/ops/undo")
def api_ops_undo(req: OpsActReq):
    try:
        return atlasops.undo_plan(req.plan_id)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/imageatlas/api/status")
def viewer_status():
    return imageatlas.status()


@router.get("/imageatlas/api/note")
def viewer_note(path: str = "", vault: str = ""):
    text = imageatlas.note_text(path, _vid(vault))
    if text is None:
        raise HTTPException(404, "not found")
    return {"content": text}


@router.get("/imageatlas/api/config/{row}")
def viewer_config_get(row: str, vault: str = ""):
    if len(row) > 64 or not row.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise HTTPException(400, "bad row")
    return {"content": imageatlas.viewer_config_get(row, _vid(vault))}


@router.put("/imageatlas/api/config/{row}")
def viewer_config_put(row: str, req: ConfigPutReq, vault: str = ""):
    if len(row) > 64 or not row.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise HTTPException(400, "bad row")
    try:
        imageatlas.viewer_config_put(row, req.content, _vid(vault))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@router.post("/imageatlas/api/embed")
def viewer_embed(req: EmbedReq):
    text = (req.text or "").strip()
    image_bytes = None
    if not text and req.image.startswith("data:image"):
        if len(req.image) > 12_000_000:
            raise HTTPException(413, "image too large")
        try:
            image_bytes = base64.b64decode(req.image.split(",", 1)[1])
        except (IndexError, ValueError):
            raise HTTPException(400, "bad image")
    if not text and image_bytes is None:
        raise HTTPException(400, "text or image required")
    v = imageatlas.embed_query(text=text, image_bytes=image_bytes)
    if v is None:
        raise HTTPException(503, "no local embedder available")
    return {"vector": [float(x) for x in v]}
