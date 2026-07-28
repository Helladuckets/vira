"""Admission control for CPU-bound request work — the guard that keeps one
busy window from freezing the whole server.

WHAT THIS FIXES, measured rather than assumed (2026-07-28). Vira serves
every route as a sync `def`, so handlers run on anyio's worker threads
while the event loop runs on the main thread. That is the right shape for
I/O — 200 concurrent trivial requests cost nothing measurable. It is the
wrong shape for CPU, because CPython gives the whole process ONE
interpreter: worker threads doing Python work hold the GIL, and the event
loop thread is just another GIL contender. Starve it and uvicorn cannot
accept a connection, dispatch a request, or push an SSE frame, so EVERY
endpoint goes dark at once — including the async ones, which is the
symptom that makes this look like a crash rather than a slow handler.

The numbers from the live instance, 60 concurrent requests to one 0.2s
CPU-bound route (/api/ideas/duplicates):

    load               sync probe   event-loop probe
    200x trivial          0.003s          0.003s     no effect at all
    60x CPU-bound         9.081s          7.870s     whole process dark

A stack sample taken during the stall put the event-loop thread in
__psynch_cvwait inside the evaluator: parked waiting for the GIL, not
waiting on I/O. Stall length tracked total queued CPU work almost exactly
(N=6 -> 0.55s, N=16 -> 2.2s, N=40 -> 5.7s), because there is no fairness
anywhere in that path — the interpreter is one resource and the request
path admitted unbounded work into it.

So the fix is admission control, not a bigger threadpool: a threadpool
only decides how many threads QUEUE for the GIL, never how much Python
runs at once. SLOTS is deliberately small. Pure-Python work gains nothing
from a second slot (the GIL serializes it regardless); the default of 2
exists because the scoring paths spend part of their time in numpy, which
releases the GIL, so a little overlap is real. Raising this does not make
the server faster, it makes stalls longer.

WAITING IS THE FEATURE. A request that queues 300ms behind two others is
working correctly; a server that answers nothing for 90 seconds is not.
MAX_WAIT is the ceiling on that queue, and passing it raises Full, which
main.py turns into a 503 with Retry-After. That is deliberate: Vira's
weakest layer is silent failure, and a bounded honest refusal beats an
unbounded hang that the client reads as "Looking..." forever.

Cross-check the effect with /api/health/loop, which reports both this
gate's counters and the measured event-loop lag (see server/loopwatch.py).
"""
import os
import threading
import time
from contextlib import contextmanager

from . import settings


class Full(Exception):
    """No CPU slot came free inside MAX_WAIT. Carries the queue depth so
    the 503 can say how busy it actually was, not just that it failed."""

    def __init__(self, label, waited, depth):
        self.label = label
        self.waited = waited
        self.depth = depth
        super().__init__(
            f"{label}: no CPU slot after {waited:.1f}s ({depth} waiting)")


def _cfg(key, default):
    try:
        v = settings.get(key)
    except Exception:  # noqa: BLE001 — settings must never break admission
        v = None
    return default if v is None else v


class CpuGate:
    """A counting gate over the CPU-heavy request paths, plus the counters
    that make its behavior legible after the fact."""

    def __init__(self, slots=None, max_wait=None):
        self.slots = max(1, int(slots if slots is not None
                                else _cfg("cpu_slots", 2)))
        self.max_wait = float(max_wait if max_wait is not None
                              else _cfg("cpu_max_wait_s", 15))
        self._sem = threading.BoundedSemaphore(self.slots)
        self._lock = threading.Lock()
        self.waiting = 0
        self.peak_waiting = 0
        self.admitted = 0
        self.rejected = 0
        self.peak_wait_s = 0.0
        self.peak_hold_s = 0.0
        self.busiest = ""

    @contextmanager
    def slot(self, label="cpu"):
        """Hold one CPU slot for the duration of the block. Blocks until a
        slot frees or MAX_WAIT elapses, then raises Full."""
        t0 = time.monotonic()
        with self._lock:
            self.waiting += 1
            self.peak_waiting = max(self.peak_waiting, self.waiting)
        try:
            got = self._sem.acquire(timeout=self.max_wait)
        finally:
            with self._lock:
                self.waiting -= 1
        waited = time.monotonic() - t0
        if not got:
            with self._lock:
                self.rejected += 1
            raise Full(label, waited, self.waiting)
        with self._lock:
            self.admitted += 1
            if waited > self.peak_wait_s:
                self.peak_wait_s = waited
        held = time.monotonic()
        try:
            yield
        finally:
            self._sem.release()
            dt = time.monotonic() - held
            with self._lock:
                if dt > self.peak_hold_s:
                    self.peak_hold_s = dt
                    self.busiest = label

    def stats(self):
        with self._lock:
            return {
                "slots": self.slots,
                "max_wait_s": self.max_wait,
                "waiting": self.waiting,
                "peak_waiting": self.peak_waiting,
                "admitted": self.admitted,
                "rejected": self.rejected,
                "peak_wait_s": round(self.peak_wait_s, 3),
                "peak_hold_s": round(self.peak_hold_s, 3),
                "slowest_path": self.busiest,
            }

    def reset_peaks(self):
        with self._lock:
            self.peak_waiting = self.waiting
            self.peak_wait_s = 0.0
            self.peak_hold_s = 0.0
            self.busiest = ""


# The one gate. Module-level so every CPU-heavy path shares the same
# budget — two separately-sized gates would add up to more concurrent
# Python than either intended, which is the bug this module exists to stop.
gate = CpuGate()


@contextmanager
def cpu(label="cpu"):
    """`with admission.cpu("ideas.related"):` around CPU-heavy request work."""
    with gate.slot(label):
        yield


def is_worker():
    """True when called on an anyio worker thread (a request), false on a
    background thread. Background passes should NOT take a request slot:
    they are not what the gate is protecting, and a background pass that
    waits 15s for a slot and then raises would look like a tagging failure."""
    return threading.current_thread().name.startswith("AnyIO")


# Machine shape, reported alongside the counters so a stall report from
# another install carries the context needed to read it.
CPU_COUNT = os.cpu_count() or 1
