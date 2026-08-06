"""Rendered-face thumbnails for the library's HTML documents.

The grid view shows films with real frames while every plan and dossier —
designed pages, the hook's whole job — rendered as a text card. This module
captures each HTML document's actual face with a headless browser and caches
it, so the library reads like the site's gallery (owner's ask, 2026-08-05).

Shape:
  - DORMANT WITHOUT A BROWSER. `browser()` probes for Chrome/Chromium/Edge
    (the models.find_binary pattern — PATH, then app bundles); config
    `thumb_browser` overrides. No browser = no thumbs, honestly reported by
    status(), never an error.
  - The capture is a SUBPROCESS (`--headless=new --screenshot=...` over a
    file:// URL), so it has its own GIL and the CPU-admission rule is
    untouched. sips downscales on macOS; elsewhere the full capture serves.
  - Thumbs are DERIVED: `data/doc-thumbs/<entry-id>-<source-mtime>.png`.
    The mtime in the name is the invalidation — a re-rendered plan gets a
    fresh capture on the next sweep and the stale file is removed.
  - Films are EXCLUDED: they carry their own thumb.jpg + motion.mp4.
  - `annotate(rows)` joins `thumb` URLs at the ROUTE layer with one
    directory listing — never stored on the entry (the doctags rule: a copy
    on the row is a copy that goes stale).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import readinglist, settings

ROOT = Path(__file__).resolve().parent.parent
THUMB_DIR = ROOT / "data" / "doc-thumbs"

ID_RE = re.compile(r"^rl_[a-z0-9]{4,20}$")

# Candidate browsers, cheapest-to-likeliest on this project's machines.
BROWSER_NAMES = ("google-chrome", "chromium", "chromium-browser", "chrome",
                 "msedge")
BROWSER_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

CAPTURE_W, CAPTURE_H = 1280, 800
THUMB_W = 560            # sips downscale target; the tile renders ~300px wide
TIMEOUT_S = 40


def browser():
    """The capture binary, or None (dormant). Config override wins."""
    cfg = (settings.raw().get("thumb_browser") or "").strip()
    if cfg:
        return cfg if Path(cfg).exists() else None
    for name in BROWSER_NAMES:
        hit = shutil.which(name)
        if hit:
            return hit
    for p in BROWSER_PATHS:
        if Path(p).exists():
            return p
    return None


def eligible(entry):
    """An HTML document this module should capture: a url locator resolving
    to a real .html file, excluding films (they carry their own frames)."""
    if entry.get("locator_kind") != "url":
        return None
    if entry.get("kind") == "walkthrough":
        return None
    p = readinglist.source_path(entry)
    if not p or not p.is_file() or p.suffix.lower() not in (".html", ".htm"):
        return None
    return p


def _key(entry_id, mtime):
    return f"{entry_id}-{int(mtime)}.png"


def thumb_file(entry):
    """The CURRENT thumb for an entry, or None. Stale captures (older source
    mtime in the name) are removed on sight."""
    src = eligible(entry)
    if not src:
        return None
    want = THUMB_DIR / _key(entry["id"], src.stat().st_mtime)
    stale = [p for p in THUMB_DIR.glob(entry["id"] + "-*.png") if p != want]
    for p in stale:
        try:
            p.unlink()
        except OSError:
            pass
    return want if want.is_file() else None


def by_id(entry_id):
    """Serve-path lookup: the newest capture for a validated id, no source
    stat — a thumb should serve even if the source moved a moment ago."""
    if not ID_RE.match(entry_id or ""):
        return None
    hits = sorted(THUMB_DIR.glob(entry_id + "-*.png"))
    return hits[-1] if hits else None


def generate(entry):
    """Capture one document. True on success; quiet False on any failure —
    a page that cannot render must never stall the sweep."""
    src = eligible(entry)
    bin_ = browser()
    if not src or not bin_:
        return False
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = THUMB_DIR / (".cap-" + entry["id"] + ".png")
    out = THUMB_DIR / _key(entry["id"], src.stat().st_mtime)
    try:
        subprocess.run(
            [bin_, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             "--no-first-run", "--disable-extensions",
             f"--window-size={CAPTURE_W},{CAPTURE_H}",
             "--virtual-time-budget=5000",
             f"--screenshot={tmp}", src.resolve().as_uri()],
            capture_output=True, timeout=TIMEOUT_S, check=True)
    except (OSError, subprocess.SubprocessError):
        tmp.unlink(missing_ok=True)
        return False
    if not tmp.is_file():
        return False
    # Downscale where sips exists (macOS); a failed downscale keeps the full
    # capture — a big thumb beats no thumb.
    if settings.IS_MAC and shutil.which("sips"):
        subprocess.run(["sips", "--resampleWidth", str(THUMB_W), str(tmp)],
                       capture_output=True, timeout=30)
    try:
        tmp.replace(out)
    except OSError:
        tmp.unlink(missing_ok=True)
        return False
    return True


def sweep(limit=6):
    """Capture up to `limit` missing thumbs, newest documents first.
    Returns {made, pending, dormant}."""
    if not browser():
        return {"made": 0, "pending": 0, "dormant": True}
    made = 0
    pending = 0
    for entry in readinglist.library():
        if not eligible(entry):
            continue
        if thumb_file(entry):
            continue
        if made < limit:
            if generate(entry):
                made += 1
                continue
        pending += 1
    return {"made": made, "pending": pending, "dormant": False}


def annotate(rows):
    """Attach `thumb` URLs with ONE directory listing. Derived at the route
    layer, never stored on the entry."""
    try:
        have = {}
        for p in THUMB_DIR.glob("rl_*.png"):
            eid = p.name.rsplit("-", 1)[0]
            have[eid] = True
    except OSError:
        return rows
    for r in rows:
        if r.get("kind") == "walkthrough":
            continue
        if have.get(r.get("id")):
            r["thumb"] = "/api/reading/thumb/" + r["id"]
    return rows


def status():
    b = browser()
    n = len(list(THUMB_DIR.glob("rl_*.png"))) if THUMB_DIR.is_dir() else 0
    return {"browser": bool(b), "thumbs": n}


class Sweeper(threading.Thread):
    """Background capture, a few documents per tick — the doctags cadence."""

    def __init__(self):
        super().__init__(daemon=True, name="doc-thumbs")
        self._stop = threading.Event()

    def run(self):
        interval = max(300, int(settings.get("doc_thumb_interval_min")) * 60)
        # First pass soon after boot, then the steady cadence.
        self._stop.wait(90)
        while not self._stop.is_set():
            try:
                sweep()
            except Exception:
                pass
            self._stop.wait(interval)

    def stop(self):
        self._stop.set()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "sweep":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
        print(sweep(limit=n))
    else:
        print(status())
