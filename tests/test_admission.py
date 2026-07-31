"""The CPU admission gate and the event-loop stall watchdog — the two
halves of the fix for the 2026-07-27 whole-process stalls.

The defect these guard against was measured, not guessed: Vira serves
every route as a sync `def`, so handlers run on anyio worker threads, and
CPython gives the process one interpreter. Enough concurrent Python work
in those threads starves the event loop of the GIL and EVERY endpoint goes
dark at once, async ones included. 60 concurrent requests to a 0.2s
CPU-bound route took the live instance down for 9 seconds; 200 concurrent
trivial requests did nothing at all. The mechanism is CPU, not connections
and not threads, so the guard has to bound CPU.

What is worth testing here is the CONTRACT, not the physics: the gate lets
exactly `slots` through at once, it waits rather than failing while the
wait is reasonable, it refuses honestly instead of hanging forever, and it
never leaks a slot on an exception. Plus: the watchdog notices a blocked
loop and records what the threads were doing.

Run: .venv/bin/python -m unittest discover tests
"""
import asyncio
import threading
import time
import unittest
from unittest import mock

from server import admission, loopwatch


class GateTests(unittest.TestCase):

    def test_slots_bound_concurrency(self):
        """The whole point: N callers, at most `slots` inside at once."""
        gate = admission.CpuGate(slots=2, max_wait=10)
        inside, peak = 0, 0
        lock = threading.Lock()
        release = threading.Event()

        def worker():
            nonlocal inside, peak
            with gate.slot("t"):
                with lock:
                    inside += 1
                    peak = max(peak, inside)
                release.wait(2.0)
                with lock:
                    inside -= 1

        ts = [threading.Thread(target=worker) for _ in range(8)]
        for t in ts:
            t.start()
        time.sleep(0.3)
        self.assertLessEqual(peak, 2)
        release.set()
        for t in ts:
            t.join(5)
        self.assertEqual(inside, 0)
        self.assertEqual(gate.stats()["admitted"], 8)

    def test_queued_callers_are_served_not_refused(self):
        """Waiting is the feature. A short queue must drain, not 503 —
        otherwise the fix trades a stall for an error page."""
        gate = admission.CpuGate(slots=1, max_wait=10)
        done = []

        def worker(i):
            with gate.slot(f"t{i}"):
                time.sleep(0.05)
            done.append(i)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        self.assertEqual(len(done), 6)
        self.assertEqual(gate.stats()["rejected"], 0)

    def test_refuses_honestly_past_max_wait(self):
        """An unbounded queue is the stall wearing a different hat. Past
        max_wait the gate raises Full, which main.py turns into a 503 with
        Retry-After — a named failure the client can act on."""
        gate = admission.CpuGate(slots=1, max_wait=0.2)
        holding = threading.Event()
        release = threading.Event()

        def hog():
            with gate.slot("hog"):
                holding.set()
                release.wait(3.0)

        t = threading.Thread(target=hog)
        t.start()
        self.assertTrue(holding.wait(2.0))
        with self.assertRaises(admission.Full) as cm:
            with gate.slot("late"):
                pass
        self.assertGreaterEqual(cm.exception.waited, 0.15)
        self.assertIn("late", str(cm.exception))
        release.set()
        t.join(5)
        self.assertEqual(gate.stats()["rejected"], 1)

    def test_exception_inside_the_slot_releases_it(self):
        """A handler that raises must not permanently shrink the gate —
        that would turn one bad request into a slow leak toward zero
        capacity, which is the original bug with extra steps."""
        gate = admission.CpuGate(slots=1, max_wait=1)
        for _ in range(3):
            with self.assertRaises(ValueError):
                with gate.slot("boom"):
                    raise ValueError("handler blew up")
        with gate.slot("after"):
            pass
        self.assertEqual(gate.stats()["rejected"], 0)

    def test_stats_report_the_queue(self):
        gate = admission.CpuGate(slots=1, max_wait=5)
        with gate.slot("a"):
            pass
        st = gate.stats()
        self.assertEqual(st["slots"], 1)
        self.assertEqual(st["admitted"], 1)
        self.assertIn("peak_wait_s", st)
        self.assertEqual(st["slowest_path"], "a")

    def test_a_coarse_clock_still_names_the_path(self):
        """Windows CI caught this and macOS could not: time.monotonic() has
        ~15.6ms granularity on Windows before Python 3.13, so a fast path
        measures a hold of exactly 0.0 and a strict `>` left slowest_path
        empty while admitted counted up — a diagnostic contradicting itself.
        Pinned with a frozen clock so the platform stops being what decides
        whether this is tested."""
        gate = admission.CpuGate(slots=1, max_wait=5)
        with mock.patch.object(admission.time, "monotonic", return_value=1.0):
            with gate.slot("frozen"):
                pass
        st = gate.stats()
        self.assertEqual(st["peak_hold_s"], 0.0)
        self.assertEqual(st["slowest_path"], "frozen")

    def test_the_slower_path_still_wins(self):
        """The counterpart: naming the first holder must not pin it there."""
        gate = admission.CpuGate(slots=1, max_wait=5)
        with gate.slot("quick"):
            pass
        with gate.slot("slow"):
            time.sleep(0.05)
        with gate.slot("quick2"):
            pass
        self.assertEqual(gate.stats()["slowest_path"], "slow")


