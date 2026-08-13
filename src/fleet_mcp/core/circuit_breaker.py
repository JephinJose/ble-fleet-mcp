"""Per-device circuit breaker / health tracking.

After N consecutive failures a device is marked UNHEALTHY and skipped for a cooldown
period rather than retried forever, so one bad device can't consume the scheduler's
attention (or the pool's connection slots) indefinitely.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class HealthState(str, Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


@dataclass(slots=True)
class DeviceHealth:
    address: str
    state: HealthState = HealthState.HEALTHY
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_failure_reason: str | None = None
    last_failure_at: float | None = None
    unhealthy_since: float | None = None
    cooldown_until: float | None = None
    total_failures: int = 0
    total_successes: int = 0


@dataclass(slots=True)
class CircuitBreaker:
    failure_threshold: int = 3
    cooldown_s: float = 60.0
    _health: dict[str, DeviceHealth] = field(default_factory=dict)

    def _get(self, address: str) -> DeviceHealth:
        h = self._health.get(address)
        if h is None:
            h = DeviceHealth(address=address)
            self._health[address] = h
        return h

    def record_success(self, address: str) -> None:
        h = self._get(address)
        h.consecutive_failures = 0
        h.consecutive_successes += 1
        h.total_successes += 1
        h.state = HealthState.HEALTHY
        h.unhealthy_since = None
        h.cooldown_until = None

    def record_failure(self, address: str, reason: str) -> None:
        h = self._get(address)
        h.consecutive_successes = 0
        h.consecutive_failures += 1
        h.total_failures += 1
        h.last_failure_reason = reason
        h.last_failure_at = time.monotonic()
        if h.consecutive_failures >= self.failure_threshold and h.state == HealthState.HEALTHY:
            h.state = HealthState.UNHEALTHY
            h.unhealthy_since = time.monotonic()
            h.cooldown_until = time.monotonic() + self.cooldown_s

    def is_available(self, address: str) -> bool:
        """True if the device may be scheduled now (healthy, or cooldown has elapsed)."""
        h = self._health.get(address)
        if h is None or h.state == HealthState.HEALTHY:
            return True
        assert h.cooldown_until is not None
        # Half-open: once the cooldown elapses, allow a single probe attempt through.
        return time.monotonic() >= h.cooldown_until

    def health_of(self, address: str) -> DeviceHealth:
        return self._get(address)

    def snapshot(self) -> dict[str, DeviceHealth]:
        return dict(self._health)
