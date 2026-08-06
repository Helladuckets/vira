"""Image Atlas — the vault's images as a navigable 3D galaxy (chaska adapter).

chaska (~/workspace/chaska, the qocha pattern) is the standalone local-first
engine: scan a vault for images, embed them ON THIS MACHINE, project into
2D/3D coordinates, cluster + label, export the payload, ship the WebGL
viewer. This module is Vira's thin adapter over it — the vault.py role, and
the same rules:

- Engine changes belong in the chaska repo, not here.
- The embedder is INJECTED: Vira routes image/text embedding through its own
  ``localmodels`` SigLIP functions, so the model instance (and the torch-MPS
  ``_infer_lock`` discipline) is shared with mediaindex rather than loaded a
  second time.
- The sidecar lives in the VAULT (``<vault>/.chaska``), chaska's own default,
  so a CLI build (``chaska build ~/TC-IL``) and this module see one atlas.
  That also means the sidecar sits OUTSIDE a test clone's ``data/`` — so a
  passive instance may READ it but never build or write viewer config into
  it (the plans.py precedent).
- Builds run OUT OF PROCESS (``python -m chaska build``): the projection is
  minutes of pure-numpy work whose scatter-adds hold the GIL, exactly the
  class of CPU that starves the event loop (see admission.py). A child
  process has its own GIL, so a build can never stall a request.

Dormant when chaska is not importable or no ``vault_root`` is configured —
``status()`` names which.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import os

from . import settings

try:
    from chaska import Atlas as _ChaskaAtlas
    from chaska.serve import VIEWER_DIR
    CHASKA_ERR = ""
except Exception as e:                      # dormant, never fatal
    _ChaskaAtlas = None
    VIEWER_DIR = None
    CHASKA_ERR = str(e)


class _ViraEmbedder:
    """chaska's embedder protocol over Vira's own local models. Late imports
    are the mock seam for tests, and keep torch out of module import."""

    model_id = "siglip2 (vira localmodels)"

    def embed_images(self, paths):
        try:
            from . import localmodels
            return localmodels.siglip_embed_images([str(p) for p in paths])
        except Exception:
            return None                     # backend down = pause, never raise

    def embed_text(self, text: str):
        try:
            from . import localmodels
            return localmodels.siglip_embed_text(text)
        except Exception:
            return None


_lock = threading.Lock()
_active: dict = {"key": None, "atlas": None}


def vault_root() -> Path | None:
    root = settings.get("vault_root")
    if not root:
        return None
    p = Path(root).expanduser()
    return p if p.is_dir() else None


def atlas():
    """The lazily-built engine object, re-keyed when config changes (the
    vault.py discipline: settings are re-read on every access)."""
    if _ChaskaAtlas is None:
        return None
    root = vault_root()
    if root is None:
        return None
    key = str(root)
    with _lock:
        if _active["key"] != key:
            _active["atlas"] = _ChaskaAtlas(root, embedder=_ViraEmbedder(),
                                            name=root.name)
            _active["key"] = key
        return _active["atlas"]


# ---------------------------------------------------------------- build ----
# One build at a time, spawned as a child process; the log rides status().

_build = {"proc": None, "log": deque(maxlen=200), "started": 0.0,
          "returncode": None}
_build_lock = threading.Lock()


def building() -> bool:
    p = _build["proc"]
    return bool(p and p.poll() is None)


def start_build(limit: int | None = None) -> dict:
    if os.environ.get("VIRA_PASSIVE"):
        raise PermissionError("passive instance — builds run on the live Vira only")
    a = atlas()
    if a is None:
        raise RuntimeError("image atlas is dormant: " + dormant_reason())
    with _build_lock:
        if building():
            return {"started": False, "already": True}
        argv = [sys.executable, "-m", "chaska", "build", str(a.config.root)]
        if limit:
            argv += ["--limit", str(int(limit))]
        _build["log"].clear()
        _build["returncode"] = None
        _build["started"] = time.time()
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        _build["proc"] = proc
        threading.Thread(target=_tail, args=(proc,), daemon=True,
                         name="vira-imageatlas-build").start()
        return {"started": True}


def _tail(proc) -> None:
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                _build["log"].append(line)
    finally:
        proc.wait()
        _build["returncode"] = proc.returncode
        _build["log"].append(f"[build] exited {proc.returncode}")


# --------------------------------------------------------------- status ----

def dormant_reason() -> str:
    if _ChaskaAtlas is None:
        return f"chaska is not installed ({CHASKA_ERR or 'import failed'})"
    if vault_root() is None:
        return "no vault_root configured (Config > Your data > Brain)"
    return ""


def status() -> dict:
    reason = dormant_reason()
    if reason:
        return {"available": False, "reason": reason, "building": False}
    a = atlas()
    try:
        st = a.status()
    except Exception as e:
        return {"available": False, "reason": f"engine error: {e}",
                "building": building()}
    return {
        "available": True,
        **st,
        "building": building(),
        "build_log": list(_build["log"])[-30:],
        "build_returncode": _build["returncode"],
    }


# ------------------------------------------------------------- payloads ----

def export_dir() -> Path | None:
    a = atlas()
    return a.config.export_dir if a else None


def contained(base: Path, rel: str) -> Path | None:
    try:
        p = (base / rel.lstrip("/")).resolve()
        p.relative_to(base.resolve())
        return p
    except (ValueError, OSError):
        return None


def note_text(rel: str) -> str | None:
    """Markdown of a vault note, containment-checked. None = refuse/missing."""
    root = vault_root()
    if root is None or not rel.endswith(".md"):
        return None
    p = contained(root, rel)
    if p is None or not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


# viewer overlay config (renamed labels, hidden images, custom clusters) —
# stored in the SIDECAR's viewer-config.json so the chaska CLI server and
# Vira serve one truth. Writes refuse on passive: the sidecar is the real
# vault's, not the clone's.

_cfg_lock = threading.Lock()


def viewer_config_get(row: str):
    a = atlas()
    if a is None:
        return None
    f = a.config.sidecar / "viewer-config.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return data.get(row)


def viewer_config_put(row: str, content) -> None:
    if os.environ.get("VIRA_PASSIVE"):
        raise PermissionError("passive instance — viewer config writes land in the real vault sidecar")
    a = atlas()
    if a is None:
        raise RuntimeError("image atlas is dormant")
    f = a.config.sidecar / "viewer-config.json"
    with _cfg_lock:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[row] = content
        a.config.sidecar.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)


# ---------------------------------------------------------------- query ----

def embed_query(text: str = "", image_bytes: bytes | None = None):
    """A query vector, or None when no backend/atlas. Text queries embed
    through the shared SigLIP instance (the _infer_lock lives inside
    localmodels, so this is safe beside mediaindex's scene tick)."""
    a = atlas()
    if a is None:
        return None
    if text:
        return a.embed_query_text(text[:600])
    if image_bytes:
        import io
        emb = a.embedder.embed_images([io.BytesIO(image_bytes)])
        return emb[0] if emb else None
    return None
