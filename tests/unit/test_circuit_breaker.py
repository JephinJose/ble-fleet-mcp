from __future__ import annotations

import time

from fleet_mcp.core.circuit_breaker import CircuitBreaker, HealthState


def test_healthy_by_default() -> None:
    cb = CircuitBreaker(failure_threshold=3, cooldown_s=60)
    assert cb.is_available("d0")
    assert cb.health_of("d0").state == HealthState.HEALTHY


def test_opens_after_threshold_consecutive_failures() -> None:
    cb = CircuitBreaker(failure_threshold=3, cooldown_s=60)
    cb.record_failure("d0", "timeout")
    cb.record_failure("d0", "timeout")
    assert cb.is_available("d0")  # still under threshold
    cb.record_failure("d0", "timeout")
    assert cb.health_of("d0").state == HealthState.UNHEALTHY
    assert not cb.is_available("d0")


def test_success_resets_consecutive_failures() -> None:
    cb = CircuitBreaker(failure_threshold=3, cooldown_s=60)
    cb.record_failure("d0", "timeout")
    cb.record_failure("d0", "timeout")
    cb.record_success("d0")
    assert cb.health_of("d0").consecutive_failures == 0
    cb.record_failure("d0", "timeout")
    cb.record_failure("d0", "timeout")
    assert cb.is_available("d0")  # only 2 consecutive since the success reset it


def test_cooldown_expires_and_allows_probe() -> None:
    cb = CircuitBreaker(failure_threshold=1, cooldown_s=0.01)
    cb.record_failure("d0", "timeout")
    assert not cb.is_available("d0")
    time.sleep(0.02)
    assert cb.is_available("d0")


def test_independent_per_device() -> None:
    cb = CircuitBreaker(failure_threshold=1, cooldown_s=60)
    cb.record_failure("d0", "timeout")
    assert not cb.is_available("d0")
    assert cb.is_available("d1")
