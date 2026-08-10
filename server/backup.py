"""Local backup rotation for the canonical, non-regenerable data in data/.
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
2026-07-21 (module-audit wave 1). evidence.json (Evidence Ledger case
studies — curated owner work, not regenerable) joined 2026-07-25. The
2026-08-10 data audit added seven more sole-copy files and the DIRS pass
below.

One dated snapshot per file per day into ~/.vira-backups/ (outside the
repo), keeping the newest 14 of each. DIRS get the same treatment as
dated directory copies (built under a .tmp name, then renamed, so a
half-copied tree is never mistaken for a snapshot). Runs at startup and
then daily from a daemon thread. Pure stdlib, never raises into the
caller.
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
         # atlas-groups.json is created lazily on the FIRST group edit
         # (atlas._groups_write); until then it does not exist on disk and
         # the missing-file skip below is the correct behavior, not a dead
         # entry (verified against the writer 2026-08-10).
         "atlas-groups.json", "jobs-log.json", "applications.json",
         "mail-accounts.json", "circuits.json", "evidence.json",
         # The Reader's queue: which documents are worth reading and which are
         # read. The documents themselves live at their sources, but the
         # curation and the read-state are only here.
         "reading-list.json",
         # Lesson recurrence: rules/verdicts/evidence are regenerable from
         # the ledger + retros, but owner verdict overrides, dismissals and
         # aliases exist nowhere else.
         "lesson-recurrence.json",
         # Glossary: the term NOTES live in the vault (backed up with it),
         # but which terms have been looked up, by which rung, and how often
         # exists only here — and losing it makes every banked term climb
         # the ladder again.
         "glossary.json",
         # 2026-08-10 data audit: sole-copy stores that had quietly grown
         # outside the rotation.
         "brain-chat.json",      # Brain conversation history
         "plans.json",           # forged plans — owner decisions, no other copy
         "contact-cards.json",   # owner-curated contact cards
         "ui-state.json",        # workspace layouts and owner arrangements
         "modules.json",         # module registry + owner enable/disable state
         "orphan-work.json",     # orphaned-work ledger
         "doc-index.json",       # doc registry: curation + read state
         "pii-patterns.txt")     # anonymization scanner's learned patterns
# Sole-copy DIRECTORIES: one dated tree copy per day, same 14-day window.
DIRS = (
    # WhatsApp device-link credentials (creds + signal keys). Losing this
    # unlinks the phone and nothing can regenerate it — the single most
    # unrecoverable thing under data/.
    "whatsapp/session",
    "blog/posts",        # canonical blog source (published HTML is derived)
    "reading/rooms",     # reading-room curation and annotations
    "genres",            # genre definitions + poster/specimen assets
    "idea-images",       # images attached to ideas — referenced nowhere else
    "walkthrough-anon",  # anonymized walkthrough output + scanner state
)
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
    for rel in DIRS:
        _snapshot_dir(rel, stamp)


def _snapshot_dir(rel, stamp):
    """Dated copy of one data/ subtree into DEST/<slug>-<stamp>/. The tree
    is copied under a .tmp name and renamed into place, so a crash mid-copy
    leaves debris (swept on the next tick), never a plausible-looking
    partial snapshot."""
    src = DATA / rel
    if not src.is_dir():
        return
    slug = rel.replace("/", "-")
    try:
        DEST.mkdir(exist_ok=True)
        for debris in DEST.glob(f"{slug}-*.tmp"):
            shutil.rmtree(debris, ignore_errors=True)
        target = DEST / f"{slug}-{stamp}"
        if not target.exists():
            tmp = DEST / f"{slug}-{stamp}.tmp"
            shutil.copytree(src, tmp)
            tmp.replace(target)
        olds = sorted(p for p in DEST.glob(f"{slug}-*") if p.is_dir())
        for old in olds[:-KEEP]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass  # best-effort; try again on the next tick


def start():
    def loop():
        while True:
            snapshot()
            time.sleep(6 * 3600)  # re-check 4x/day; snapshot() is per-day idempotent
    threading.Thread(target=loop, daemon=True, name="vira-backup").start()
