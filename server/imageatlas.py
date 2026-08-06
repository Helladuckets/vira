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
import re
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
_active: dict = {}          # vid -> {"key": rootstr, "atlas": Atlas}

# The primary vault's id. The viewer reserves "local" (its in-browser BYO
# corpus) and "__byo", so registered ids may never collide with those.
PRIMARY = "primary"
RESERVED_IDS = {PRIMARY, "local", "__byo"}
VAULT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def vault_root() -> Path | None:
    root = settings.get("vault_root")
    if not root:
        return None
    p = Path(root).expanduser()
    return p if p.is_dir() else None


def vaults() -> list[dict]:
    """Every vault the atlas can address: the primary (vault_root) first,
    then the owner-registered `atlas_vaults` config rows. DERIVED on every
    call (the onboard.steps discipline); rows whose directory has vanished
    are kept with exists=False so the UI can say so instead of silently
    shrinking the list."""
    out = []
    root = vault_root()
    if root is not None:
        out.append({"id": PRIMARY, "name": root.name, "root": str(root),
                    "exists": True, "primary": True})
    for row in settings.get("atlas_vaults") or []:
        if not isinstance(row, dict):
            continue
        vid, name, r = str(row.get("id") or ""), str(row.get("name") or ""), str(row.get("root") or "")
        if not vid or not r or vid in RESERVED_IDS:
            continue
        p = Path(r).expanduser()
        out.append({"id": vid, "name": name or p.name, "root": str(p),
                    "exists": p.is_dir(), "primary": False})
    return out


def vault_by_id(vid: str) -> dict | None:
    for v in vaults():
        if v["id"] == vid:
            return v
    return None


def atlas_for(vid: str = PRIMARY):
    """The lazily-built engine object for one vault, re-keyed when config
    changes (the vault.py discipline: settings are re-read on every access)."""
    if _ChaskaAtlas is None:
        return None
    v = vault_by_id(vid)
    if v is None or not v["exists"]:
        return None
    key = v["root"]
    with _lock:
        slot = _active.get(vid)
        if not slot or slot["key"] != key:
            slot = {"key": key,
                    "atlas": _ChaskaAtlas(Path(key), embedder=_ViraEmbedder(),
                                          name=v["name"])}
            _active[vid] = slot
        return slot["atlas"]


def atlas():
    """Back-compat: the primary vault's atlas."""
    return atlas_for(PRIMARY)


def register_vault(name: str, root: str, create: bool = False) -> dict:
    """Add a vault to the registry (and optionally create its skeleton).
    Config is the store (atlas_vaults), written through onboard.config_set —
    the sanctioned identity-key writer. Refused on passive instances: the
    directory creation lands on the real filesystem and the config write
    belongs to the live install."""
    if os.environ.get("VIRA_PASSIVE"):
        raise PermissionError("passive instance — vault registration runs on the live Vira only")
    name = (name or "").strip()
    if not name:
        raise ValueError("a vault needs a name")
    vid = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
    vid = re.sub(r"-+", "-", vid)[:40]
    if not VAULT_ID_RE.match(vid or ""):
        raise ValueError("that name does not reduce to a usable id")
    if vid in RESERVED_IDS or vault_by_id(vid) is not None:
        raise ValueError(f"a vault named '{vid}' already exists")
    p = Path(root).expanduser() if root else (Path.home() / "vaults" / vid)
    rp = vault_root()
    if rp is not None:
        inside = True
        try:
            p.resolve().relative_to(rp.resolve())
        except ValueError:
            inside = False
        if inside:
            raise ValueError("a new vault cannot live inside the primary vault")
    if create:
        if p.exists() and any(p.iterdir()):
            raise ValueError(f"{p} already exists and is not empty")
        (p / "wiki").mkdir(parents=True, exist_ok=True)
        (p / "raw").mkdir(parents=True, exist_ok=True)
    elif not p.is_dir():
        raise ValueError(f"{p} is not a directory")
    from . import onboard
    rows = [r for r in (settings.get("atlas_vaults") or []) if isinstance(r, dict)]
    rows.append({"id": vid, "name": name, "root": str(p)})
    onboard.config_set(atlas_vaults=rows)
    return {"id": vid, "name": name, "root": str(p), "exists": True,
            "primary": False}


# ---------------------------------------------------------------- build ----
# One build at a time, spawned as a child process; the log rides status().

_build = {"proc": None, "log": deque(maxlen=200), "started": 0.0,
          "returncode": None, "vault": ""}
_build_lock = threading.Lock()


def building() -> bool:
    p = _build["proc"]
    return bool(p and p.poll() is None)


def start_build(limit: int | None = None, vault: str = PRIMARY) -> dict:
    if os.environ.get("VIRA_PASSIVE"):
        raise PermissionError("passive instance — builds run on the live Vira only")
    a = atlas_for(vault)
    if a is None:
        raise RuntimeError("image atlas is dormant: " + (dormant_reason() or f"unknown vault '{vault}'"))
    with _build_lock:
        if building():
            return {"started": False, "already": True,
                    "vault": _build["vault"]}
        _build["vault"] = vault
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
        "build_vault": _build["vault"],
        "build_log": list(_build["log"])[-30:],
        "build_returncode": _build["returncode"],
        "vaults": vault_rows(),
    }


def vault_rows() -> list[dict]:
    """The registry annotated with per-vault atlas facts (built = an export
    exists to serve; count from its meta.json when present)."""
    rows = []
    for v in vaults():
        row = dict(v)
        row["built"] = False
        row["count"] = 0
        a = atlas_for(v["id"]) if v["exists"] else None
        if a is not None:
            meta = a.config.export_dir / "meta.json"
            if meta.is_file():
                row["built"] = True
                try:
                    row["count"] = int(json.loads(
                        meta.read_text(encoding="utf-8")).get("count") or 0)
                except (OSError, ValueError):
                    row["count"] = 0
        rows.append(row)
    return rows


# ------------------------------------------------------------- payloads ----

def export_dir(vid: str = PRIMARY) -> Path | None:
    a = atlas_for(vid)
    return a.config.export_dir if a else None


def contained(base: Path, rel: str) -> Path | None:
    try:
        p = (base / rel.lstrip("/")).resolve()
        p.relative_to(base.resolve())
        return p
    except (ValueError, OSError):
        return None


def note_text(rel: str, vid: str = PRIMARY) -> str | None:
    """Markdown of a vault note, containment-checked. None = refuse/missing."""
    v = vault_by_id(vid)
    if v is None or not v["exists"] or not rel.endswith(".md"):
        return None
    p = contained(Path(v["root"]), rel)
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


def viewer_config_get(row: str, vid: str = PRIMARY):
    a = atlas_for(vid)
    if a is None:
        return None
    f = a.config.sidecar / "viewer-config.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return data.get(row)


def viewer_config_put(row: str, content, vid: str = PRIMARY) -> None:
    if os.environ.get("VIRA_PASSIVE"):
        raise PermissionError("passive instance — viewer config writes land in the real vault sidecar")
    a = atlas_for(vid)
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
