"""Tiny cross-process file lock shared by the JSON stores.

The durable-runner architecture puts two kinds of process on the same
stores: the Vira server AND every detached job runner write
data/jobs-log.json (and runners write data/ideas.json when they close out
an idea). An in-memory cache in either process would clobber the other's
writes, and two concurrent read-modify-write cycles would race — so the
stores re-read from disk on every operation and serialize mutations
through this advisory fcntl lock (a sidecar <store>.lock file, so the
atomic tmp+rename on the store itself never disturbs the lock inode).
"""
import fcntl
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def locked(path):
    """Exclusive advisory lock scoped to `path` (any process, any thread)."""
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
