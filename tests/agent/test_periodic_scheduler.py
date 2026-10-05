"""agent/periodic_scheduler: one shared thread runs every periodic timer."""

import threading
import time
from types import SimpleNamespace

from agent import periodic_scheduler
from agent.periodic_scheduler import PeriodicScheduler, schedule


def _wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def test_two_intervals_fire_proportionally_and_cancel_stops_one(monkeypatch):
    sched = PeriodicScheduler()
    clock = [0.0]
    monkeypatch.setattr(periodic_scheduler, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    fast, slow = [], []
    h_fast = sched.schedule(lambda: fast.append(clock[0]), 0.125)
    h_slow = sched.schedule(lambda: slow.append(clock[0]), 0.625)

    def advance(t, expected_fast, expected_slow):
        with sched._cond:
            clock[0] = t
            sched._cond.notify_all()
            assert sched._cond.wait_for(
                lambda: len(fast) == expected_fast and len(slow) == expected_slow,
                timeout=2.0,
            ), (fast, slow)

    try:
        for tick in range(1, 16):
            advance(tick * 0.125, tick, tick // 5)
        assert len(fast) == 5 * len(slow)
        # Scheduling more timers does not create another worker thread.
        before = threading.active_count()
        sched.schedule(lambda: None, 0.01).cancel()
        assert threading.active_count() == before
        assert sched._thread is not None and sched._thread.is_alive()

        h_fast.cancel()
        advance(2.5, 15, 4)
        assert len(fast) == 15, "cancelled callback kept firing"
        assert len(slow) == 4, "sibling callback stopped when another was cancelled"
    finally:
        h_fast.cancel()
        h_slow.cancel()


def test_raising_callback_is_rescheduled_and_does_not_kill_sibling():
    sched = PeriodicScheduler()
    boom, ok = [], []

    def raises():
        boom.append(1)
        raise RuntimeError("bad callback")

    h1 = sched.schedule(raises, 0.01)
    h2 = sched.schedule(lambda: ok.append(1), 0.01)
    assert _wait_until(lambda: len(boom) >= 3 and len(ok) >= 3)
    h1.cancel()
    h2.cancel()


def test_returning_false_stops_callback_and_cancel_wait_joins_inflight():
    sched = PeriodicScheduler()
    calls = []
    sched.schedule(lambda: (calls.append(1), False)[1], 0.01)
    assert _wait_until(lambda: len(calls) == 1)
    time.sleep(0.05)
    assert calls == [1]

    entered = threading.Event()
    release = threading.Event()

    def blocking():
        entered.set()
        release.wait(2.0)

    h = sched.schedule(blocking, 0.01)
    assert entered.wait(2.0)
    threading.Timer(0.05, release.set).start()
    t0 = time.monotonic()
    h.cancel(wait=2.0)  # returns once the in-flight run finished
    assert release.is_set()
    assert time.monotonic() - t0 < 1.5


def test_module_level_schedule_uses_shared_default():
    hits = []
    h = schedule(lambda: hits.append(1), 0.01)
    assert _wait_until(lambda: hits)
    h.cancel()
    thread = periodic_scheduler._DEFAULT._thread
    assert thread is not None and thread.name == "hermes-periodic-scheduler"
    # Scheduling more timers on the shared default adds no OS threads.
    before = threading.active_count()
    handles = [schedule(lambda: None, 0.01) for _ in range(20)]
    assert threading.active_count() == before
    for handle in handles:
        handle.cancel()
