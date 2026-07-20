"""Design Studio — Vira hosts the design-foundation repo's studio page
(/design/studio.html, served by the static mount in main.py) and provides
the save power the standalone page lacks: POST /api/design/save rewrites
token values inside themes/<theme>/theme.css in place, commits with a
message naming the changed tokens, and pushes. The repo path comes from
config ``design_foundation_root``; when the directory is missing the
studio is dormant (mount skipped, save 404s).

The rewrite is line-based and conservative: only the value between
``--token:`` and the first ``;`` changes, so trailing comments (the
brand-book annotations) survive every save. Tokens the theme has not
overridden yet are appended inside the theme block under a
``/* -- studio additions -- */`` marker.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import settings

router = APIRouter(prefix="/api/design")

TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
THEME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
VALUE_BAD = re.compile(r"[;{}<>\\\n\r]")
ADD_MARK = "  /* -- studio additions -- */"
MAX_CHANGES = 200
MAX_FILE_BYTES = 512 * 1024

# whole-file writes are limited to the known editable set; the theme
# path is validated separately against the request's theme name
FOUNDATION_PATHS = {
    "foundation/tokens.css",
    "foundation/base.css",
    "foundation/components.css",
}

# the "vira" target edits THIS checkout's stylesheet — the studio designs
# against the real app it is served by, and saves commit to its repo
APP_ROOT = Path(__file__).resolve().parents[1]
VIRA_PATHS = {"static/style.css"}


def root() -> Path:
    return Path(settings.get("design_foundation_root")).expanduser()


# ---------------------------------------------------------------- rewrite

def _theme_block(lines: list[str], theme: str) -> tuple[int, int]:
    """(open, close) line indexes of the :root[data-theme="<theme>"] block."""
    open_re = re.compile(r'^:root\[data-theme="?' + re.escape(theme) + r'"?\]')
    for i, line in enumerate(lines):
        if open_re.match(line):
            depth = 0
            for j in range(i, len(lines)):
                depth += lines[j].count("{") - lines[j].count("}")
                if depth == 0 and j > i:
                    return i, j
            break
    raise ValueError(f"theme block for {theme!r} not found")


def validate_changes(theme: str, changes: dict) -> None:
    if not THEME_RE.match(theme or ""):
        raise ValueError("bad theme name")
    if not changes or not isinstance(changes, dict):
        raise ValueError("no changes")
    if len(changes) > MAX_CHANGES:
        raise ValueError("too many changes")
    for name, value in changes.items():
        if not TOKEN_RE.match(name or ""):
            raise ValueError(f"bad token name: {name!r}")
        if not isinstance(value, str) or not value.strip() or len(value) > 300:
            raise ValueError(f"bad value for --{name}")
        if VALUE_BAD.search(value):
            raise ValueError(f"illegal characters in value for --{name}")


def rewrite_theme(text: str, theme: str, changes: dict) -> str:
    """Apply token changes inside the theme block; preserve everything else."""
    lines = text.split("\n")
    open_i, close_i = _theme_block(lines, theme)
    existing: dict[str, int] = {}
    for i in range(open_i, close_i + 1):
        m = re.match(r"^\s*--([a-z0-9-]+)\s*:", lines[i])
        if m:
            existing[m.group(1)] = i
    additions: list[str] = []
    for name, value in changes.items():
        if name in existing:
            i = existing[name]
            lines[i] = re.sub(r"(--" + re.escape(name) + r"\s*:\s*)[^;]+;",
                              lambda m: m.group(1) + value.strip() + ";",
                              lines[i], count=1)
        else:
            additions.append(f"  --{name}: {value.strip()};")
    if additions:
        block = "\n".join(lines[open_i:close_i])
        if ADD_MARK.strip() not in block:
            additions = ["", ADD_MARK] + additions
        lines[close_i:close_i] = additions
    return "\n".join(lines)


# ---------------------------------------------------------------- git

def _git(repo: Path, *args: str, timeout: int = 25) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=timeout)


def commit_and_push(repo: Path, relpath: str | list[str], message: str) -> dict:
    paths = [relpath] if isinstance(relpath, str) else list(relpath)
    _git(repo, "add", *paths)
    commit = _git(repo, "commit", "-m", message)
    if commit.returncode != 0:
        if "nothing to commit" in (commit.stdout + commit.stderr):
            return {"committed": False, "sha": None, "pushed": False}
        raise RuntimeError(commit.stderr.strip() or "git commit failed")
    sha = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    pushed = False
    try:
        pushed = _git(repo, "push", timeout=30).returncode == 0
    except subprocess.TimeoutExpired:
        pushed = False
    return {"committed": True, "sha": sha, "pushed": pushed}


def commit_message(theme: str, names: list[str]) -> str:
    shown = ", ".join(names[:6])
    extra = f" +{len(names) - 6} more" if len(names) > 6 else ""
    return f"{theme}: adjust {shown}{extra} (via Design Studio)"


# ---------------------------------------------------------------- files path

def validate_files(theme: str, files: dict, target: str = "foundation") -> None:
    if not THEME_RE.match(theme or ""):
        raise ValueError("bad theme name")
    if not files or not isinstance(files, dict):
        raise ValueError("no files")
    if target == "vira":
        allowed = set(VIRA_PATHS)
    elif target == "foundation":
        allowed = FOUNDATION_PATHS | {f"themes/{theme}/theme.css"}
    else:
        raise ValueError(f"unknown target: {target!r}")
    for path, text in files.items():
        if path not in allowed:
            raise ValueError(f"path not editable: {path!r}")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"empty content for {path}")
        if len(text.encode()) > MAX_FILE_BYTES:
            raise ValueError(f"{path} too large")
        if "\x00" in text:
            raise ValueError(f"binary content in {path}")


def _root_block(lines: list[str]) -> tuple[int, int]:
    """(open, close) of a plain :root { } block (untargeted apps)."""
    open_re = re.compile(r"^:root\s*{")
    for i, line in enumerate(lines):
        if open_re.match(line):
            depth = 0
            for j in range(i, len(lines)):
                depth += lines[j].count("{") - lines[j].count("}")
                if depth == 0 and j > i:
                    return i, j
            break
    raise ValueError(":root block not found")


def _block_tokens(text: str, theme: str | None) -> dict:
    try:
        lines = text.split("\n")
        if theme:
            open_i, close_i = _theme_block(lines, theme)
        else:
            open_i, close_i = _root_block(lines)
    except ValueError:
        return {}
    out = {}
    for i in range(open_i, close_i + 1):
        m = re.match(r"^\s*--([a-z0-9-]+)\s*:\s*([^;]+);", lines[i])
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _token_diff_names(old: dict, new: dict) -> list[str]:
    return sorted(n for n in (old.keys() | new.keys()) if old.get(n) != new.get(n))


def files_commit_message(theme: str, repo: Path, files: dict,
                         target: str = "foundation") -> str:
    """Name the commit by what actually changed: token diffs of the target's
    token block when its file is in the batch, file basenames otherwise."""
    if target == "vira":
        rel = "static/style.css"
        prefix = "design"
        block_theme = None
    else:
        rel = f"themes/{theme}/theme.css"
        prefix = theme
        block_theme = theme
    names: list[str] = []
    if rel in files:
        old = _block_tokens((repo / rel).read_text(), block_theme) \
            if (repo / rel).is_file() else {}
        names = _token_diff_names(old, _block_tokens(files[rel], block_theme))
    others = sorted(Path(p).name for p in files if p != rel)
    if names and not others:
        shown = ", ".join(names[:6])
        extra = f" +{len(names) - 6} more" if len(names) > 6 else ""
        return f"{prefix}: adjust {shown}{extra} (via Design Studio)"
    parts = []
    if names:
        parts.append("adjust " + ", ".join(names[:4]) +
                     (f" +{len(names) - 4} more" if len(names) > 4 else ""))
    if others:
        parts.append("edit " + ", ".join(others))
    body = "; ".join(parts) if parts else "edit theme"
    return f"{prefix}: {body} (via Design Studio)"


# ---------------------------------------------------------------- routes

class SaveReq(BaseModel):
    theme: str = "taurid"
    target: str = "foundation"              # foundation | vira (this checkout)
    changes: dict[str, str] | None = None   # token-level path (API/tests)
    files: dict[str, str] | None = None     # whole-file path (the studio editor)


@router.post("/save")
def save(req: SaveReq):
    repo = APP_ROOT if req.target == "vira" else root()
    if not repo.is_dir():
        raise HTTPException(404, "target repo not found")
    if req.files:
        return _save_files(repo, req.theme, req.files, req.target)
    if req.changes:
        if req.target != "foundation":
            raise HTTPException(400, "token-level changes only for foundation")
        return _save_changes(repo, req.theme, req.changes)
    raise HTTPException(400, "nothing to save")


def _save_files(repo: Path, theme: str, files: dict, target: str = "foundation"):
    try:
        validate_files(theme, files, target)
    except ValueError as e:
        raise HTTPException(400, str(e))
    message = files_commit_message(theme, repo, files, target)
    wrote = []
    for rel, text in files.items():
        path = repo / rel
        if not path.is_file():
            raise HTTPException(404, f"{rel} not found")
        if path.read_text() != text:
            path.write_text(text)
            wrote.append(rel)
    if not wrote:
        return {"ok": True, "committed": False, "note": "no changes"}
    try:
        out = commit_and_push(repo, wrote, message)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        raise HTTPException(500, f"saved to disk but git failed: {e}")
    return {"ok": True, **out}


def _save_changes(repo: Path, theme: str, changes: dict):
    try:
        validate_changes(theme, changes)
    except ValueError as e:
        raise HTTPException(400, str(e))
    rel = f"themes/{theme}/theme.css"
    path = repo / rel
    if not path.is_file():
        raise HTTPException(404, f"{rel} not found")
    text = path.read_text()
    try:
        new = rewrite_theme(text, theme, changes)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if new == text:
        return {"ok": True, "committed": False, "note": "no changes"}
    path.write_text(new)
    try:
        out = commit_and_push(repo, rel, commit_message(theme, sorted(changes)))
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        raise HTTPException(500, f"saved to disk but git failed: {e}")
    return {"ok": True, **out}


@router.get("/history")
def history(theme: str = "taurid", target: str = "foundation"):
    if target == "vira":
        repo, rel = APP_ROOT, "static/style.css"
    else:
        repo, rel = root(), f"themes/{theme}/theme.css"
    if not repo.is_dir():
        raise HTTPException(404, "target repo not found")
    log = _git(repo, "log", "--oneline", "-12", "--", rel)
    rows = [line.split(" ", 1) for line in log.stdout.strip().split("\n") if line]
    return {"history": [{"sha": r[0], "message": r[1] if len(r) > 1 else ""} for r in rows]}
