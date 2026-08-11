import errno
import time

import pytest


def test_host_connectivity_errors_are_classified_through_exception_chain():
    from agent.network_circuit_breaker import is_host_connectivity_error

    low = OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")
    wrapped = RuntimeError("provider wrapper")
    wrapped.__cause__ = low

    assert is_host_connectivity_error(wrapped) is True
    assert is_host_connectivity_error(OSError(errno.EHOSTUNREACH, "No route to host")) is True
    assert is_host_connectivity_error(TimeoutError("ordinary timeout")) is False


def test_breaker_opens_after_repeated_host_connectivity_errors(monkeypatch):
    from agent.network_circuit_breaker import NetworkCircuitBreaker, NetworkCircuitOpen

    now = {"t": 1000.0}
    monkeypatch.setattr(time, "time", lambda: now["t"])
    breaker = NetworkCircuitBreaker(threshold=2, cooldown_seconds=30)

    breaker.before_request("provider")
    breaker.record_failure(OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address"), surface="provider")
    breaker.before_request("provider")
    breaker.record_failure(OSError(errno.EHOSTUNREACH, "No route to host"), surface="provider")

    with pytest.raises(NetworkCircuitOpen):
        breaker.before_request("provider")

    now["t"] += 31
    breaker.before_request("provider")
