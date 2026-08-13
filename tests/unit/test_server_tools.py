from __future__ import annotations

import pytest

from fleet_mcp.config import Settings
from fleet_mcp.core.exceptions import UnknownFleetError, UnknownOperationError, WritesDisabledError
from fleet_mcp.server import tools as t
from fleet_mcp.server.context import build_context
from fleet_mcp.transports.fake import FakePeripheral, FakeTransport


def _settings(**overrides: object) -> Settings:
    return Settings(max_connections=2, **overrides)  # type: ignore[arg-type]


@pytest.fixture
def transport() -> FakeTransport:
    tr = FakeTransport(max_concurrent=2)
    for i in range(4):
        tr.add_peripheral(FakePeripheral(address=f"D{i}", name=f"sensor-{i}", resources={"r": i}))
    return tr


@pytest.mark.asyncio
async def test_register_by_explicit_addresses(transport: FakeTransport) -> None:
    ctx = build_context(transport, "fake", _settings())
    result = await t.fleet_register(ctx, name="warehouse", addresses=["D0", "D1"])
    assert result["device_count"] == 2
    assert result["addresses"] == ["D0", "D1"]


@pytest.mark.asyncio
async def test_register_by_discovery_filter(transport: FakeTransport) -> None:
    ctx = build_context(transport, "fake", _settings())
    result = await t.fleet_register(ctx, name="all", name_pattern="sensor")
    assert result["device_count"] == 4


@pytest.mark.asyncio
async def test_scan_does_not_register(transport: FakeTransport) -> None:
    ctx = build_context(transport, "fake", _settings())
    result = await t.fleet_scan(ctx, name_pattern="sensor")
    assert len(result["devices"]) == 4
    with pytest.raises(UnknownFleetError):
        ctx.registry.get("anything")


@pytest.mark.asyncio
async def test_read_returns_complete_results_for_small_fleet(transport: FakeTransport) -> None:
    ctx = build_context(transport, "fake", _settings())
    await t.fleet_register(ctx, name="all", addresses=["D0", "D1", "D2", "D3"])
    result = await t.fleet_read(ctx, fleet="all", resource="r", quick_wait_s=2.0)
    assert result["status"] == "completed"
    assert result["completed"] == 4
    for r in result["results"].values():
        assert r["status"] == "success"


@pytest.mark.asyncio
async def test_write_disabled_by_default(transport: FakeTransport) -> None:
    ctx = build_context(transport, "fake", _settings(allow_writes=False))
    await t.fleet_register(ctx, name="all", addresses=["D0"])
    with pytest.raises(WritesDisabledError):
        await t.fleet_write(ctx, fleet="all", resource="r", value=1)


@pytest.mark.asyncio
async def test_write_verify_after_write(transport: FakeTransport) -> None:
    ctx = build_context(transport, "fake", _settings(allow_writes=True))
    await t.fleet_register(ctx, name="all", addresses=["D0"])
    result = await t.fleet_write(ctx, fleet="all", resource="r", value=42, quick_wait_s=2.0)
    write = result["results"]["D0"]["write"]
    assert write["acknowledged"] is True
    assert write["converged"] is True
    assert write["readback_value"] == 42


@pytest.mark.asyncio
async def test_write_blocks_safety_critical_without_confirmation(
    transport: FakeTransport,
) -> None:
    ctx = build_context(transport, "fake", _settings(allow_writes=True))
    await t.fleet_register(ctx, name="mixed", addresses=["D0"], risk_tier="read_only")
    await t.fleet_register(ctx, name="mixed", addresses=["D1"], risk_tier="safety_critical")

    result = await t.fleet_write(ctx, fleet="mixed", resource="r", value=1, quick_wait_s=2.0)
    assert result["results"]["D1"]["status"] == "confirmation_required"
    assert result["results"]["D0"]["status"] == "success"


@pytest.mark.asyncio
async def test_write_allows_safety_critical_with_confirmation(transport: FakeTransport) -> None:
    ctx = build_context(transport, "fake", _settings(allow_writes=True))
    await t.fleet_register(ctx, name="locks", addresses=["D1"], risk_tier="safety_critical")

    result = await t.fleet_write(
        ctx, fleet="locks", resource="r", value=1, confirm_addresses=["D1"], quick_wait_s=2.0
    )
    assert result["results"]["D1"]["status"] == "success"


@pytest.mark.asyncio
async def test_operation_status_unknown_id_raises(transport: FakeTransport) -> None:
    ctx = build_context(transport, "fake", _settings())
    with pytest.raises(UnknownOperationError):
        await t.fleet_operation_status(ctx, operation_id="nonexistent")


@pytest.mark.asyncio
async def test_pool_status_reports_max_connections(transport: FakeTransport) -> None:
    ctx = build_context(transport, "fake", _settings())
    status = await t.fleet_pool_status(ctx)
    assert status["max_connections"] == 2


@pytest.mark.asyncio
async def test_fleet_status_reports_health(transport: FakeTransport) -> None:
    ctx = build_context(transport, "fake", _settings())
    await t.fleet_register(ctx, name="all", addresses=["D0"])
    await t.fleet_read(ctx, fleet="all", resource="r", quick_wait_s=2.0)

    status = await t.fleet_status(ctx, fleet="all")
    assert status["devices"]["D0"]["health"] == "healthy"


@pytest.mark.asyncio
async def test_watch_subscribe_poll_unsubscribe(transport: FakeTransport) -> None:
    ctx = build_context(transport, "fake", _settings())
    await t.fleet_register(ctx, name="all", addresses=["D0"])

    sub_result = await t.fleet_watch(ctx, fleet="all", resource="r", action="subscribe")
    assert sub_result["subscribed"] == ["D0"]

    await transport.notify("D0", "r", 99)
    poll_result = await t.fleet_watch(ctx, fleet="all", resource="r", action="poll")
    assert poll_result["results"]["D0"][0]["value"] == 99

    unsub_result = await t.fleet_watch(ctx, fleet="all", resource="r", action="unsubscribe")
    assert unsub_result["addresses"] == ["D0"]
    assert ctx.watches.held_device_count == 0
