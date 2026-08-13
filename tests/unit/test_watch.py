from __future__ import annotations

import asyncio

import pytest

from fleet_mcp.core.pool import ConnectionPoolManager
from fleet_mcp.core.types import DeviceHandle
from fleet_mcp.core.watch import WatchCapacityExceeded, WatchManager
from fleet_mcp.transports.fake import FakePeripheral, FakeTransport


def _device(addr: str) -> DeviceHandle:
    return DeviceHandle(address=addr, transport_kind="fake")


@pytest.mark.asyncio
async def test_subscribe_poll_drains_buffer() -> None:
    transport = FakeTransport(max_concurrent=2)
    transport.add_peripheral(FakePeripheral(address="d0", resources={"r": 1}))
    pool = ConnectionPoolManager(transport, max_connections=2)
    watches = WatchManager(pool, transport)

    await watches.subscribe(_device("d0"), "r")
    await transport.notify("d0", "r", 42)

    entries = watches.poll("d0", "r")
    assert [e.reading.value for e in entries] == [42]
    # draining clears the buffer
    assert watches.poll("d0", "r") == []


@pytest.mark.asyncio
async def test_debounce_suppresses_rapid_duplicate_notifications() -> None:
    transport = FakeTransport(max_concurrent=2)
    transport.add_peripheral(FakePeripheral(address="d0", resources={"r": 1}))
    pool = ConnectionPoolManager(transport, max_connections=2)
    watches = WatchManager(pool, transport)

    await watches.subscribe(_device("d0"), "r", debounce_s=1.0)
    await transport.notify("d0", "r", 1)
    await transport.notify("d0", "r", 2)  # within the debounce window — suppressed
    await transport.notify("d0", "r", 3)  # also suppressed

    entries = watches.poll("d0", "r")
    assert [e.reading.value for e in entries] == [1]


@pytest.mark.asyncio
async def test_debounce_allows_next_notification_after_window_elapses() -> None:
    transport = FakeTransport(max_concurrent=2)
    transport.add_peripheral(FakePeripheral(address="d0", resources={"r": 1}))
    pool = ConnectionPoolManager(transport, max_connections=2)
    watches = WatchManager(pool, transport)

    await watches.subscribe(_device("d0"), "r", debounce_s=0.05)
    await transport.notify("d0", "r", 1)
    await asyncio.sleep(0.1)
    await transport.notify("d0", "r", 2)

    entries = watches.poll("d0", "r")
    assert [e.reading.value for e in entries] == [1, 2]


@pytest.mark.asyncio
async def test_subscribe_holds_connection_open() -> None:
    transport = FakeTransport(max_concurrent=2)
    transport.add_peripheral(FakePeripheral(address="d0", resources={"r": 1}))
    pool = ConnectionPoolManager(transport, max_connections=2)
    watches = WatchManager(pool, transport)

    await watches.subscribe(_device("d0"), "r")
    assert pool.is_connected("d0")
    assert watches.held_device_count == 1

    await watches.unsubscribe("d0", "r")
    assert watches.held_device_count == 0


@pytest.mark.asyncio
async def test_watch_capacity_exceeded_is_reported_not_raised_as_pool_starvation() -> None:
    transport = FakeTransport(max_concurrent=1)
    transport.add_peripheral(FakePeripheral(address="d0", resources={"r": 1}))
    transport.add_peripheral(FakePeripheral(address="d1", resources={"r": 1}))
    pool = ConnectionPoolManager(transport, max_connections=1)
    watches = WatchManager(pool, transport)

    await watches.subscribe(_device("d0"), "r")
    with pytest.raises(WatchCapacityExceeded):
        await watches.subscribe(_device("d1"), "r")

    # d0's watch must still be intact — a failed second watch doesn't disturb the first
    assert watches.held_device_count == 1
    assert watches.is_watching("d0", "r")


@pytest.mark.asyncio
async def test_concurrent_subscribes_to_same_device_open_exactly_one_connection() -> None:
    """Regression test: subscribe() used to check-then-acquire without a lock, so two
    concurrent calls for different resources on the same not-yet-watched device could
    each see "not held yet" and both open their own connection, leaking one."""
    transport = FakeTransport(max_concurrent=4)
    transport.add_peripheral(FakePeripheral(address="d0", resources={"a": 1, "b": 2}))
    pool = ConnectionPoolManager(transport, max_connections=4)
    watches = WatchManager(pool, transport)

    await asyncio.gather(
        watches.subscribe(_device("d0"), "a"),
        watches.subscribe(_device("d0"), "b"),
    )

    assert watches.held_device_count == 1
    assert transport.peak_active_connections == 1
    assert watches.is_watching("d0", "a")
    assert watches.is_watching("d0", "b")
