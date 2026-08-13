from __future__ import annotations

import pytest

from fleet_mcp.core.types import DiscoveryFilter
from fleet_mcp.transports.fake import FakePeripheral, FakeTransport


@pytest.mark.asyncio
async def test_discover_filters_by_name_pattern() -> None:
    transport = FakeTransport()
    transport.add_peripheral(FakePeripheral(address="a", name="warehouse-sensor-1"))
    transport.add_peripheral(FakePeripheral(address="b", name="office-light-1"))

    found = await transport.discover(DiscoveryFilter(name_pattern="warehouse"))
    assert [d.address for d in found] == ["a"]


@pytest.mark.asyncio
async def test_discover_filters_by_explicit_addresses() -> None:
    transport = FakeTransport()
    for addr in ("a", "b", "c"):
        transport.add_peripheral(FakePeripheral(address=addr))

    found = await transport.discover(DiscoveryFilter(addresses=("a", "c")))
    assert {d.address for d in found} == {"a", "c"}


@pytest.mark.asyncio
async def test_connect_read_write_disconnect_round_trip() -> None:
    transport = FakeTransport()
    transport.add_peripheral(FakePeripheral(address="a", resources={"level": 5}))
    device = (await transport.discover(DiscoveryFilter(addresses=("a",))))[0]

    conn = await transport.connect(device)
    reading = await transport.read(conn, "level")
    assert reading.value == 5

    result = await transport.write(conn, "level", 9)
    assert result.acknowledged is True
    reading2 = await transport.read(conn, "level")
    assert reading2.value == 9

    await transport.disconnect(conn)


@pytest.mark.asyncio
async def test_subscribe_and_notify_invokes_callback() -> None:
    transport = FakeTransport()
    transport.add_peripheral(FakePeripheral(address="a", resources={"motion": False}))
    device = (await transport.discover(DiscoveryFilter(addresses=("a",))))[0]
    conn = await transport.connect(device)

    received = []
    await transport.subscribe(conn, "motion", received.append)
    await transport.notify("a", "motion", True)

    assert len(received) == 1
    assert received[0].value is True
