from __future__ import annotations

import asyncio

import pytest

from fleet_mcp.core.circuit_breaker import CircuitBreaker
from fleet_mcp.core.pool import ConnectionPoolManager
from fleet_mcp.core.scheduler import JobKind, JobStatus, Priority, Scheduler
from fleet_mcp.core.types import DeviceHandle
from fleet_mcp.transports.fake import FakePeripheral, FakeTransport, make_simulated_fleet


def _devices(transport: FakeTransport, *addrs: str) -> list[DeviceHandle]:
    return [DeviceHandle(address=a, transport_kind="fake") for a in addrs]


async def _wait_done(op, timeout: float = 5.0) -> None:
    await asyncio.wait_for(op.done_event.wait(), timeout=timeout)


@pytest.mark.asyncio
async def test_read_all_devices_returns_success() -> None:
    transport = make_simulated_fleet(10, max_concurrent=3, resource="temperature_c")
    pool = ConnectionPoolManager(transport, max_connections=3)
    sched = Scheduler(pool, transport, device_timeout_s=2.0)
    devices = [DeviceHandle(address=f"SIM:{i:04d}", transport_kind="fake") for i in range(10)]

    op = await sched.submit(devices, JobKind.READ, "temperature_c", timeout_s=5.0)
    await _wait_done(op)

    assert op.done_count == 10
    for result in op.results.values():
        assert result.status == JobStatus.SUCCESS
        assert result.reading is not None
    await sched.stop()


@pytest.mark.asyncio
async def test_never_exceeds_pool_cap_under_fleet_larger_than_cap() -> None:
    transport = make_simulated_fleet(30, max_concurrent=3)
    pool = ConnectionPoolManager(transport, max_connections=3)
    sched = Scheduler(pool, transport, device_timeout_s=2.0)
    devices = [DeviceHandle(address=f"SIM:{i:04d}", transport_kind="fake") for i in range(30)]

    op = await sched.submit(devices, JobKind.READ, "temperature_c", timeout_s=10.0)
    await _wait_done(op)

    assert transport.peak_active_connections <= 3
    assert op.done_count == 30
    await sched.stop()


@pytest.mark.asyncio
async def test_unreachable_device_reports_specific_error_not_dropped() -> None:
    transport = FakeTransport(max_concurrent=2)
    transport.add_peripheral(FakePeripheral(address="good", resources={"r": 1}))
    transport.add_peripheral(FakePeripheral(address="bad", unreachable=True))
    pool = ConnectionPoolManager(transport, max_connections=2)
    sched = Scheduler(
        pool, transport, device_timeout_s=1.0, backoff_initial_s=0.01, backoff_max_s=0.02
    )

    op = await sched.submit(
        _devices(transport, "good", "bad"), JobKind.READ, "r", timeout_s=5.0, max_attempts=2
    )
    await _wait_done(op)

    assert op.results["good"].status == JobStatus.SUCCESS
    assert op.results["bad"].status == JobStatus.UNREACHABLE
    assert op.results["bad"].error is not None
    await sched.stop()


@pytest.mark.asyncio
async def test_slow_device_does_not_stall_the_rest() -> None:
    transport = FakeTransport(max_concurrent=2)
    transport.add_peripheral(FakePeripheral(address="hung", hang=True))
    for i in range(5):
        transport.add_peripheral(FakePeripheral(address=f"fast{i}", resources={"r": i}))
    pool = ConnectionPoolManager(transport, max_connections=2)
    sched = Scheduler(
        # Generous relative to the fake transport's ~5ms op latency, so the fast
        # devices aren't flaky under event-loop scheduling jitter when the full
        # suite runs together.
        pool,
        transport,
        device_timeout_s=0.5,
        backoff_initial_s=0.01,
        backoff_max_s=0.02,
    )

    addrs = ["hung"] + [f"fast{i}" for i in range(5)]
    op = await sched.submit(
        _devices(transport, *addrs), JobKind.READ, "r", timeout_s=3.0, max_attempts=1
    )
    await _wait_done(op)

    for i in range(5):
        assert op.results[f"fast{i}"].status == JobStatus.SUCCESS
    assert op.results["hung"].status == JobStatus.TIMEOUT
    await sched.stop()


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_repeated_failures() -> None:
    transport = FakeTransport(max_concurrent=1)
    transport.add_peripheral(FakePeripheral(address="bad", unreachable=True))
    pool = ConnectionPoolManager(transport, max_connections=1)
    cb = CircuitBreaker(failure_threshold=2, cooldown_s=60)
    sched = Scheduler(
        pool,
        transport,
        circuit_breaker=cb,
        device_timeout_s=0.5,
        backoff_initial_s=0.01,
        backoff_max_s=0.01,
    )

    op = await sched.submit(
        _devices(transport, "bad"), JobKind.READ, "r", timeout_s=5.0, max_attempts=5
    )
    await _wait_done(op)

    assert cb.health_of("bad").consecutive_failures >= 2
    assert not cb.is_available("bad")
    await sched.stop()


@pytest.mark.asyncio
async def test_operation_timeout_returns_partial_results() -> None:
    transport = FakeTransport(max_concurrent=1)
    transport.add_peripheral(FakePeripheral(address="ok", resources={"r": 1}, op_latency_s=0.01))
    transport.add_peripheral(FakePeripheral(address="hung", hang=True))
    pool = ConnectionPoolManager(transport, max_connections=1)
    sched = Scheduler(pool, transport, device_timeout_s=5.0)

    op = await sched.submit(_devices(transport, "ok", "hung"), JobKind.READ, "r", timeout_s=0.2)
    await _wait_done(op)

    assert op.status == "timed_out"
    assert op.results["hung"].status == JobStatus.TIMEOUT
    await sched.stop()


