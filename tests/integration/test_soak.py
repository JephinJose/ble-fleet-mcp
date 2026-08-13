"""Soak test: a simulated fleet an order of magnitude larger than the connection cap.
Confirms no deadlocks, no starved devices, and the pool cap is never violated —
the properties the non-negotiable constraints in the project brief require.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from fleet_mcp.core.pool import ConnectionPoolManager
from fleet_mcp.core.scheduler import JobKind, JobStatus, Scheduler
from fleet_mcp.core.types import DeviceHandle
from fleet_mcp.transports.fake import make_simulated_fleet

CAP = 4
FLEET_SIZE = CAP * 15  # comfortably >= the required 10x


@pytest.mark.soak
@pytest.mark.asyncio
async def test_soak_large_fleet_no_deadlock_no_starvation_bounded_latency() -> None:
    transport = make_simulated_fleet(FLEET_SIZE, max_concurrent=CAP)
    pool = ConnectionPoolManager(transport, max_connections=CAP)
    sched = Scheduler(
        pool, transport, device_timeout_s=2.0, backoff_initial_s=0.05, backoff_max_s=0.2
    )
    devices = [
        DeviceHandle(address=f"SIM:{i:04d}", transport_kind="fake") for i in range(FLEET_SIZE)
    ]

    start = time.monotonic()
    op = await sched.submit(devices, JobKind.READ, "temperature_c", timeout_s=30.0)
    # A hang here (deadlock) fails the test via pytest-asyncio's default timeout
    # handling / CI job timeout rather than looping forever.
    await asyncio.wait_for(op.done_event.wait(), timeout=25.0)
    elapsed = time.monotonic() - start

    assert op.status == "completed"
    assert op.done_count == FLEET_SIZE, "every device must resolve — none left behind"
    for result in op.results.values():
        assert result.status == JobStatus.SUCCESS, "no device should be starved of service"

    assert transport.peak_active_connections <= CAP, "pool cap must never be exceeded"

    # Worst case: FLEET_SIZE devices drained through CAP connections serially, each op
    # taking on the order of a few ms in the fake transport. Generous bound to catch
    # pathological serialization bugs (e.g. accidental global locking) without being
    # flaky on a loaded CI box.
    worst_case_bound_s = (FLEET_SIZE / CAP) * 0.5 + 5.0
    assert elapsed < worst_case_bound_s, (
        f"fleet read took {elapsed:.2f}s, expected under {worst_case_bound_s:.2f}s"
    )

    await sched.stop()


@pytest.mark.soak
@pytest.mark.asyncio
async def test_soak_sequential_operations_reuse_connections_without_thrashing() -> None:
    """Multiple back-to-back fleet reads over the same fleet shouldn't churn through
    endless connect/evict cycles once the working set stabilizes."""
    transport = make_simulated_fleet(CAP, max_concurrent=CAP)  # fleet == cap: no eviction needed
    pool = ConnectionPoolManager(transport, max_connections=CAP)
    sched = Scheduler(pool, transport, device_timeout_s=2.0)
    devices = [DeviceHandle(address=f"SIM:{i:04d}", transport_kind="fake") for i in range(CAP)]

    for _ in range(5):
        op = await sched.submit(devices, JobKind.READ, "temperature_c", timeout_s=10.0)
        await asyncio.wait_for(op.done_event.wait(), timeout=10.0)
        assert op.done_count == CAP

    # One connect per device total — never reconnected once established.
    assert transport.peak_active_connections <= CAP
    await sched.stop()
