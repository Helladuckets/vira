"""Shared discipline for the small JSON state stores under data/.

server/briefstate.py set the pattern the other little state files kept
half-implementing: fresh read on EVERY operation (no module-level cache —
the server and detached job runners share these files), mutations
serialized through filelock.locked, atomic tmp-then-replace writes, and
oldest-first key-cap pruning. This module is that pattern, written once.

write_atomic is also the single home for the bare tmp+rename idiom used
by stores that carry their own locking (registry saves, import writes):
pass the exact json.dumps kwargs the file has always used — the bytes on
disk are the caller's contract and must not change shape.
"""
import json
from pathlib import Path

from .filelock import locked


def read(path, default):
    """Tolerant fresh read: a missing or corrupt file returns `default`."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_atomic(path, obj, *, newline=False, **dumps_kw):
    """Serialize obj with the caller's exact json.dumps kwargs and write it
    atomically (sibling .tmp, then replace). mkdir -p on the parent. The
    serialized bytes are the caller's on-disk contract — pass the same
    indent/ensure_ascii/sort_keys the store has always used."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(obj, **dumps_kw)
    if newline:
        text += "\n"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def mutate(path, fn, default, **dumps_kw):
    """Locked read-modify-write: fresh read under the store's file lock,
    fn(state) mutates in place (or returns a replacement), atomic write
    with the caller's dumps kwargs. Returns the state as written."""
    with locked(path):
        s = read(path, default)
        out = fn(s)
        if out is not None:
            s = out
        write_atomic(path, s, **dumps_kw)
    return s


def prune_oldest(bucket, max_keys):
    """Cap a {key: sortable-stamp} dict in place, dropping oldest first."""
    if len(bucket) > max_keys:
        for k in sorted(bucket, key=bucket.get)[:len(bucket) - max_keys]:
            bucket.pop(k, None)