@pytest.mark.asyncio
async def test_late_success_after_operation_timeout_does_not_overwrite_timeout_result() -> None:
    """Regression test: a job that finishes just after the operation-level watchdog
    force-times it out used to overwrite the already-reported TIMEOUT result back to
    SUCCESS — the caller could observe op.status == "timed_out" while a per-device
    result silently flipped to success behind its back. First resolution should win."""
    transport = FakeTransport(max_concurrent=1)
    # op_latency_s (~0.15s) is comfortably longer than the operation timeout (0.05s)
    # but well inside the per-job device_timeout_s (5s), so the read genuinely
    # completes *after* the operation has already been force-timed-out.
    transport.add_peripheral(FakePeripheral(address="slow", resources={"r": 1}, op_latency_s=0.15))
    pool = ConnectionPoolManager(transport, max_connections=1)
    sched = Scheduler(pool, transport, device_timeout_s=5.0)

    op = await sched.submit(_devices(transport, "slow"), JobKind.READ, "r", timeout_s=0.05)
    await _wait_done(op)
    assert op.status == "timed_out"
    assert op.results["slow"].status == JobStatus.TIMEOUT

    # Let the in-flight read actually finish and try to report back.
    await asyncio.sleep(0.3)
    assert op.results["slow"].status == JobStatus.TIMEOUT, (
        "a late success must not overwrite the already-reported timeout"
    )
    await sched.stop()


@pytest.mark.asyncio
async def test_write_requires_verify_after_write_convergence() -> None:
    transport = FakeTransport(max_concurrent=1)
    transport.add_peripheral(FakePeripheral(address="d0", resources={"brightness": 0}))
    pool = ConnectionPoolManager(transport, max_connections=1)
    sched = Scheduler(pool, transport, device_timeout_s=1.0)

    op = await sched.submit(
        _devices(transport, "d0"), JobKind.WRITE, "brightness", value=80, timeout_s=2.0
    )
    await _wait_done(op)

    result = op.results["d0"]
    assert result.status == JobStatus.SUCCESS
    assert result.write_result is not None
    assert result.write_result.converged is True
    assert result.write_result.readback_value == 80
    await sched.stop()


@pytest.mark.asyncio
async def test_write_rejected_is_not_retried_and_reported() -> None:
    transport = FakeTransport(max_concurrent=1)
    transport.add_peripheral(FakePeripheral(address="d0", reject_writes=True))
    pool = ConnectionPoolManager(transport, max_connections=1)
    sched = Scheduler(pool, transport, device_timeout_s=1.0)

    op = await sched.submit(_devices(transport, "d0"), JobKind.WRITE, "r", value=1, timeout_s=2.0)
    await _wait_done(op)

    assert op.results["d0"].status == JobStatus.WRITE_REJECTED
    assert op.results["d0"].attempts == 1
    await sched.stop()


@pytest.mark.asyncio
async def test_already_connected_devices_batch_ahead_of_new_connections() -> None:
    """Batching: jobs targeting already-connected devices are drained before jobs
    that would require a new connection (and possibly an eviction) — even when the
    new-connection job was queued first."""
    order: list[str] = []

    class RecordingTransport(FakeTransport):
        async def read(self, conn, resource):  # type: ignore[override]
            order.append(conn.device.address)
            return await super().read(conn, resource)

    transport = RecordingTransport(max_concurrent=2)
    for addr in ("A", "B", "C"):
        transport.add_peripheral(FakePeripheral(address=addr, resources={"r": 1}))
    pool = ConnectionPoolManager(transport, max_connections=2)
    sched = Scheduler(pool, transport, device_timeout_s=1.0)

    # Pre-connect A and B (fills the cap=2 pool), then release them back to idle.
    warmup = await sched.submit(_devices(transport, "A", "B"), JobKind.READ, "r", timeout_s=3.0)
    await _wait_done(warmup)
    assert pool.is_connected("A") and pool.is_connected("B")
    order.clear()

    # C is listed *first* (earliest created_at) but isn't connected; A and B are
    # connected but listed after. Batching should still dispatch A and B first.
    op = await sched.submit(_devices(transport, "C", "A", "B"), JobKind.READ, "r", timeout_s=3.0)
    await _wait_done(op)

    assert set(order[:2]) == {"A", "B"}, f"expected A and B dispatched first, got {order}"
    assert order[2] == "C"
    await sched.stop()


@pytest.mark.asyncio
async def test_high_priority_dispatched_before_low_priority() -> None:
    transport = FakeTransport(max_concurrent=1)
    order: list[str] = []

    class RecordingTransport(FakeTransport):
        async def read(self, conn, resource):  # type: ignore[override]
            order.append(conn.device.address)
            return await super().read(conn, resource)

    transport = RecordingTransport(max_concurrent=1)
    for addr in ("low1", "low2", "high1"):
        transport.add_peripheral(
            FakePeripheral(address=addr, resources={"r": 1}, op_latency_s=0.02)
        )
    pool = ConnectionPoolManager(transport, max_connections=1)
    sched = Scheduler(pool, transport, device_timeout_s=1.0)

    # Submit low-priority jobs first, then a high-priority one shortly after;
    # the high-priority job should still jump the remaining queue.
    op_low = await sched.submit(
        _devices(transport, "low1", "low2"), JobKind.READ, "r", priority=Priority.LOW, timeout_s=3.0
    )
    await asyncio.sleep(0.005)
    op_high = await sched.submit(
        _devices(transport, "high1"), JobKind.READ, "r", priority=Priority.HIGH, timeout_s=3.0
    )
    await _wait_done(op_low)
    await _wait_done(op_high)

    assert order[0] == "low1"  # already in flight when high1 was submitted
    assert "high1" in order[1:]
    assert order.index("high1") < order.index("low2")
    await sched.stop()
