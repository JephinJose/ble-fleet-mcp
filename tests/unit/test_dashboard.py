from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from fleet_mcp.config import Settings
from fleet_mcp.core.scheduler import JobKind
from fleet_mcp.server import tools as t
from fleet_mcp.server.context import build_context
from fleet_mcp.server.dashboard import build_status, start_dashboard
from fleet_mcp.transports.fake import FakePeripheral, FakeTransport


@pytest.fixture
def ctx():
    transport = FakeTransport(max_concurrent=2)
    transport.add_peripheral(FakePeripheral(address="D0", name="sensor-0", resources={"r": 1}))
    transport.add_peripheral(FakePeripheral(address="D1", name="sensor-1", resources={"r": 2}))
    return build_context(transport, "fake", Settings(max_connections=2, trace_enabled=False))


@pytest.mark.asyncio
async def test_build_status_shape_empty(ctx) -> None:
    status = build_status(ctx)
    assert status["pool"]["max_connections"] == 2
    assert status["fleets"] == []
    assert status["operations"] == []
    assert status["watches"]["watched_devices"] == 0


@pytest.mark.asyncio
async def test_build_status_reflects_registered_fleet_and_operations(ctx) -> None:
    await t.fleet_register(ctx, name="all", addresses=["D0", "D1"])
    op = await ctx.scheduler.submit([ctx.registry.get("all").devices["D0"]], JobKind.READ, "r")
    await op.done_event.wait()

    status = build_status(ctx)
    assert status["fleets"][0]["name"] == "all"
    assert status["fleets"][0]["device_count"] == 2
    addresses = {d["address"] for d in status["fleets"][0]["devices"]}
    assert addresses == {"D0", "D1"}
    assert len(status["operations"]) == 1
    assert status["operations"][0]["status"] == "completed"


def test_dashboard_serves_html_and_json_over_real_http(ctx) -> None:
    server = start_dashboard(ctx, "127.0.0.1", 0)
    try:
        port = server.server_address[1]

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
            assert "fleet-mcp dashboard" in body

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5) as resp:
            assert resp.status == 200
            payload = json.loads(resp.read())
            assert payload["pool"]["max_connections"] == 2

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nonexistent", timeout=5)
        assert exc_info.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
