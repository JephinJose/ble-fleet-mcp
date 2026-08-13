from __future__ import annotations

import asyncio

import pytest

from fleet_mcp.core.pool import ConnectionPoolManager
from fleet_mcp.core.types import ConnState, DeviceHandle
from fleet_mcp.transports.fake import FakePeripheral, FakeTransport


def _device(addr: str) -> DeviceHandle:
    return DeviceHandle(address=addr, transport_kind="fake", name=addr)


@pytest.mark.asyncio
async def test_never_exceeds_max_connections() -> None:
    transport = FakeTransport(max_concurrent=2)
    for i in range(5):
        transport.add_peripheral(FakePeripheral(address=f"d{i}"))
    pool = ConnectionPoolManager(transport, max_connections=2)

    async def touch(addr: str) -> None:
        async with pool.acquire(_device(addr)) as conn:
            assert conn.state == ConnState.ACTIVE
            await asyncio.sleep(0.01)

    await asyncio.gather(*(touch(f"d{i}") for i in range(5)))
    assert transport.peak_active_connections <= 2


@pytest.mark.asyncio
async def test_reuses_existing_connection_without_reconnecting() -> None:
    transport = FakeTransport(max_concurrent=2)
    transport.add_peripheral(FakePeripheral(address="d0"))
    pool = ConnectionPoolManager(transport, max_connections=2)

    async with pool.acquire(_device("d0")):
        pass
    assert pool.is_connected("d0")
    async with pool.acquire(_device("d0")) as conn:
        # still the same underlying connection (no new connect() beyond the first)
        assert conn.device.address == "d0"
    assert transport.peak_active_connections == 1


@pytest.mark.asyncio
async def test_evicts_lru_idle_when_full_never_evicts_active() -> None:
    transport = FakeTransport(max_concurrent=1)
    transport.add_peripheral(FakePeripheral(address="d0"))
    transport.add_peripheral(FakePeripheral(address="d1"))
    pool = ConnectionPoolManager(transport, max_connections=1)

    async with pool.acquire(_device("d0")):
        pass
    assert pool.is_connected("d0")

    # d0 is idle; connecting d1 should evict it rather than blocking forever.
    async with pool.acquire(_device("d1")):
        assert pool.is_connected("d1")
        assert not pool.is_connected("d0")


@pytest.mark.asyncio
async def test_waits_when_full_and_all_active() -> None:
    transport = FakeTransport(max_concurrent=1)
    transport.add_peripheral(FakePeripheral(address="d0", op_latency_s=0.05))
    transport.add_peripheral(FakePeripheral(address="d1"))
    pool = ConnectionPoolManager(transport, max_connections=1)

    async def hold_d0() -> None:
        async with pool.acquire(_device("d0")):
            await asyncio.sleep(0.05)

    async def take_d1_after_release() -> float:
        start = asyncio.get_event_loop().time()
        async with pool.acquire(_device("d1")):
            pass
        return asyncio.get_event_loop().time() - start

    t0 = asyncio.create_task(hold_d0())
    await asyncio.sleep(0.005)  # ensure d0 acquires first and is ACTIVE
    waited = await take_d1_after_release()
    await t0
    assert waited >= 0.03  # had to wait for d0's active lease to end


@pytest.mark.asyncio
async def test_pool_status_reports_counts() -> None:
    transport = FakeTransport(max_concurrent=2)
    transport.add_peripheral(FakePeripheral(address="d0"))
    pool = ConnectionPoolManager(transport, max_connections=2)
    async with pool.acquire(_device("d0")):
        status = pool.status()
        assert status.active_count == 1
        assert status.max_connections == 2
    status = pool.status()
    assert status.idle_count == 1
    assert status.active_count == 0
