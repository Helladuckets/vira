"""Tiny cross-process file lock shared by the JSON stores.

The durable-runner architecture puts two kinds of process on the same
stores: the Vira server AND every detached job runner write
data/jobs-log.json (and runners write data/ideas.json when they close out
an idea). An in-memory cache in either process would clobber the other's
writes, and two concurrent read-modify-write cycles would race — so the
stores re-read from disk on every operation and serialize mutations
through this advisory lock (a sidecar <store>.lock file, so the atomic
tmp+rename on the store itself never disturbs the lock inode).

Platform seam: fcntl.flock is Unix-only. On Windows the same contract is
kept with an msvcrt byte-range lock on the sidecar's first byte — LK_LOCK
gives up after ~10 seconds, so it loops until acquired to match flock's
block-forever semantics.
"""
import os
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt

    def _acquire(fh):
        fh.seek(0)
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                continue

    def _release(fh):
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _acquire(fh):
        fcntl.flock(fh, fcntl.LOCK_EX)

    def _release(fh):
        fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def locked(path):
    """Exclusive advisory lock scoped to `path` (any process, any thread)."""
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        _acquire(fh)
        try:
            yield
        finally:
            _release(fh)
