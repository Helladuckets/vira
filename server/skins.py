"""Skins — genre-compiled jumping-off points the Design Studio can wear.

A skin is a complete ``:root`` token set (the colour soul — editable
afterward by the Design Studio's token editor, which reads that same block)
plus an optional glass layer (geometry, type, the tube — things tokens
cannot carry). Applying a skin is a LOCAL, no-git action: it rewrites
``static/style.css``'s ``:root`` in place (values only, comments preserved —
the same conservative line rewrite the studio uses), swaps
``static/skin-active.css`` for the skin's extras, records the choice, and the
app reloads wearing it. Nothing is committed or pushed — a skin is a personal
look on this machine, not a change to the shared repo. (This is why it works
the same on every download: a downloaded copy points at a public repo it
can't push to.) If the owner wants a skin to become the shipped default, that
is a deliberate git commit, not a side effect of a click.

Because a skin leaves the tracked stylesheet modified, the in-app updater
will decline to fast-forward until the skin is reset — applying the Dark Mode
floor restores the pristine files (see update.py, which names this).

Skins ship as tracked data under ``static/skins/<id>.json`` (+ optional
``<id>.css``), so a new skin is added by dropping files in. Dark Mode (the
Taurid Capital skin) is the FLOOR: every apply merges the target skin's
overrides over it, so applying is idempotent and applying it resets Vira to
stock.

The picker lives at the top of the Design Studio module: pick a skin up top,
tweak it in the studio below.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

from . import designstudio, jsonstore

router = APIRouter(prefix="/api/skins")

_SWATCH_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$|^(?:rgb|rgba|hsl|hsla)\([0-9 ,.%/]+\)$")

APP_ROOT = Path(__file__).resolve().parents[1]
SKINS_DIR = APP_ROOT / "static" / "skins"
STYLE_CSS = APP_ROOT / "static" / "style.css"
SKIN_ACTIVE = APP_ROOT / "static" / "skin-active.css"
STATE = APP_ROOT / "data" / "active-skin.json"   # per-instance (data/ is ignored)

FLOOR_ID = "taurid"                              # the base every apply resets to
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
EXTRAS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}\.css$")
MAX_MANIFEST_BYTES = 128 * 1024
MAX_EXTRAS_BYTES = designstudio.MAX_FILE_BYTES   # 512 KB, same ceiling as a saved stylesheet

# The default (glass-free) content of skin-active.css. Applying Dark Mode
# floor writes exactly this, so a reset-to-stock leaves no git diff. Kept in
# sync with the shipped static/skin-active.css. Comments only, no rules.
_ACTIVE_HEADER = (
    "/* skin-active.css — the glass layer of the currently applied skin.\n"
    "   Linked after style.css; the Design Studio's skins picker overwrites\n"
    "   this file when a skin is applied. The default skin (Dark Mode)\n"
    "   carries no glass, so until a skin is applied this file has comments\n"
    "   only, no rules. */\n"
)


# ---------------------------------------------------------------- manifests

def _manifest_path(skin_id: str) -> Path:
    if not ID_RE.match(skin_id or ""):
        raise HTTPException(400, "bad skin id")
    p = (SKINS_DIR / f"{skin_id}.json").resolve()
    # ID_RE already forbids separators, but resolve-and-contain is the belt.
    if SKINS_DIR.resolve() not in p.parents:
        raise HTTPException(400, "bad skin id")
    return p


def _check_token(key: str, value) -> None:
    name = key[2:] if key.startswith("--") else key
    if not designstudio.TOKEN_RE.match(name):
        raise ValueError(f"bad token name: {key!r}")
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ValueError(f"bad value for {key}")
    if designstudio.VALUE_BAD.search(value):
        raise ValueError(f"illegal characters in value for {key}")
    # a value carrying a CSS comment sequence would comment out the following
    # :root declarations when written in place — reject it (this also hardens
    # the shared value brake the studio save path relies on).
    if "/*" in value or "*/" in value:
        raise ValueError(f"comment sequence in value for {key}")


def load_manifest(skin_id: str) -> dict:
    """Load + validate a skin manifest. Manifests are trusted (tracked) data;
    the validation catches authoring errors and is defence in depth for the
    values that reach the stylesheet."""
    p = _manifest_path(skin_id)
    if not p.is_file():
        raise HTTPException(404, f"no such skin: {skin_id}")
    if p.stat().st_size > MAX_MANIFEST_BYTES:
        raise HTTPException(500, f"skin manifest too large: {skin_id}")
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise HTTPException(500, f"unreadable skin manifest: {e}")
    if not isinstance(m, dict) or m.get("id") != skin_id:
        raise HTTPException(500, f"skin manifest id mismatch: {skin_id}")
    tokens = m.get("tokens") or {}
    if not isinstance(tokens, dict) or not tokens:
        raise HTTPException(500, f"skin {skin_id} has no tokens")
    try:
        for k, v in tokens.items():
            _check_token(k, v)
    except ValueError as e:
        raise HTTPException(500, str(e))
    extras = m.get("extras")
    if extras is not None and not EXTRAS_RE.match(str(extras)):
        raise HTTPException(500, f"skin {skin_id} has a bad extras name")
    return m


def _meta(m: dict) -> dict:
    """The listing shape — everything a card needs, minus the full token map."""
    return {
        "id": m["id"],
        "name": m.get("name", m["id"]),
        "genre": m.get("genre"),
        "register": m.get("register"),
        "medium": m.get("medium"),
        "tagline": m.get("tagline"),
        "description": m.get("description"),
        "swatch": [c for c in (m.get("swatch") or [])
                   if isinstance(c, str) and _SWATCH_RE.match(c)][:8],
        "source": m.get("source"),
        "covenants": m.get("covenants") or [],
        "default": bool(m.get("default")),
        "has_glass": bool(m.get("extras")),
    }


def active_id() -> str:
    s = jsonstore.read(STATE, {"id": FLOOR_ID})
    got = s.get("id") if isinstance(s, dict) else None
    return got if isinstance(got, str) and ID_RE.match(got) else FLOOR_ID


def list_skins() -> dict:
    """Every skin on disk, floor/default first, then the rest by name."""
    metas = []
    for p in sorted(SKINS_DIR.glob("*.json")):
        skin_id = p.stem
        if not ID_RE.match(skin_id):
            continue
        try:
            metas.append(_meta(load_manifest(skin_id)))
        except HTTPException:
            continue                              # skip a malformed file, list the rest
    metas.sort(key=lambda m: (not m["default"], m["name"].lower()))
    return {"active": active_id(), "skins": metas}


# ---------------------------------------------------------------- apply

def _rewrite_root(text: str, tokens: dict) -> str:
    """Replace the value of each named token inside the plain ``:root`` block,
    preserving the trailing ``;`` and any brand-book comment on the line —
    the same conservative rewrite the Design Studio's save path uses."""
    lines = text.split("\n")
    open_i, close_i = designstudio._root_block(lines)
    line_re = re.compile(r"^(\s*--)([a-z0-9-]+)(\s*:\s*)([^;]+)(;.*)$")
    for i in range(open_i, close_i + 1):
        m = line_re.match(lines[i])
        if not m:
            continue
        key = "--" + m.group(2)
        if key in tokens:
            lines[i] = m.group(1) + m.group(2) + m.group(3) + tokens[key].strip() + m.group(5)
    return "\n".join(lines)


