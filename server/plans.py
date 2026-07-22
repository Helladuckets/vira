"""Saved plans — the durable vault home + registry for Plan-mode output.

A Plan-mode idea or action produces plan markdown. On the owner's own
machine Vira ALSO publishes it to a hosted lab page via a private
~/.claude/scripts/plan-html-deploy.py hook; on every other install that
hook is absent and the publish silently no-ops. This module is the
universal home every install shares: each plan is saved as a markdown
note in the vault — creating a Vira vault at ~/.vira/vault when none is
connected, so a first plan is what STARTS the vault — and recorded in a
small registry so it keeps a stable id + name, opens in an in-app viewer,
and stays reachable long after the job terminal is gone.

Registry entry shape (data/plans.json):
  { "id": "pl_<hex>", "title": str, "path": <absolute .md path>,
    "created": ISO8601, "idea_id": str|None, "job_id": str|None,
    "lab_url": str }   # lab_url set only where the private hook published

The registry is REGENERABLE in shape (the plan files under <vault>/plans
are the real content) but canonical in role, so writes are atomic
(tmp+rename) and serialized through the cross-process filelock — a
detached job runner calls save_plan from its own process.
"""
import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from . import settings
from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
REG_PATH = ROOT / "data" / "plans.json"
PLANS_SUBDIR = "plans"                       # <vault>/plans/<file>.md
DEFAULT_VAULT = Path.home() / ".vira" / "vault"

_lock = threading.Lock()


def _extract_title(md):
    """The plan's title: the first `# ` heading (the plan format mandates one
    on line 1), else the first non-empty line, else a fallback."""
    for line in (md or "").splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()[:120] or "Untitled plan"
    for line in (md or "").splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:120]
    return "Untitled plan"


def _slugify(title):
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:60] or "plan"


def _load():
    """Fresh read every time — a detached runner and the server both touch
    this store, so an in-memory cache would clobber the other's writes."""
    try:
        s = json.loads(REG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        s = {"plans": []}
    if not isinstance(s, dict) or "plans" not in s:
        s = {"plans": []}
    return s


def _save(s):
    REG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REG_PATH.with_name(REG_PATH.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
    tmp.replace(REG_PATH)


def ensure_vault() -> Path:
    """The vault that plans live in. If the owner connected one, use it; else
    create a Vira vault at ~/.vira/vault (qocha-initialized) and connect it —
    a plan is what starts the vault. Falls back to a bare directory when the
    qocha CLI is unavailable (a vault is, at bottom, a folder of markdown)."""
    raw = str(settings.get("vault_root") or "").strip()
    if raw:
        root = Path(raw).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        return root
    from . import onboard
    try:
        onboard.vault_setup(str(DEFAULT_VAULT), init=True)
    except Exception:  # noqa: BLE001 — qocha missing / init failed
        DEFAULT_VAULT.mkdir(parents=True, exist_ok=True)
        try:
            onboard.config_set(vault_root=str(DEFAULT_VAULT))
        except Exception:  # noqa: BLE001 — best-effort connect
            pass
    return DEFAULT_VAULT


def save_plan(md, idea_id=None, job_id=None, lab_url=None):
    """Write plan markdown to <vault>/plans/<date>-<slug>.md and register it.
    Returns the registry entry. Raises ValueError on empty input."""
    md = (md or "").strip()
    if not md:
        raise ValueError("empty plan")
    title = _extract_title(md)
    pdir = ensure_vault() / PLANS_SUBDIR
    pdir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d-%H%M")
    slug = _slugify(title)
    # Filename allocation, the file write, and the registry append all happen
    # under one lock so two concurrent runners finishing same-title plans in
    # the same minute can't pick the same path and clobber each other (the
    # ideas.py discipline: nothing shared touched outside the lock).
    with _lock, locked(REG_PATH):
        fpath = pdir / f"{stamp}-{slug}.md"
        n = 2
        while fpath.exists():                 # two plans, same stamp+slug
            fpath = pdir / f"{stamp}-{slug}-{n}.md"
            n += 1
        tmp = fpath.with_name(fpath.name + ".tmp")
        tmp.write_text(md + "\n")
        tmp.replace(fpath)
        entry = {
            "id": "pl_" + uuid.uuid4().hex[:10],
            "title": title,
            "path": str(fpath),
            "created": now.isoformat(timespec="seconds"),
            "idea_id": idea_id,
            "job_id": job_id,
            "lab_url": (lab_url or "").strip(),
        }
        s = _load()
        s["plans"].insert(0, entry)
        _save(s)
    return entry


def list_plans():
    """Every saved plan, newest first (annotated with `missing` when the
    backing file has been moved or deleted out from under the registry)."""
    out = []
    for p in _load()["plans"]:
        out.append({**p, "missing": not Path(p["path"]).is_file()})
    return out


def get_plan(pid):
    """One plan's registry entry + its markdown body. Raises KeyError."""
    for p in _load()["plans"]:
        if p["id"] == pid:
            try:
                md = Path(p["path"]).read_text()
            except OSError:
                md = ""
            return {**p, "markdown": md, "missing": not md}
    raise KeyError(pid)


def delete_plan(pid):
    """Deregister a plan and remove its vault file. Raises KeyError."""
    with _lock, locked(REG_PATH):
        s = _load()
        gone = next((p for p in s["plans"] if p["id"] == pid), None)
        if gone is None:
            raise KeyError(pid)
        s["plans"] = [p for p in s["plans"] if p["id"] != pid]
        _save(s)
    try:
        Path(gone["path"]).unlink()
    except OSError:
        pass
    return {"removed": pid}
