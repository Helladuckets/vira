"""Site documents — migrated owner-only documents, served from Vira.

The 2026-07-27 documents-merge decision: documents whose only reader is the
owner (plan pages, Vira ops dossiers) do not belong on the public site.
thedurham.nyc keeps running prototypes; Vira keeps documents. This module owns
Vira's side of that split.

- static/docs/ (git-ignored, like static/explainer/) is the byte home, served
  by the root static mount at /docs/ — no route code needed.
- migrate() copies the site's lab/plans pages (with their plans.json metadata)
  and the Vira ops dossier dirs into static/docs/, inverts the site's
  lab/_provenance/index.json into a per-page provenance sidecar, records each
  page's old public URL as an alias, and copies any lab/plans/src/*.md
  markdown sources into the vault so those plans become searchable.
- registry.json under static/docs/ is the module's own idempotent record of
  every known document. readinglist's backfill sweeps it via registry_rows()
  (which also folds in manifest.json — the file the repointed plan-mode hook
  appends to for every NEW plan), so hook-written plans keep flowing into the
  Reader with no server-side worker.
- migrate() ALSO registers directly (the plans.save_plan producer pattern),
  mirroring backfill's rule: an entry seen for the first time that is older
  than the freshness window is filed as already read, never queued.
- index.html under static/docs/ is a generated plain listing so /docs/ is
  browsable on any of the owner's devices even outside the Reader.

CLI-first: python -m server.sitedocs migrate|sweep|status. No routes.

Passive note: migrate/sweep write only this checkout's static/docs/ and its
cloned data/, which is safe on a test instance — EXCEPT the vault markdown
copy, which targets the owner's real vault and is skipped under VIRA_PASSIVE
(the plans.py precedent).
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import settings

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "static" / "docs"          # patched by tests
REGISTRY = "registry.json"                    # under DOCS_DIR
MANIFEST = "manifest.json"                    # under DOCS_DIR/plans (hook-written)

SITE_URL = "https://thedurham.nyc"

# The Vira ops dossier dirs that migrate off the site. Deliberately explicit
# and conservative: anything not on this list stays on the site untouched.
DOSSIER_DIRS = (
    "vira-atlas",
    "vira-audit",
    "vira-dossier",
    "vira-agentic-os",
    "vira-first-branch",
    "vira-model-connections",
)

_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def site_root() -> Path | None:
    """The owner's thedurham-nyc checkout, from config `site_root`. Empty =
    dormant (a stranger's install has no site)."""
    raw = str(settings.get("site_root") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_dir() else None


def _doc_id(locator: str) -> str:
    return "doc_" + hashlib.sha256(locator.encode("utf-8")).hexdigest()[:10]


def _created_of(path: Path, name: str = "") -> str:
    """Date for freshness decisions: a YYYY-MM-DD filename prefix wins (these
    files are rsynced/copied, so mtime is when the copy landed, not when the
    document was made); else the mtime date."""
    m = _DATE_PREFIX.match(name or path.name)
    if m:
        return m.group(1)
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return ""


def _read_registry(docs_dir: Path) -> dict:
    try:
        data = json.loads((docs_dir / REGISTRY).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"items": []}


def _write_registry(docs_dir: Path, reg: dict) -> None:
    docs_dir.mkdir(parents=True, exist_ok=True)
    tmp = docs_dir / (REGISTRY + ".tmp")
    tmp.write_text(json.dumps(reg, indent=1, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(docs_dir / REGISTRY)


def _record(docs_dir: Path, entries: list[dict]) -> int:
    """Fold entries into registry.json, keyed by locator. Returns new count."""
    reg = _read_registry(docs_dir)
    known = {it.get("locator") for it in reg["items"]}
    new = 0
    for e in entries:
        if e["locator"] in known:
            continue
        reg["items"].append(e)
        known.add(e["locator"])
        new += 1
    if new:
        _write_registry(docs_dir, reg)
    return new


def _manifest_rows(docs_dir: Path) -> list[dict]:
    """Plans the repointed plan-mode hook has written since migration —
    manifest.json beside them is the hook's append-only record."""
    rows = []
    try:
        data = json.loads((docs_dir / "plans" / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return rows
    if not isinstance(data, list):
        return rows
    for e in data:
        fn = str(e.get("filename") or "")
        if not fn.endswith(".html"):
            continue
        if not (docs_dir / "plans" / fn).is_file():
            continue
        rows.append({
            "id": _doc_id(f"/docs/plans/{fn}"),
            "kind": "plan",
            "title": str(e.get("title") or fn),
            "locator": f"/docs/plans/{fn}",
            "summary": str(e.get("summary") or ""),
            "created": str(e.get("date") or _created_of(docs_dir / "plans" / fn, fn)),
            "alias": "",
            "source": "plan-hook",
        })
    return rows


def registry_rows(docs_dir: Path | None = None) -> list[dict]:
    """Everything /docs/ holds, in readinglist-backfill row shape. Reads the
    registry AND the hook manifest, so a plan written five minutes ago is
    sweepable before any sitedocs command has run."""
    docs_dir = docs_dir or DOCS_DIR
    seen: set[str] = set()
    rows: list[dict] = []
    for it in _read_registry(docs_dir)["items"] + _manifest_rows(docs_dir):
        loc = it.get("locator") or ""
        if not loc or loc in seen:
            continue
        seen.add(loc)
        rows.append({
            "title": it.get("title") or loc,
            "kind": it.get("kind") or "dossier",
            "locator": loc,
            "locator_kind": "url",
            "created": it.get("created") or "",
        })
    return rows


def _invert_provenance(site: Path) -> dict[str, list[str]]:
    """The site's lab/_provenance/index.json is keyed by SOURCE with a pages
    list; invert it to page -> [source names] so each migrated document can
    carry its own build trail."""
    out: dict[str, list[str]] = {}
    try:
        data = json.loads((site / "lab" / "_provenance" / "index.json")
                          .read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(data, dict):
        return out
    for source, info in data.items():
        pages = (info or {}).get("pages") or []
        for p in pages:
            out.setdefault(str(p), []).append(str(source))
    return out


def _register_direct(entries: list[dict]) -> dict:
    """Producer registration into the reading list (the plans.save_plan
    pattern), mirroring backfill's freshness rule: a first-sight entry older
    than FRESH_DAYS files as already read. Never raises."""
    stats = {"registered": 0, "queued": 0, "filed_read": 0, "errors": 0}
    try:
        from . import readinglist
    except ImportError:
        stats["errors"] = len(entries)
        return stats
    for e in entries:
        try:
            before = readinglist.find_by_locator(e["locator"], "url")
            it = readinglist.register(
                e["title"], e["kind"], e["locator"], "url",
                source=e.get("source", "sitedocs"), created=e.get("created"))
            if before:
                continue
            stats["registered"] += 1
            if readinglist._is_stale(it.get("created")):
                readinglist.complete(it["id"])
                stats["filed_read"] += 1
            else:
                stats["queued"] += 1
        except Exception:
            stats["errors"] += 1
    return stats


def _copy_tree(src: Path, dst: Path) -> int:
    """Copy a directory, tolerating re-runs. Returns files copied."""
    n = 0
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        n += 1
    return n


def migrate(site: Path | None = None, docs_dir: Path | None = None,
            vault_plans: Path | None = None) -> dict:
    """Copy the site's plans + Vira ops dossiers into static/docs/, build the
    registry/provenance sidecars, register with the Reader, regenerate the
    /docs/ index. Idempotent — re-running refreshes copies and adds nothing
    twice."""
    site = site or site_root()
    docs_dir = docs_dir or DOCS_DIR
    if site is None:
        return {"error": "site_root not configured or missing"}

    plans_src = site / "lab" / "plans"
    prov = _invert_provenance(site)
    entries: list[dict] = []
    copied = {"plans": 0, "dossier_files": 0, "vault_md": 0}

    # --- plans -----------------------------------------------------------
    plans_meta: dict[str, dict] = {}
    try:
        loaded = json.loads((plans_src / "plans.json").read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            plans_meta = {str(e.get("filename")): e for e in loaded}
    except (OSError, json.JSONDecodeError):
        pass

    dst_plans = docs_dir / "plans"
    dst_plans.mkdir(parents=True, exist_ok=True)
    for f in sorted(plans_src.glob("*.html")):
        if f.name == "index.html":
            continue
        shutil.copy2(f, dst_plans / f.name)
        copied["plans"] += 1
        meta = plans_meta.get(f.name, {})
        locator = f"/docs/plans/{f.name}"
        entries.append({
            "id": _doc_id(locator),
            "kind": "plan",
            "title": str(meta.get("title") or f.stem),
            "locator": locator,
            "summary": str(meta.get("summary") or ""),
            "created": str(meta.get("date") or _created_of(f)),
            "alias": f"{SITE_URL}/lab/plans/{f.name}",
            "provenance": prov.get(f"/lab/plans/{f.name}", []),
            "source": "sitedocs-migrate",
        })
    thumbs = plans_src / "thumbs"
    if thumbs.is_dir():
        _copy_tree(thumbs, dst_plans / "thumbs")

    # Markdown sources (the hook wrote lab/plans/src/<stem>.md for newer
    # plans) go to the vault so those plans are searchable. Owner-only:
    # a passive clone must never write the real vault.
    src_md = plans_src / "src"
    if src_md.is_dir() and not os.environ.get("VIRA_PASSIVE"):
        vp = vault_plans
        if vp is None:
            vroot = str(settings.get("vault_root") or "").strip()
            vp = Path(vroot).expanduser() / "plans" if vroot else None
        if vp is not None:
            vp.mkdir(parents=True, exist_ok=True)
            for m in sorted(src_md.glob("*.md")):
                target = vp / m.name
                if not target.exists():
                    shutil.copy2(m, target)
                    copied["vault_md"] += 1

    # --- dossier dirs ----------------------------------------------------
    for name in DOSSIER_DIRS:
        d = site / "lab" / name
        if not d.is_dir():
            continue
        copied["dossier_files"] += _copy_tree(d, docs_dir / name)
        index = d / "index.html"
        page = index if index.is_file() else None
        locator = f"/docs/{name}/"
        entries.append({
            "id": _doc_id(locator),
            "kind": "dossier",
            "title": _title_of(page) or name,
            "locator": locator,
            "summary": "",
            "created": _created_of(page) if page else "",
            "alias": f"{SITE_URL}/lab/{name}/",
            "provenance": prov.get(f"/lab/{name}/index.html", []),
            "source": "sitedocs-migrate",
        })

    new = _record(docs_dir, entries)
    _write_provenance_sidecar(docs_dir, entries)
    reg_stats = _register_direct(entries)
    write_index(docs_dir)
    return {"copied": copied, "entries": len(entries), "new_in_registry": new,
            **reg_stats}


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _title_of(page: Path | None) -> str:
    if page is None:
        return ""
    try:
        head = page.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return ""
    m = _TITLE_RE.search(head)
    if not m:
        return ""
    # Unescape entities: the <title> text is HTML-escaped in source, and
    # every consumer (registry, Reader, the generated index) re-escapes on
    # render — without this, "&amp;" displays literally.
    return _html.unescape(re.sub(r"\s+", " ", m.group(1)).strip())


def _write_provenance_sidecar(docs_dir: Path, entries: list[dict]) -> None:
    """One sidecar mapping each /docs/ locator to its old public URL and the
    build sources the site's provenance index recorded for it."""
    path = docs_dir / "provenance.json"
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    except (OSError, json.JSONDecodeError):
        existing = {}
    for e in entries:
        existing[e["locator"]] = {
            "site_url": e.get("alias") or "",
            "sources": e.get("provenance") or [],
        }
    docs_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(existing, indent=1, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def sweep() -> dict:
    """Fold hook-written plans into the registry, register them with the
    Reader, regenerate the index. Cheap; safe to re-run."""
    rows = _manifest_rows(DOCS_DIR)
    new = _record(DOCS_DIR, rows)
    stats = _register_direct(rows)
    write_index(DOCS_DIR)
    return {"manifest_plans": len(rows), "new_in_registry": new, **stats}


def add_dossier(title: str, source: Path | str, *, summary: str = "",
                 created: str | None = None) -> dict:
    """Copy a portable dossier into Vira Documents and register it.

    The source must contain ``index.html``. Symlinks are refused so the copied
    document remains a self-contained reviewed bundle. A title collision gets
    a numbered slug instead of overwriting an existing document.
    """
    from .blog import slugify

    clean_title = (title or "").strip()
    if not clean_title:
        raise ValueError("title required")
    src = Path(source).expanduser().resolve()
    if not src.is_dir() or not (src / "index.html").is_file():
        raise ValueError("dossier source must be a directory with index.html")
    for p in src.rglob("*"):
        if p.is_symlink():
            raise ValueError(f"dossier source contains symlink: {p}")

    base = slugify(clean_title)
    parent = DOCS_DIR / "dossiers"
    slug, n = base, 2
    while (parent / slug).exists():
        slug, n = f"{base}-{n}", n + 1
    dst = parent / slug
    shutil.copytree(src, dst)
    locator = f"/docs/dossiers/{slug}/"
    entry = {
        "id": _doc_id(locator),
        "kind": "dossier",
        "title": clean_title,
        "locator": locator,
        "summary": (summary or "").strip(),
        "created": created or datetime.now().strftime("%Y-%m-%d"),
        "alias": "",
        # Served registry metadata must not expose a workstation path or user.
        "provenance": ["local interactive dossier import"],
        "source": "sitedocs-add",
    }
    new = _record(DOCS_DIR, [entry])
    _write_provenance_sidecar(DOCS_DIR, [entry])
    stats = _register_direct([entry])
    write_index(DOCS_DIR)
    return {
        "slug": slug,
        "locator": locator,
        "files": sum(1 for p in dst.rglob("*") if p.is_file()),
        "new_in_registry": new,
        **stats,
    }


def status() -> dict:
    reg = _read_registry(DOCS_DIR)
    kinds: dict[str, int] = {}
    for it in reg["items"]:
        kinds[it.get("kind", "?")] = kinds.get(it.get("kind", "?"), 0) + 1
    return {"docs_dir": str(DOCS_DIR), "registered": len(reg["items"]),
            "kinds": kinds, "site_root": str(site_root() or "")}


# --- the /docs/ index page ---------------------------------------------------

_INDEX_STYLE = """
:root { color-scheme: dark; --bg:#0b0d10; --ink:#e8e4da; --dim:#8b8578;
  --line:#26221c; --accent:#d4a24e; --scroll:#2e2921; --scroll-hi:#463d2f; }
@media (prefers-color-scheme: light) {
  :root { color-scheme: light; --bg:#faf7f0; --ink:#1c1a16; --dim:#6e675c;
    --line:#e2dcd0; --accent:#8a6420; --scroll:#d8d2c6; --scroll-hi:#bfb6a6; } }
* { box-sizing: border-box; }
html { overflow-y: scroll; }
::-webkit-scrollbar { width: 11px; height: 11px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--scroll); border-radius: 6px;
  border: 3px solid transparent; background-clip: content-box; }
::-webkit-scrollbar-thumb:hover { background: var(--scroll-hi);
  border: 3px solid transparent; background-clip: content-box; }
body { margin: 0; padding: 40px 20px 80px; background: var(--bg);
  color: var(--ink); font: 15px/1.6 -apple-system, BlinkMacSystemFont,
  "Segoe UI", sans-serif; }
main { max-width: 860px; margin: 0 auto; }
h1 { font-family: Georgia, serif; font-weight: 700; font-size: 28px;
  margin: 0 0 4px; }
.sub { color: var(--dim); margin: 0 0 34px; font-size: 13px; }
h2 { font-family: ui-monospace, Menlo, monospace; font-size: 11px;
  letter-spacing: .18em; text-transform: uppercase; color: var(--accent);
  border-bottom: 1px solid var(--line); padding-bottom: 8px; margin: 38px 0 6px; }
a.row { display: flex; gap: 14px; align-items: baseline; padding: 9px 4px;
  border-bottom: 1px solid var(--line); color: var(--ink);
  text-decoration: none; }
a.row:hover { color: var(--accent); }
.d { font-family: ui-monospace, Menlo, monospace; font-size: 11px;
  color: var(--dim); flex: 0 0 84px; }
.t { min-width: 0; }
"""


def write_index(docs_dir: Path | None = None) -> None:
    """A generated plain listing at /docs/ — browsable everywhere the owner
    is, independent of the Reader. Standalone document, so it carries its own
    color-scheme + scrollbar (the reading-room lesson: an iframe/standalone
    page inherits nothing from the app shell)."""
    docs_dir = docs_dir or DOCS_DIR
    rows = registry_rows(docs_dir)
    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    for v in by_kind.values():
        v.sort(key=lambda r: r.get("created") or "", reverse=True)

    parts = ["<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
             "<meta name='viewport' content='width=device-width, initial-scale=1'>",
             "<title>Documents - Vira</title>",
             f"<style>{_INDEX_STYLE}</style></head><body><main>",
             "<h1>Documents</h1>",
             f"<p class='sub'>{len(rows)} documents migrated into Vira from "
             "the site, plus every plan rendered since. The Reader is the "
             "richer surface; this page is the plain shelf.</p>"]
    label = {"plan": "Plans", "dossier": "Dossiers"}
    for kind in sorted(by_kind, key=lambda k: (k != "dossier", k)):
        parts.append(f"<h2>{_html.escape(label.get(kind, kind.title()))} "
                     f"({len(by_kind[kind])})</h2>")
        for r in by_kind[kind]:
            parts.append(
                f"<a class='row' href='{_html.escape(r['locator'])}'>"
                f"<span class='d'>{_html.escape(r.get('created') or '')}</span>"
                f"<span class='t'>{_html.escape(r['title'])}</span></a>")
    parts.append("</main></body></html>")
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "index.html").write_text("\n".join(parts), encoding="utf-8")


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "migrate":
        out = migrate()
    elif cmd == "sweep":
        out = sweep()
    elif cmd == "status":
        out = status()
    elif cmd == "add" and len(argv) >= 3:
        out = add_dossier(argv[1], argv[2])
    else:
        print("usage: python -m server.sitedocs migrate|sweep|status | "
              "add \"<title>\" <directory>",
              file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1))
    return 0 if "error" not in out else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
