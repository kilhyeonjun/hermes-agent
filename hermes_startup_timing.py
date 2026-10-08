"""Bounded startup spans routed through Hermes' existing private log handlers."""

import itertools
import json
import logging
import os
import threading
from time import perf_counter_ns


_STAGES = frozenset({
    "cli.imports_after_watchdog", "cli.agent_startup", "gateway.module_import",
    "gateway.constructor", "gateway.config", "gateway.runtime_settings",
    "gateway.session_store", "gateway.lifecycle_state", "gateway.runtime_caches",
    "gateway.startup_checks", "gateway.session_db", "gateway.registries",
    "mcp.discovery", "mcp.sdk_import", "mcp.lock_wait", "mcp.connection", "mcp.registration",
})
_LOCK_OUTCOMES = frozenset({"acquired", "unavailable", "exhausted"})
_TRACE_IDS = itertools.count(1)
_TRACE_LOCK = threading.Lock()
_LOGGER = logging.getLogger("gateway.startup_timing")


class StartupTiming:
    def __init__(self):
        self._origin_ns = perf_counter_ns()
        self._pid = os.getpid()
        with _TRACE_LOCK:
            self._trace_id = f"{self._pid}:{self._origin_ns}:{next(_TRACE_IDS)}"
        self._lock = threading.Lock()
        self._span_ids = itertools.count(1)
        self._pending = []
        self._ready = False

    def span(self, stage, *, profile_slot=None, server_slot=None):
        if stage not in _STAGES:
            raise ValueError("Unknown startup timing stage")
        slots = {k: v for k, v in {
            "profile_slot": profile_slot, "server_slot": server_slot,
        }.items() if v is not None}
        if any(type(v) is not int or v < 0 for v in slots.values()):
            raise ValueError("Startup timing slots must be nonnegative integers")
        return _Span(self, stage, slots)

    def flush(self):
        """Call after gateway logging configuration; headers reflect flush time."""
        with self._lock:
            self._ready = True
            pending, self._pending = self._pending, []
        for event in pending:
            self._emit(event)

    def _record(self, event):
        with self._lock:
            if not self._ready:
                if len(self._pending) < 128:
                    self._pending.append(event)
                return
        self._emit(event)

    @staticmethod
    def _emit(event):
        # A broken custom log sink must not turn successful startup into failure
        # or replace the original exception. This helper owns no file handlers.
        try:
            _LOGGER.info("STARTUP_TIMING %s", json.dumps(event, separators=(",", ":")))
        except Exception:
            return


class _Span:
    def __init__(self, owner, stage, slots):
        self._owner = owner
        self._stage = stage
        self._slots = slots
        self._lock = threading.Lock()
        self._started_ns = None
        self._finished = False
        self._lock_outcome = None

    @property
    def lock_outcome(self):
        return self._lock_outcome

    @lock_outcome.setter
    def lock_outcome(self, value):
        if value not in _LOCK_OUTCOMES:
            raise ValueError("Unknown discovery lock outcome")
        self._lock_outcome = value

    def __enter__(self):
        with self._lock:
            if self._started_ns is not None:
                return self
            self._started_ns = perf_counter_ns()
            with self._owner._lock:
                self._span_id = next(self._owner._span_ids)
            event = self._event("begin", "running", self._started_ns)
        self._owner._record(event)
        return self

    def _event(self, phase, status, tick_ns):
        return {
            "trace_id": self._owner._trace_id, "span_id": self._span_id,
            "pid": self._owner._pid, "stage": self._stage, "phase": phase,
            "status": status, "elapsed_ms": (tick_ns - self._owner._origin_ns) / 1_000_000,
            **self._slots,
        }

    def __exit__(self, exc_type, exc, tb):
        with self._lock:
            if self._finished:
                return False
            self._finished = True
            tick_ns = perf_counter_ns()
            status = "ok"
            if exc_type is not None:
                from asyncio import CancelledError
                status = "cancelled" if isinstance(exc, CancelledError) else "error"
            event = self._event("end", status, tick_ns)
            event["duration_ms"] = (tick_ns - self._started_ns) / 1_000_000
            if self._lock_outcome is not None:
                event["lock_outcome"] = self._lock_outcome
        self._owner._record(event)
        return False

