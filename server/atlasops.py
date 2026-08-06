"""Vault operations behind a plan-and-approve gate — the Image Atlas as an
operating surface, not just a viewer.

The owner's workflow (2026-08-06): select a cluster in the 3D galaxy
("emotional abuse / attachment"), say "move all of this to my personal
vault", read back a plan of exactly what would move and what would break,
approve it, and the files move. Three disciplines meet here:

- **chaska never writes the vault** (its own AGENTS.md invariant), so the
  mutation layer lives in VIRA, beside plans.py / vaultpeople / fullingest —
  the modules that already write vaults, with the same VIRA_PASSIVE guard.
- **A plan is DETERMINISTIC and the model is nowhere in it.** plan_move()
  reads the vault conventions chaska scans (raw/<sha> pairs, wiki/assets
  anchor pages, loose files), derives every companion note, every shared-
  note conflict, every inbound wikilink that will break, and every
  destination collision. The reconcile card the owner approves is composed
  from facts on disk, not judgment.
- **Nothing moves until Approve, and everything that moves is receipted.**
  apply_plan() re-verifies the disk against the plan (a vault edited since
  planning refuses with "re-plan" rather than moving files the owner never
  saw listed), records every from->to pair, and undo_plan() replays the
  receipt backwards. A move is a rename, never a copy-then-delete of the
  only copy; cross-device falls back to shutil.move's copy2+unlink.

Sidecar migration: an image's embedding is keyed by its VAULT-RELATIVE path
and invalidated by mtime. A move preserves both (same rel path at the
destination, rename preserves mtime), so the vecs/items rows are copied
into the destination sidecar and the destination build skips re-embedding
everything that just arrived. Best-effort: a failed migration only costs a
re-embed, never the move.

Store: data/atlas-ops.json (jsonstore discipline), plans pruned at 100.
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote

from . import imageatlas, jsonstore

try:
    from chaska.config import Config as _ChaskaConfig, EXCLUDE_DIRS as _EXCLUDE
    from chaska.index import Index as _ChaskaIndex
    from chaska.scan import parse_frontmatter
    CHASKA_OK = True
except Exception:                       # dormant alongside imageatlas
    _ChaskaConfig = _ChaskaIndex = None
    _EXCLUDE = ["pending-user-deletion", ".obsidian", ".git", ".chaska"]
    CHASKA_OK = False

    def parse_frontmatter(text: str) -> dict:   # minimal fallback, unused when dormant
        return {}

STORE = Path(__file__).resolve().parent.parent / "data" / "atlas-ops.json"
MAX_PLANS = 100
MAX_FILES = 4000
SAMPLE_LINKS = 20

_apply_lock = threading.Lock()


def _passive() -> bool:
    return bool(os.environ.get("VIRA_PASSIVE"))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _load() -> dict:
    data = jsonstore.read(STORE, {"plans": {}})
    if not isinstance(data, dict) or "plans" not in data:
        data = {"plans": {}}
    return data


def _mutate(fn):
    def op(data):
        if not isinstance(data, dict) or "plans" not in data:
            data = {"plans": {}}
        fn(data)
        plans = data["plans"]
        if len(plans) > MAX_PLANS:
            for pid in sorted(plans, key=lambda k: plans[k].get("created", ""))[:-MAX_PLANS]:
                plans.pop(pid, None)
        return data
    return jsonstore.mutate(STORE, op, {"plans": {}})


def get_plan(pid: str) -> dict | None:
    return _load()["plans"].get(pid)


def recent(limit: int = 20) -> list[dict]:
    plans = list(_load()["plans"].values())
    plans.sort(key=lambda p: p.get("created", ""), reverse=True)
    return plans[:limit]


# ------------------------------------------------------------ vault read ---

def _root_of(vid: str) -> Path:
    v = imageatlas.vault_by_id(vid)
    if v is None or not v["exists"]:
        raise ValueError(f"unknown vault '{vid}'")
    return Path(v["root"])


def _contained_rel(root: Path, rel: str) -> str:
    """Validate a vault-relative POSIX path; returns the normalized rel."""
    rel = (rel or "").strip().lstrip("/")
    p = imageatlas.contained(root, rel)
    if p is None:
        raise ValueError(f"path escapes the vault: {rel}")
    return rel


def _wiki_pages(root: Path) -> dict:
    """anchor -> page rel, from images_anchors frontmatter across wiki/*.md.
    A page's own stem always claims its matching anchor dir."""
    anchor_to_page: dict[str, str] = {}
    wiki = root / "wiki"
    if not wiki.is_dir():
        return anchor_to_page
    for md in sorted(wiki.glob("*.md")):
        try:
            fm = parse_frontmatter(md.read_text(encoding="utf-8"))
        except OSError:
            continue
        rel = md.relative_to(root).as_posix()
        anchors = fm.get("images_anchors")
        if isinstance(anchors, list):
            for a in anchors:
                if isinstance(a, str) and a.strip():
                    anchor_to_page.setdefault(a.strip(), rel)
    return anchor_to_page


def _page_asset_files(root: Path, page_rel: str, anchor_to_page: dict) -> list[str]:
    """Every file under every anchor dir the page owns (its own stem plus
    the anchors its frontmatter claims)."""
    stem = Path(page_rel).stem
    anchors = {stem} | {a for a, p in anchor_to_page.items() if p == page_rel}
    out = []
    for a in anchors:
        d = root / "wiki" / "assets" / a
        if d.is_dir():
            for f in sorted(d.rglob("*")):
                if f.is_file():
                    out.append(f.relative_to(root).as_posix())
    return out


# ------------------------------------------------------------------ plan ---

def plan_move(src_vid: str, paths: list[str], dest_vid: str = "",
              new_vault: dict | None = None) -> dict:
    """Compose (and store) a move plan. Read-only against the vaults —
    nothing is created, moved, or registered until apply_plan.

    dest is EITHER an existing registry id or new_vault {name, root?}."""
    root = _root_of(src_vid)
    if not paths:
        raise ValueError("nothing selected")
    if len(paths) > MAX_FILES:
        raise ValueError(f"selection too large (max {MAX_FILES})")
    if not dest_vid and not new_vault:
        raise ValueError("no destination vault")
    if dest_vid and src_vid == dest_vid:
        raise ValueError("source and destination are the same vault")

    dest_root: Path | None = None
    dest_label = ""
    if dest_vid:
        dest_root = _root_of(dest_vid)
        dest_label = (imageatlas.vault_by_id(dest_vid) or {}).get("name", dest_vid)
    else:
        name = str((new_vault or {}).get("name") or "").strip()
        if not name:
            raise ValueError("a new vault needs a name")
        dest_label = name + " (new vault)"

    sel = []
    seen = set()
    for rel in paths:
        rel = _contained_rel(root, rel)
        if rel in seen:
            continue
        seen.add(rel)
        sel.append(rel)
    sel_set = set(sel)

    anchor_to_page = _wiki_pages(root)
    files: list[dict] = []
    notes_moving: set[str] = set()
    conflicts: list[dict] = []
    page_verdict: dict[str, dict] = {}   # page rel -> {"moves": bool, ...}
    missing: list[str] = []
    total_bytes = 0

    for rel in sel:
        p = root / rel
        if not p.is_file():
            missing.append(rel)
            continue
        st = p.stat()
        total_bytes += st.st_size
        entry = {"path": rel, "size": st.st_size, "mtime": st.st_mtime,
                 "kind": "loose", "note": "", "note_action": "none"}
        if rel.startswith("raw/"):
            entry["kind"] = "raw-pair"
            note = f"wiki/{Path(rel).stem}.md"
            if (root / note).is_file():
                entry["note"] = note
                entry["note_action"] = "moves"
                notes_moving.add(note)
            else:
                entry["note_action"] = "none"
        elif rel.startswith("wiki/assets/"):
            entry["kind"] = "asset"
            anchor = Path(rel).parent.name
            page = anchor_to_page.get(anchor)
            if page is None and (root / "wiki" / f"{anchor}.md").is_file():
                page = f"wiki/{anchor}.md"
            if page:
                entry["note"] = page
                if page not in page_verdict:
                    assets = _page_asset_files(root, page, anchor_to_page)
                    uncovered = [a for a in assets if a not in sel_set]
                    page_verdict[page] = {"moves": not uncovered,
                                          "uncovered": uncovered,
                                          "assets": assets}
                    if uncovered:
                        conflicts.append({
                            "note": page,
                            "reason": f"stays — it also embeds {len(uncovered)} "
                                      f"file(s) not in this selection",
                            "uncovered": uncovered[:8],
                        })
                    else:
                        notes_moving.add(page)
                entry["note_action"] = "moves" if page_verdict[page]["moves"] else "stays"
        files.append(entry)

    if missing:
        raise ValueError("not on disk (re-select and try again): " + ", ".join(missing[:5]))

    # note records get their own drift stamps
    note_files = []
    for note in sorted(notes_moving):
        st = (root / note).stat()
        note_files.append({"path": note, "size": st.st_size, "mtime": st.st_mtime})
        total_bytes += st.st_size

    moving_all = sel_set | notes_moving
    inbound = _inbound_links(root, moving_all)
    collisions = []
    if dest_root is not None:
        for rel in sorted(moving_all):
            if (dest_root / rel).exists():
                collisions.append(rel)

    plan = {
        "id": "ap_" + uuid.uuid4().hex[:10],
        "created": _now(),
        "status": "proposed",
        "src": src_vid,
        "src_root": str(root),
        "dest": dest_vid,
        "dest_label": dest_label,
        "new_vault": ({"name": str(new_vault.get("name") or "").strip(),
                       "root": str(new_vault.get("root") or "").strip()}
                      if (new_vault and not dest_vid) else None),
        "files": files,
        "notes": note_files,
        "conflicts": conflicts,
        "inbound": inbound,
        "collisions": collisions,
        "totals": {"images": len(files), "notes": len(note_files),
                   "bytes": total_bytes},
    }
    _mutate(lambda d: d["plans"].__setitem__(plan["id"], plan))
    return plan


_WIKILINK_RE = re.compile(r"!?\[\[([^\]|#^]+)")
_MDLINK_RE = re.compile(r"\]\(<?([^)>\s]+)")


def _inbound_links(root: Path, moving: set[str]) -> dict:
    """LINKS from notes that are STAYING to anything that is moving — the
    references a cross-vault move breaks. Counted by extracting every
    wikilink/embed target and markdown link target and set-intersecting
    with the moving names: a giant alternation regex (one token per moving
    file) over an 8k-note vault measured in MINUTES on the first live plan;
    extraction is one linear pass per note. Plain-text mentions deliberately
    do not count — links are what break."""
    names = set()
    for rel in moving:
        p = Path(rel)
        names.add(p.name.lower())
        if rel.endswith(".md"):
            names.add(p.stem.lower())
    if not names:
        return {"count": 0, "samples": []}
    excluded = set(_EXCLUDE)
    count = 0
    samples: list[dict] = []
    for md in root.rglob("*.md"):
        parts = md.relative_to(root).parts
        rel = "/".join(parts)
        if rel in moving:
            continue
        if set(parts[:-1]) & excluded or any(x.startswith(".") for x in parts):
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        hits = set()
        for m in _WIKILINK_RE.finditer(text):
            t = m.group(1).strip()
            base = t.split("/")[-1].lower()
            if base in names:
                hits.add(base)
            elif (base + ".md") in names:
                hits.add(base + ".md")
        for m in _MDLINK_RE.finditer(text):
            url = m.group(1)
            if "://" in url or url.startswith(("mailto:", "obsidian:")):
                continue
            base = unquote(url).split("/")[-1].lower()
            if base in names:
                hits.add(base)
        if not hits:
            continue
        count += len(hits)
        if len(samples) < SAMPLE_LINKS:
            samples.append({"note": rel, "refs": sorted(hits)[:4]})
    return {"count": count, "samples": samples}


# ----------------------------------------------------------------- apply ---

def apply_plan(pid: str) -> dict:
    """Execute an approved plan. Refused on passive instances (the moves
    land in the real vaults). Verifies the disk still matches the plan —
    a drifted file refuses the whole apply rather than moving unlisted
    state."""
    if _passive():
        raise PermissionError("passive instance — vault moves run on the live Vira only")
    with _apply_lock:
        plan = get_plan(pid)
        if plan is None:
            raise ValueError("unknown plan")
        if plan["status"] != "proposed":
            raise ValueError(f"plan is {plan['status']}, not proposed")
        src_root = Path(plan["src_root"])
        if not src_root.is_dir():
            raise ValueError("source vault is gone")

        # drift check BEFORE anything is created or moved
        drifted = []
        for rec in plan["files"] + plan["notes"]:
            p = src_root / rec["path"]
            if not p.is_file() or abs(p.stat().st_mtime - rec["mtime"]) > 1e-6:
                drifted.append(rec["path"])
        if drifted:
            raise ValueError("the vault changed since this plan was made — "
                             "re-plan. Drifted: " + ", ".join(drifted[:5]))

        # destination: existing vault, or create + register the new one now
        if plan["dest"]:
            dest_root = _root_of(plan["dest"])
            dest_vid = plan["dest"]
        else:
            nv = plan["new_vault"] or {}
            entry = imageatlas.register_vault(nv.get("name", ""),
                                              nv.get("root", ""), create=True)
            dest_root = Path(entry["root"])
            dest_vid = entry["id"]

        moving = [rec["path"] for rec in plan["files"]] + \
                 [rec["path"] for rec in plan["notes"]]
        collisions = [rel for rel in moving if (dest_root / rel).exists()]
        if collisions:
            raise ValueError("destination already has: " + ", ".join(collisions[:5]))

        receipt: list[dict] = []
        failures: list[dict] = []
        for rel in moving:
            src = src_root / rel
            dst = dest_root / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                receipt.append({"path": rel})
            except OSError as e:
                failures.append({"path": rel, "error": str(e)})

        # empty anchor dirs left behind by fully-moved pages
        for rel in receipt:
            parent = (src_root / rel["path"]).parent
            try:
                if parent.is_dir() and parent != src_root and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass

        migrated = _migrate_sidecar_rows(src_root, dest_root,
                                         [r["path"] for r in receipt])

        def upd(d):
            p = d["plans"].get(pid)
            if p is not None:
                p["status"] = "applied" if not failures else "applied-partial"
                p["applied_at"] = _now()
                p["dest"] = dest_vid
                p["dest_root"] = str(dest_root)
                p["receipt"] = receipt
                p["failures"] = failures
                p["migrated_vectors"] = migrated
        _mutate(upd)
        return {"plan_id": pid, "moved": len(receipt), "failed": failures,
                "dest": dest_vid, "dest_root": str(dest_root),
                "migrated_vectors": migrated,
                "note": "the source atlas shows moved items until its next build"}


def undo_plan(pid: str) -> dict:
    """Replay an applied plan's receipt backwards. Refuses when the source
    path has been re-occupied since — never overwrites."""
    if _passive():
        raise PermissionError("passive instance — vault moves run on the live Vira only")
    with _apply_lock:
        plan = get_plan(pid)
        if plan is None:
            raise ValueError("unknown plan")
        if plan["status"] not in ("applied", "applied-partial"):
            raise ValueError(f"plan is {plan['status']} — nothing to undo")
        src_root = Path(plan["src_root"])
        dest_root = Path(plan.get("dest_root") or "")
        if not dest_root.is_dir():
            raise ValueError("destination vault is gone")
        returned: list[dict] = []
        failures: list[dict] = []
        for rec in plan.get("receipt", []):
            rel = rec["path"]
            src = dest_root / rel      # where it lives now
            dst = src_root / rel       # where it came from
            if not src.is_file():
                failures.append({"path": rel, "error": "no longer at the destination"})
                continue
            if dst.exists():
                failures.append({"path": rel, "error": "source path re-occupied"})
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                returned.append({"path": rel})
            except OSError as e:
                failures.append({"path": rel, "error": str(e)})
        _migrate_sidecar_rows(dest_root, src_root, [r["path"] for r in returned])

        def upd(d):
            p = d["plans"].get(pid)
            if p is not None:
                p["status"] = "undone" if not failures else "undone-partial"
                p["undone_at"] = _now()
                p["undo_failures"] = failures
        _mutate(upd)
        return {"plan_id": pid, "returned": len(returned), "failed": failures}


def _migrate_sidecar_rows(src_root: Path, dest_root: Path,
                          rels: list[str]) -> int:
    """Copy items/vecs rows for moved paths into the destination sidecar and
    drop them from the source. Rel paths and mtimes survive a move, so the
    destination build re-uses the embeddings instead of recomputing them.
    Best-effort: any failure costs a re-embed, never the move."""
    if not CHASKA_OK or not rels:
        return 0
    moved = 0
    try:
        src_idx = _ChaskaIndex(_ChaskaConfig(root=src_root))
        dst_idx = _ChaskaIndex(_ChaskaConfig(root=dest_root))
        try:
            for rel in rels:
                for table in ("items", "vecs"):
                    row = src_idx.con.execute(
                        f"SELECT * FROM {table} WHERE path = ?", (rel,)).fetchone()
                    if row is None:
                        continue
                    cols = row.keys()
                    dst_idx.con.execute(
                        f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
                        f"VALUES ({','.join('?' for _ in cols)})",
                        tuple(row[c] for c in cols))
                    src_idx.con.execute(
                        f"DELETE FROM {table} WHERE path = ?", (rel,))
                    if table == "vecs":
                        moved += 1
            dst_idx.con.commit()
            src_idx.con.commit()
        finally:
            src_idx.close()
            dst_idx.close()
    except Exception:
        return moved
    return moved
