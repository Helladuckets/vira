"""Event-loop stall detector — the thing that was missing when the server
went dark for 90 seconds and left nothing behind to read.

The 2026-07-27 stalls were only ever visible from OUTSIDE: curl timing
out, playwright's page.goto giving up at 30s, the topbar stuck on
"connecting...". Inside the process there was no trace at all. uvicorn's
access log carries no timestamps, so a 90-second hole in it is
indistinguishable from a quiet minute, and by the time anyone looked the
process had recovered and the evidence was gone. Diagnosing it needed a
live reproduction and a stack sample taken by hand, mid-stall.

This module makes the process notice its own freeze. Two halves, and the
split matters:

  1. A HEARTBEAT COROUTINE on the event loop, stamping a monotonic clock
     every INTERVAL. When the loop is starved this simply stops running,
     and the stamp goes stale. Loop lag is exactly that staleness.
  2. A WATCHDOG THREAD, deliberately NOT on the loop, reading the stamp.
     A detector living on the surface it monitors cannot report the one
     condition it exists to catch. The thread is a GIL contender like any
     other, so it is late during a stall too — but it gets scheduled,
     which is the whole requirement, and it captures stacks WHILE the
     stall is happening rather than after it clears.

What it records: when the stall started, how long it lasted, the peak lag,
and what every thread was executing at the moment it was first noticed.
That last part is the diagnosis — GIL starvation and a deadlock look
identical from outside and completely different in the frames.

Stalls land in the launchd log with real timestamps (the access log's gap
is not enough to reconstruct one) and in a ring buffer served at
/api/health/loop, alongside the admission gate's counters. A stall that
correlates with a full CPU gate is starvation the gate throttled too
late; a stall with an idle gate is CPU coming from somewhere the gate
does not cover, which is the next thing to go look at.
"""
import asyncio
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

from . import settings

INTERVAL = 0.5        # heartbeat cadence
TICK = 0.5            # how often the watchdog thread checks the stamp
THRESHOLD = 2.0       # lag past which the loop counts as stalled
KEEP = 25             # stalls retained in the ring buffer


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _thread_frames(limit_threads=16, depth=4):
    """Innermost frames of every live thread, right now. Best-effort: a
    thread that dies mid-walk is skipped, never raised."""
    names = {t.ident: t.name for t in threading.enumerate()}
    out = []
    try:
        frames = sys._current_frames()
    except Exception:  # noqa: BLE001 — diagnostics never break the server
        return out
    for tid, frame in list(frames.items())[:limit_threads]:
        try:
            stack = traceback.extract_stack(frame)[-depth:]
            out.append({
                "thread": names.get(tid, str(tid)),
                "stack": [f"{f.filename.split('/')[-1]}:{f.lineno} {f.name}"
                          for f in stack],
            })
        except Exception:  # noqa: BLE001
            continue
    return out


class Watcher:
    """Measures event-loop lag and records the stalls it finds."""

    def __init__(self, threshold=None, interval=INTERVAL, tick=TICK):
        if threshold is None:
            try:                 # settings must never break the watchdog
                threshold = settings.get("loop_stall_threshold_s")
            except Exception:  # noqa: BLE001
                threshold = None
        self.threshold = float(threshold or THRESHOLD)
        self.interval = interval
        self.tick = tick
        self._beat = time.monotonic()
        self._stop = threading.Event()
        self._thread = None
        self._task = None
        self._lock = threading.Lock()
        self.stalls = []          # newest last, capped at KEEP
        self.worst_lag = 0.0
        self.started = None
        # A stall in progress, visible while it lasts — a record only
        # exists after the clear, so anything that needs to see the stall
        # AS IT HAPPENS (the health payload, a test holding a block open
        # until the watchdog has noticed) reads this, not `stalls`.
        self.stalling = False

    # ----- the two halves -----

    async def _heartbeat(self):
        while not self._stop.is_set():
            self._beat = time.monotonic()
            await asyncio.sleep(self.interval)

    def lag(self):
        """Seconds the loop is overdue. Zero when it is keeping up."""
        return max(0.0, time.monotonic() - self._beat - self.interval)

    def _run(self):
        began, noticed, peak, frames = 0.0, "", 0.0, None
        while not self._stop.is_set():
            lag = self.lag()
            if lag >= self.threshold:
                if not self.stalling:
                    self.stalling, noticed = True, _now()
                    # The stall began `lag` seconds ago, not now: the
                    # watchdog only learns of it once the stamp is already
                    # stale, so back-date the start or every stall reads
                    # THRESHOLD seconds shorter than it was.
                    began = time.monotonic() - lag
                    peak, frames = lag, _thread_frames()
                else:
                    peak = max(peak, lag)
            elif self.stalling:
                self.stalling = False
                self._record(noticed, time.monotonic() - began, peak, frames)
            self._stop.wait(self.tick)

    def _record(self, noticed, duration, peak, frames):
        from . import admission          # local: avoid an import cycle
        entry = {
            "noticed": noticed,
            "duration_s": round(duration, 2),
            "peak_lag_s": round(peak, 2),
            "gate": admission.gate.stats(),
            "threads": frames or [],
        }
        with self._lock:
            self.stalls.append(entry)
            del self.stalls[:-KEEP]
            self.worst_lag = max(self.worst_lag, peak)
        # stderr, because the launchd log is the only place a stall from
        # last week can still be found — and unlike the access log, this
        # line carries the time it happened.
        busy = ", ".join(t["thread"] for t in (frames or [])[:6])
        print(f"[loopwatch] {noticed} event loop stalled "
              f"{entry['duration_s']}s (peak lag {entry['peak_lag_s']}s) "
              f"gate={entry['gate']['waiting']} waiting / "
              f"{entry['gate']['admitted']} admitted; threads: {busy}",
              file=sys.stderr, flush=True)

    # ----- lifecycle -----

    def start(self, loop=None):
        """Called from the startup hook, on the loop it will watch."""
        if self._thread is not None:
            return
        loop = loop or asyncio.get_running_loop()
        self._task = loop.create_task(self._heartbeat())
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="vira-loopwatch")
        self._thread.start()
        self.started = _now()

    def stop(self):
        self._stop.set()
        if self._task is not None:
            self._task.cancel()

    def snapshot(self):
        from . import admission
        with self._lock:
            stalls = list(self.stalls)
        return {
            "watching": self._thread is not None,
            "started": self.started,
            "threshold_s": self.threshold,
            "lag_now_s": round(self.lag(), 3),
            "stalling": self.stalling,
            "worst_lag_s": round(self.worst_lag, 2),
            "stall_count": len(stalls),
            "stalls": list(reversed(stalls)),     # newest first for the UI
            "gate": admission.gate.stats(),
            "cpu_count": admission.CPU_COUNT,
        }


watcher = Watcher()
