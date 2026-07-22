"""Local backup rotation for the canonical, non-regenerable files in data/.
Everything else under data/ is a cache or index that rebuilds itself; these
are the real loss risk since data/ is git-ignored.

Covered: ideas.json (cross-session backlog), config.json (instance config),
subscriptions.json (curated merchant registry), routines.json (standing
agent loops), circuit-runs.json (circuit state), brief-journal.json (every
note told to Vira), atlas-groups.json (curated network groups),
jobs-log.json (the durable job ledger). The last five joined 2026-07-20
closing the external audit's P1-8 gap list (decision D5 bucket A).
applications.json (job-application owner state), mail-accounts.json (mail
account registry), and circuits.json (circuit definitions) joined
2026-07-21 (module-audit wave 1).

One dated snapshot per file per day into ~/.vira-backups/ (outside the
repo), keeping the newest 14 of each. Runs at startup and then daily from
a daemon thread. Pure stdlib, never raises into the caller.
"""
import shutil
import threading
import time
from datetime import date
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
DEST = Path.home() / ".vira-backups"
FILES = ("ideas.json", "config.json", "subscriptions.json",
         "routines.json", "circuit-runs.json", "brief-journal.json",
         "atlas-groups.json", "jobs-log.json", "applications.json",
         "mail-accounts.json", "circuits.json")
KEEP = 14


def snapshot():
    stamp = date.today().isoformat()
    for name in FILES:
        src = DATA / name
        if not src.exists():
            continue
        try:
            DEST.mkdir(exist_ok=True)
            target = DEST / f"{src.stem}-{stamp}{src.suffix}"
            if not target.exists():
                shutil.copy2(src, target)
            olds = sorted(DEST.glob(f"{src.stem}-*{src.suffix}"))
            for old in olds[:-KEEP]:
                old.unlink()
        except OSError:
            continue  # best-effort; try again on the next tick


def start():
    def loop():
        while True:
            snapshot()
            time.sleep(6 * 3600)  # re-check 4x/day; snapshot() is per-day idempotent
    threading.Thread(target=loop, daemon=True, name="vira-backup").start()
