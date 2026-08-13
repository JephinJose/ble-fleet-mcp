"""Builds the MCP server: registers the eight fleet_* tools against a shared
AppContext, and provides the `fleet-mcp` console-script entrypoint.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from fleet_mcp.__about__ import __version__
from fleet_mcp.config import Settings
from fleet_mcp.server import tools as t
from fleet_mcp.server.context import AppContext, build_context
from fleet_mcp.transports.ble import BleTransport

INSTRUCTIONS = """\
fleet-mcp manages a fleet of constrained-connection devices (BLE today) behind
fleet-level tools. You never need to reason about the radio's connection limit —
register a fleet once with fleet_register, then call fleet_read / fleet_write
against the fleet name and every per-device connect/read/write/retry/backoff/
eviction decision happens inside the server.

Writes are disabled unless the server was started with FLEET_ALLOW_WRITES=1.
fleet_read and fleet_write return an operation_id immediately; if the whole batch
finishes within a couple of seconds you'll get complete results back directly,
otherwise poll fleet_operation_status with that operation_id.
"""


def create_app(ctx: AppContext) -> MCPServer:
    app: MCPServer[None] = MCPServer(
        "fleet-mcp",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    @app.tool()
    async def fleet_register(
        name: str,
        addresses: list[str] | None = None,
        name_pattern: str | None = None,
        service_uuids: list[str] | None = None,
        risk_tier: str = "read_only",
        scan_timeout_s: float = 10.0,
    ) -> dict[str, Any]:
        """Register a device or group (by explicit address list, or by name pattern /
        service UUIDs discovery filter) into a named fleet. risk_tier is one of
        read_only, low_risk_actuator, or safety_critical; safety_critical devices are
        excluded from fleet_write batches unless explicitly confirmed."""
        return await t.fleet_register(
            ctx,
            name=name,
            addresses=addresses,
            name_pattern=name_pattern,
            service_uuids=service_uuids,
            risk_tier=risk_tier,
            scan_timeout_s=scan_timeout_s,
        )

    @app.tool()
    async def fleet_scan(
        name_pattern: str | None = None,
        service_uuids: list[str] | None = None,
        addresses: list[str] | None = None,
        timeout_s: float = 10.0,
    ) -> dict[str, Any]:
        """Discover devices matching a filter, without connecting to or registering
        them. Use this to see what's out there before fleet_register."""
        return await t.fleet_scan(
            ctx,
            name_pattern=name_pattern,
            service_uuids=service_uuids,
            addresses=addresses,
            timeout_s=timeout_s,
        )

    @app.tool()
    async def fleet_read(
        fleet: str,
        resource: str,
        addresses: list[str] | None = None,
        priority: str = "normal",
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Read one resource across every device in a fleet, or a subset by address.
        Returns per-device results (success / unreachable / timeout / still_queued) as
        an operation snapshot keyed by operation_id."""
        return await t.fleet_read(
            ctx,
            fleet=fleet,
            resource=resource,
            addresses=addresses,
            priority=priority,
            timeout_s=timeout_s,
        )

    @app.tool()
    async def fleet_write(
        fleet: str,
        resource: str,
        value: Any,
        addresses: list[str] | None = None,
        confirm_addresses: list[str] | None = None,
        priority: str = "normal",
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Write one resource across a fleet. Requires the server to have been started
        with FLEET_ALLOW_WRITES=1. Every write is verified with a readback; the result
        reports both `acknowledged` and `converged`. Devices tiered safety_critical are
        skipped (status confirmation_required) unless their address is passed in
        confirm_addresses."""
        return await t.fleet_write(
            ctx,
            fleet=fleet,
            resource=resource,
            value=value,
            addresses=addresses,
            confirm_addresses=confirm_addresses,
            priority=priority,
            timeout_s=timeout_s,
        )

    @app.tool()
    async def fleet_watch(
        fleet: str,
        resource: str,
        action: str = "subscribe",
        addresses: list[str] | None = None,
        debounce_s: float = 0.5,
    ) -> dict[str, Any]:
        """Subscribe to (action="subscribe"), stop watching (action="unsubscribe"), or
        drain buffered notifications for (action="poll") a resource across a fleet.
        Watching a device pins one pool connection slot for as long as it's watched."""
        return await t.fleet_watch(
            ctx,
            fleet=fleet,
            resource=resource,
            action=action,
            addresses=addresses,
            debounce_s=debounce_s,
        )

    @app.tool()
    async def fleet_status(fleet: str) -> dict[str, Any]:
        """Health of every device in a fleet: healthy/unhealthy, consecutive failure
        count, and whether it's currently connected."""
        return await t.fleet_status(ctx, fleet=fleet)

    @app.tool()
    async def fleet_pool_status() -> dict[str, Any]:
        """Connection pool telemetry: active/idle/evicting connection counts, queue
        depth, and per-device wait times. The live debugging surface for the pool."""
        return await t.fleet_pool_status(ctx)

    @app.tool()
    async def fleet_operation_status(operation_id: str) -> dict[str, Any]:
        """Poll a fleet_read/fleet_write batch by operation_id for partial or complete
        results. Never returns a silently-dropped device: every device resolves to
        success, a specific error, or still_queued."""
        return await t.fleet_operation_status(ctx, operation_id=operation_id)

    return app


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=settings.log_level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("fleet_mcp").info(
        "starting fleet-mcp v%s (max_connections=%d, allow_writes=%s)",
        __version__,
        settings.max_connections,
        settings.allow_writes,
    )

    transport = BleTransport(max_connections=settings.max_connections)
    ctx = build_context(transport, transport_kind="ble", settings=settings)
    app = create_app(ctx)
    app.run()


if __name__ == "__main__":
    main()
