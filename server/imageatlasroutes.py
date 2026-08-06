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

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import imageatlas

router = APIRouter()


class BuildReq(BaseModel):
    limit: int | None = None


class EmbedReq(BaseModel):
    text: str = ""
    image: str = ""


class ConfigPutReq(BaseModel):
    content: object = None


# ---------------------------------------------------------------- module ---

@router.get("/api/imageatlas/status")
def api_status():
    return imageatlas.status()


@router.post("/api/imageatlas/build")
def api_build(req: BuildReq):
    try:
        return imageatlas.start_build(limit=req.limit)
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
    a = imageatlas.atlas()
    name = a.config.name or a.config.root.name if a else "Image atlas"
    return {
        "default": "local",
        "atlases": [{"key": "local", "name": name, "base": "./data/",
                     "blurb": "Built locally from this vault."}],
    }


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


@router.get("/imageatlas/api/me")
def viewer_me():
    return {"admin": True}


@router.get("/imageatlas/api/status")
def viewer_status():
    return imageatlas.status()


@router.get("/imageatlas/api/note")
def viewer_note(path: str = ""):
    text = imageatlas.note_text(path)
    if text is None:
        raise HTTPException(404, "not found")
    return {"content": text}


@router.get("/imageatlas/api/config/{row}")
def viewer_config_get(row: str):
    if len(row) > 64 or not row.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise HTTPException(400, "bad row")
    return {"content": imageatlas.viewer_config_get(row)}


@router.put("/imageatlas/api/config/{row}")
def viewer_config_put(row: str, req: ConfigPutReq):
    if len(row) > 64 or not row.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise HTTPException(400, "bad row")
    try:
        imageatlas.viewer_config_put(row, req.content)
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
