"""In-app update flow: know when the remote is ahead, pull, restart.

This is the beta-test seam for "how does the app handle new code updates":
the repo IS the running instance, so an update is an ordinary fast-forward
pull followed by a deliberate restart. Personal state (data/, docs/, real
config) is git-ignored, so a pull can never touch it.

The restart is the one deliberate self-restart in the codebase: under
launchd (settings key `launchd_label`) it relaunches via kickstart -k;
otherwise the process just exits and whatever supervises it (or the human
with ./run.sh) starts it again.
"""
import os
import subprocess
import threading
import time
from pathlib import Path

from . import settings

ROOT = Path(__file__).resolve().parent.parent
_last_fetch = {"at": 0.0}
_FETCH_COOLDOWN = 60  # seconds between actual network fetches


def _git(*args, timeout=15):
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True, timeout=timeout)


def status(fetch=False):
    """Current sha/date/branch plus ahead/behind counts vs the upstream.
    fetch=True refreshes the remote refs first (cooldown-limited)."""
    if not (ROOT / ".git").exists():
        return {"git": False}
    head = _git("rev-parse", "--short", "HEAD")
    if head.returncode != 0:
        return {"git": False}
    out = {
        "git": True,
        "sha": head.stdout.strip(),
        "date": _git("show", "-s", "--format=%cd",
                     "--date=format:%Y-%m-%d %H:%M", "HEAD").stdout.strip(),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
    }
    if not _git("remote").stdout.strip():
        out["remote"] = False
        return out
    out["remote"] = True
    now = time.time()
    if fetch and now - _last_fetch["at"] > _FETCH_COOLDOWN:
        f = _git("fetch", "--quiet", timeout=25)
        if f.returncode == 0:
            _last_fetch["at"] = now
        else:
            out["fetch_error"] = (f.stderr or "fetch failed").strip()[:200]
    counts = _git("rev-list", "--left-right", "--count", "HEAD...@{upstream}")
    if counts.returncode != 0:  # no upstream tracking ref yet
        out["ahead"] = out["behind"] = 0
        return out
    ahead, behind = counts.stdout.split()
    out["ahead"], out["behind"] = int(ahead), int(behind)
    if out["behind"]:
        log = _git("log", "--oneline", "HEAD..@{upstream}", "-n", "8")
        out["incoming"] = log.stdout.strip().splitlines()
    return out


def apply():
    """Fast-forward to the upstream and restart. Refuses on modified tracked
    files (untracked personal state is fine — it is ignored anyway), and
    refuses outright when no supervisor is configured — without a
    launchd_label the post-pull exit would just kill the server dead
    (audit P1-9)."""
    label = str(settings.raw().get("launchd_label") or "").strip()
    if not label:
        raise ValueError(
            "no supervisor configured (launchd_label is empty) — a "
            "web-triggered restart would kill the server with nothing to "
            "relaunch it. Update from a terminal instead: git pull, then "
            "restart the process.")
    st = status(fetch=True)
    if not st.get("git") or not st.get("remote"):
        raise ValueError("not a git clone with a remote")
    if not st.get("behind"):
        return {"updated": False, "note": "already up to date", "sha": st.get("sha")}
    dirty = [l for l in _git("status", "--porcelain").stdout.splitlines()
             if l and not l.startswith("??")]
    if dirty:
        raise ValueError(f"{len(dirty)} tracked files modified locally — "
                         "commit or stash them, then update")
    pull = _git("pull", "--ff-only", timeout=90)
    if pull.returncode != 0:
        raise ValueError("git pull failed: "
                         + (pull.stderr or pull.stdout).strip()[:300])
    new_sha = _git("rev-parse", "--short", "HEAD").stdout.strip()
    threading.Timer(0.8, _restart).start()  # let the HTTP response flush first
    return {"updated": True, "restarting": True, "sha": new_sha}


def _restart():
    label = str(settings.raw().get("launchd_label") or "").strip()
    if label:
        try:
            r = subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
                capture_output=True, timeout=10)
            if r.returncode == 0:
                return  # kickstart kills and relaunches this process
        except Exception:  # noqa: BLE001 — fall through to plain exit
            pass
    os._exit(0)