class LoopWatchTests(unittest.TestCase):

    def test_lag_is_zero_on_a_healthy_loop(self):
        w = loopwatch.Watcher(threshold=1.0, interval=0.05, tick=0.05)

        async def main():
            w.start()
            await asyncio.sleep(0.4)
            lag = w.lag()
            w.stop()
            return lag

        self.assertLess(asyncio.run(main()), 0.5)

    def test_a_blocked_loop_is_recorded_with_thread_stacks(self):
        """The hole this closes: on 2026-07-27 the server went dark and
        left nothing behind. A stall must now name itself — how long, how
        bad, and what the threads were executing while it happened.

        Flaked on windows-latest (2026-07-31): a fixed 1.0s block gives
        the watchdog thread a fixed window to get scheduled in, and a slow
        shared VM does not guarantee that. So the block is held OPEN until
        the watchdog has actually noticed (`stalling`), and the clear is
        polled rather than slept past — which OS runs the suite must not
        decide whether this behavior is tested."""
        w = loopwatch.Watcher(threshold=0.3, interval=0.05, tick=0.05)
        unblock = threading.Event()

        def release_once_noticed():
            deadline = time.monotonic() + 10
            while not w.stalling and time.monotonic() < deadline:
                time.sleep(0.01)
            unblock.set()

        helper = threading.Thread(target=release_once_noticed)

        async def main():
            w.start()
            await asyncio.sleep(0.2)   # a few healthy heartbeats first
            helper.start()
            # Block the loop, exactly like GIL starvation — held until the
            # watchdog has seen it, however late the VM schedules it.
            unblock.wait(15)
            deadline = time.monotonic() + 10
            while not w.snapshot()["stall_count"] and time.monotonic() < deadline:
                await asyncio.sleep(0.02)  # heartbeat resumes; watchdog records
            w.stop()

        asyncio.run(main())
        helper.join(5)
        snap = w.snapshot()
        self.assertGreaterEqual(snap["stall_count"], 1)
        stall = snap["stalls"][-1]     # oldest — the one this test staged
        # >= not >: peak is the lag at the notice tick, which can land ON
        # the threshold, and round(x, 2) on a coarse clock can quantize to
        # exactly 0.3 — a strict > here is the granularity flake again.
        self.assertGreaterEqual(stall["duration_s"], 0.3)
        self.assertGreaterEqual(stall["peak_lag_s"], 0.3)
        self.assertTrue(stall["threads"], "no thread stacks captured")
        self.assertIn("gate", stall)

    def test_snapshot_is_serializable(self):
        """It is served at /api/health/loop, so it has to survive JSON."""
        import json
        json.dumps(loopwatch.Watcher(threshold=9).snapshot())


class RouteWiringTests(unittest.TestCase):
    """The gate raises on an anyio WORKER thread, while FastAPI's exception
    handlers run on the event loop. That hop is the part worth proving end
    to end: a Full that does not reach the handler surfaces as a bare 500
    with no Retry-After, which is the silent failure this change exists to
    replace."""

    def setUp(self):
        from fastapi.testclient import TestClient
        from server import main
        self.client = TestClient(main.app, raise_server_exceptions=False)

    def test_a_refused_request_becomes_a_503_the_client_can_read(self):
        import unittest.mock as m
        tiny = admission.CpuGate(slots=1, max_wait=0.05)
        holding = threading.Event()
        release = threading.Event()

        def hog():
            with tiny.slot("hog"):
                holding.set()
                release.wait(5.0)

        with m.patch.object(admission, "gate", tiny):
            t = threading.Thread(target=hog)
            t.start()
            self.assertTrue(holding.wait(2.0))
            r = self.client.get("/api/ideas/duplicates")
            release.set()
            t.join(5)

        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.headers.get("retry-after"), "5")
        body = r.json()
        self.assertEqual(body["error"], "server busy")
        self.assertEqual(body["path"], "/api/ideas/duplicates")
        self.assertIn("waited_s", body)

    def test_health_loop_reports_lag_gate_and_tagger(self):
        d = self.client.get("/api/health/loop").json()
        for key in ("lag_now_s", "threshold_s", "stalls", "gate",
                    "idea_indexer"):
            self.assertIn(key, d)
        # The claim the whole fix rests on, asserted rather than assumed.
        self.assertTrue(d["idea_indexer"]["out_of_process"])


if __name__ == "__main__":
    unittest.main()