def _extras_css(m: dict) -> str:
    name = m.get("extras")
    if not name:
        return _ACTIVE_HEADER
    p = (SKINS_DIR / name).resolve()
    if SKINS_DIR.resolve() not in p.parents or not p.is_file():
        raise HTTPException(500, f"skin extras missing: {name}")
    if p.stat().st_size > MAX_EXTRAS_BYTES:
        raise HTTPException(500, f"skin extras too large: {name}")
    return p.read_text(encoding="utf-8")


def _write_text_atomic(path: Path, text: str) -> None:
    """tmp + rename so a hard kill mid-write can never leave a truncated
    tracked stylesheet (which the next successful apply would commit)."""
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def apply_skin(skin_id: str) -> dict:
    """Rewrite :root to this skin (floor + overrides), swap the glass layer,
    record the choice. A LOCAL action — no git; the app reloads wearing it.
    Writes are atomic so a hard kill can never leave a truncated stylesheet."""
    skin = load_manifest(skin_id)
    floor = load_manifest(FLOOR_ID)
    merged = dict(floor["tokens"])
    merged.update(skin.get("tokens") or {})
    for k, v in merged.items():                   # revalidate the merged surface
        try:
            _check_token(k, v)
        except ValueError as e:
            raise HTTPException(500, str(e))

    if not STYLE_CSS.is_file():
        raise HTTPException(500, "style.css not found")
    text = STYLE_CSS.read_text(encoding="utf-8")
    new_css = _rewrite_root(text, merged)
    extras = _extras_css(skin)

    changed: list[str] = []
    if new_css != text:
        _write_text_atomic(STYLE_CSS, new_css)
        changed.append("static/style.css")
    if not SKIN_ACTIVE.is_file() or SKIN_ACTIVE.read_text(encoding="utf-8") != extras:
        _write_text_atomic(SKIN_ACTIVE, extras)
        changed.append("static/skin-active.css")

    jsonstore.write_atomic(STATE, {"id": skin_id})

    # No git: a skin is a personal, local look. It takes effect on the reload
    # the client triggers next. Persisting a skin as the shipped default is a
    # deliberate commit the owner makes, never a side effect of applying one.
    return {"ok": True, "active": skin_id, "changed": changed}


# ---------------------------------------------------------------- routes

@router.get("")
def get_skins():
    return list_skins()


@router.get("/{skin_id}")
def get_one(skin_id: str):
    m = load_manifest(skin_id)
    out = _meta(m)
    out["active"] = active_id() == m["id"]
    return out


@router.post("/{skin_id}/apply")
def post_apply(skin_id: str):
    return apply_skin(skin_id)
