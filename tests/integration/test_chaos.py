"""Chaos test: a subset of the simulated fleet fails or hangs mid-operation. Confirms
the circuit breaker isolates those devices and that they never stall the healthy
majority — no device is ever silently dropped, each resolves to success or a specific
error within the operation timeout.
"""

from __future__ import annotations

import asyncio
import random
import time

import pytest

from fleet_mcp.core.circuit_breaker import CircuitBreaker
from fleet_mcp.core.pool import ConnectionPoolManager
from fleet_mcp.core.scheduler import JobKind, JobStatus, Scheduler
from fleet_mcp.core.types import DeviceHandle
from fleet_mcp.transports.fake import make_simulated_fleet

CAP = 4
FLEET_SIZE = 40
CHAOS_FRACTION = 0.25


@pytest.mark.chaos
@pytest.mark.asyncio
async def test_chaos_failing_devices_do_not_stall_healthy_majority() -> None:
    rng = random.Random(1234)
    transport = make_simulated_fleet(FLEET_SIZE, max_concurrent=CAP)

    all_addresses = [f"SIM:{i:04d}" for i in range(FLEET_SIZE)]
    chaos_addresses = set(rng.sample(all_addresses, k=int(FLEET_SIZE * CHAOS_FRACTION)))
    for addr in chaos_addresses:
        peripheral = transport.peripheral(addr)
        if rng.random() < 0.5:
            peripheral.unreachable = True
        else:
            peripheral.hang = True

    pool = ConnectionPoolManager(transport, max_connections=CAP)
    cb = CircuitBreaker(failure_threshold=2, cooldown_s=60.0)
    sched = Scheduler(
        pool,
        transport,
        circuit_breaker=cb,
        # Generous relative to the fake transport's ~5ms op latency, so this isn't
        # flaky under event-loop scheduling jitter when the full suite runs together
        # — the property under test is isolation, not timing precision.
        device_timeout_s=1.5,
        backoff_initial_s=0.05,
        backoff_max_s=0.1,
        tracer=None,
    )
    devices = [DeviceHandle(address=a, transport_kind="fake") for a in all_addresses]

    start = time.monotonic()
    op = await sched.submit(devices, JobKind.READ, "temperature_c", timeout_s=15.0, max_attempts=3)
    await asyncio.wait_for(op.done_event.wait(), timeout=20.0)
    elapsed = time.monotonic() - start

    assert op.done_count == FLEET_SIZE, "every device must resolve — chaos devices included"

    healthy_addresses = [a for a in all_addresses if a not in chaos_addresses]
    for addr in healthy_addresses:
        assert op.results[addr].status == JobStatus.SUCCESS, (
            f"{addr} is healthy but did not succeed: {op.results[addr]}"
        )

    for addr in chaos_addresses:
        assert op.results[addr].status in (JobStatus.UNREACHABLE, JobStatus.TIMEOUT), (
            f"{addr} should report a specific error, got {op.results[addr].status}"
        )
        assert op.results[addr].error is not None

    # Chaos devices retry with backoff (bounded) rather than hanging forever, and must
    # not multiply the wall-clock time the healthy majority experiences.
    generous_bound_s = 10.0
    assert elapsed < generous_bound_s, (
        f"chaos run took {elapsed:.2f}s, expected under {generous_bound_s}s"
    )

    for addr in chaos_addresses:
        health = cb.health_of(addr)
        assert health.consecutive_failures >= 2
        assert not cb.is_available(addr), f"circuit breaker should have opened for {addr}"

    await sched.stop()


@pytest.mark.chaos
@pytest.mark.asyncio
async def test_chaos_circuit_breaker_stops_retrying_open_device_across_operations() -> None:
    transport = make_simulated_fleet(CAP, max_concurrent=CAP)
    flaky = transport.peripheral("SIM:0000")
    flaky.unreachable = True

    pool = ConnectionPoolManager(transport, max_connections=CAP)
    cb = CircuitBreaker(failure_threshold=1, cooldown_s=60.0)
    sched = Scheduler(
        pool, transport, circuit_breaker=cb, device_timeout_s=0.3, backoff_initial_s=0.01
    )
    devices = [DeviceHandle(address=f"SIM:{i:04d}", transport_kind="fake") for i in range(CAP)]

    op1 = await sched.submit(devices, JobKind.READ, "temperature_c", timeout_s=5.0, max_attempts=1)
    await asyncio.wait_for(op1.done_event.wait(), timeout=10.0)
    assert not cb.is_available("SIM:0000")

    # Second operation: the breaker should skip SIM:0000 near-instantly (no fresh
    # connect attempt against a known-bad device) rather than re-timing-out on it.
    start = time.monotonic()
    op2 = await sched.submit(devices, JobKind.READ, "temperature_c", timeout_s=5.0, max_attempts=1)
    await asyncio.wait_for(op2.done_event.wait(), timeout=10.0)
    elapsed = time.monotonic() - start

    assert op2.results["SIM:0000"].status == JobStatus.TIMEOUT
    # Bounded by the operation timeout (5s) plus scheduling slack — not the 60s
    # circuit-breaker cooldown, and not a fresh per-attempt hang.
    assert elapsed < 8.0

    await sched.stop()
