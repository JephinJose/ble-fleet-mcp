"""Implementations behind the eight fleet_* MCP tools. Kept as plain async functions
over an AppContext (rather than defined inline as `@app.tool()` closures) so they're
directly unit-testable without spinning up an MCP server.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from typing import Any

from fleet_mcp.core.exceptions import UnknownOperationError, WritesDisabledError
from fleet_mcp.core.scheduler import FleetOperation, JobKind, Priority
from fleet_mcp.core.types import DeviceHandle, DiscoveryFilter, RiskTier
from fleet_mcp.server.context import AppContext

DEFAULT_QUICK_WAIT_S = 1.5
"""How long a fleet_read/fleet_write call blocks hoping to return a complete result
before falling back to "poll me via fleet_operation_status" — keeps small fleets
snappy without ever holding the MCP call open for a fleet-wide sweep."""


async def _quick_wait(op: FleetOperation, quick_wait_s: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(op.done_event.wait(), timeout=quick_wait_s)


def _priority_of(name: str) -> Priority:
    try:
        return Priority[name.upper()]
    except KeyError as exc:
        raise ValueError(f"unknown priority {name!r}; use high, normal, or low") from exc


def _risk_tier_of(name: str) -> RiskTier:
    try:
        return RiskTier(name)
    except ValueError as exc:
        raise ValueError(
            f"unknown risk_tier {name!r}; use read_only, low_risk_actuator, or safety_critical"
        ) from exc


async def fleet_register(
    ctx: AppContext,
    name: str,
    addresses: list[str] | None = None,
    name_pattern: str | None = None,
    service_uuids: list[str] | None = None,
    risk_tier: str = "read_only",
    scan_timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Register a device or group into a named fleet, by explicit address list or by
    discovery filter (name pattern / service UUIDs)."""
    tier = _risk_tier_of(risk_tier)
    if addresses:
        handles = [
            DeviceHandle(address=addr, transport_kind=ctx.transport_kind, risk_tier=tier)
            for addr in addresses
        ]
    else:
        found = await ctx.transport.discover(
            DiscoveryFilter(
                name_pattern=name_pattern,
                service_uuids=tuple(service_uuids or ()),
                timeout_s=scan_timeout_s,
            )
        )
        handles = [dataclasses.replace(h, risk_tier=tier) for h in found]

    fleet = ctx.registry.register(name, handles)
    return {
        "fleet": name,
        "device_count": len(fleet.devices),
        "addresses": sorted(fleet.devices),
    }


async def fleet_scan(
    ctx: AppContext,
    name_pattern: str | None = None,
    service_uuids: list[str] | None = None,
    addresses: list[str] | None = None,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Discover devices matching a filter, without connecting or registering them."""
    found = await ctx.transport.discover(
        DiscoveryFilter(
            name_pattern=name_pattern,
            service_uuids=tuple(service_uuids or ()),
            addresses=tuple(addresses or ()),
            timeout_s=timeout_s,
        )
    )
    return {
        "devices": [
            {"address": h.address, "name": h.name, "rssi": h.rssi, "transport": h.transport_kind}
            for h in found
        ]
    }


async def fleet_read(
    ctx: AppContext,
    fleet: str,
    resource: str,
    addresses: list[str] | None = None,
    priority: str = "normal",
    timeout_s: float | None = None,
    quick_wait_s: float = DEFAULT_QUICK_WAIT_S,
) -> dict[str, Any]:
    """Read one resource across every device in a fleet (or a named subset). Returns
    immediately with an operation_id; if all devices answer within quick_wait_s the
    results come back complete, otherwise poll fleet_operation_status."""
    devices = ctx.registry.get(fleet).resolve(addresses)
    op = await ctx.scheduler.submit(
        devices,
        JobKind.READ,
        resource,
        priority=_priority_of(priority),
        timeout_s=timeout_s or ctx.settings.fleet_operation_timeout_s,
    )
    await _quick_wait(op, quick_wait_s)
    return op.snapshot()


async def fleet_write(
    ctx: AppContext,
    fleet: str,
    resource: str,
    value: Any,
    addresses: list[str] | None = None,
    confirm_addresses: list[str] | None = None,
    priority: str = "normal",
    timeout_s: float | None = None,
    quick_wait_s: float = DEFAULT_QUICK_WAIT_S,
) -> dict[str, Any]:
    """Write one resource across a fleet. Requires FLEET_ALLOW_WRITES=1. Every write is
    followed by a readback to report convergence, not just acknowledgement.
    Safety-critical devices are excluded from the batch (reported as
    confirmation_required) unless their address is listed in confirm_addresses."""
    if not ctx.settings.allow_writes:
        raise WritesDisabledError(
            "writes are disabled; set FLEET_ALLOW_WRITES=1 to enable fleet_write"
        )

    devices = ctx.registry.get(fleet).resolve(addresses)
    confirmed = set(confirm_addresses or [])
    blocked = [
        d for d in devices if d.risk_tier == RiskTier.SAFETY_CRITICAL and d.address not in confirmed
    ]
    eligible = [d for d in devices if d not in blocked]

    if eligible:
        op = await ctx.scheduler.submit(
            eligible,
            JobKind.WRITE,
            resource,
            value=value,
            priority=_priority_of(priority),
            timeout_s=timeout_s or ctx.settings.fleet_operation_timeout_s,
        )
        await _quick_wait(op, quick_wait_s)
        snapshot = op.snapshot()
    else:
        snapshot = {
            "operation_id": None,
            "status": "completed",
            "total": 0,
            "completed": 0,
            "created_at": None,
            "finished_at": None,
            "results": {},
        }

    for device in blocked:
        snapshot["results"][device.address] = {
            "resource": resource,
            "kind": "write",
            "status": "confirmation_required",
            "attempts": 0,
            "error": ("safety-critical device; add its address to confirm_addresses to write it"),
            "value": None,
            "write": None,
        }
    snapshot["total"] += len(blocked)
    return snapshot


async def fleet_watch(
    ctx: AppContext,
    fleet: str,
    resource: str,
    action: str = "subscribe",
    addresses: list[str] | None = None,
    debounce_s: float = 0.5,
) -> dict[str, Any]:
    """Subscribe to / unsubscribe from / poll a resource across a fleet. `action` is
    one of "subscribe", "unsubscribe", or "poll" — notifications are buffered and
    debounced server-side; call again with action="poll" to drain them."""
    devices = ctx.registry.get(fleet).resolve(addresses)

    if action == "subscribe":
        errors: dict[str, str] = {}
        subscribed: list[str] = []
        for device in devices:
            try:
                await ctx.watches.subscribe(device, resource, debounce_s=debounce_s)
                subscribed.append(device.address)
            except Exception as exc:
                errors[device.address] = str(exc)
        return {"action": "subscribe", "subscribed": subscribed, "errors": errors}

    if action == "unsubscribe":
        for device in devices:
            await ctx.watches.unsubscribe(device.address, resource)
        return {"action": "unsubscribe", "addresses": [d.address for d in devices]}

    if action == "poll":
        results = {}
        for device in devices:
            entries = ctx.watches.poll(device.address, resource)
            results[device.address] = [{"value": e.reading.value, "at": e.at} for e in entries]
        return {"action": "poll", "results": results}

    raise ValueError(f"unknown action {action!r}; use subscribe, unsubscribe, or poll")


async def fleet_status(ctx: AppContext, fleet: str) -> dict[str, Any]:
    """Health of every device in a fleet: healthy/unhealthy, consecutive
    failures, and whether it's currently connected."""
    f = ctx.registry.get(fleet)
    cb = ctx.scheduler.circuit_breaker
    devices_status = {}
    for address in f.devices:
        health = cb.health_of(address)
        devices_status[address] = {
            "health": health.state.value,
            "consecutive_failures": health.consecutive_failures,
            "consecutive_successes": health.consecutive_successes,
            "last_failure_reason": health.last_failure_reason,
            "connected": ctx.pool.is_connected(address),
        }
    return {"fleet": fleet, "device_count": len(f.devices), "devices": devices_status}


async def fleet_pool_status(ctx: AppContext) -> dict[str, Any]:
    """Connection pool telemetry: active/idle/evicting counts, queue depth, per-device
    wait times, and how many slots are pinned by active fleet_watch subscriptions."""
    status = ctx.pool.status()
    return {
        "max_connections": status.max_connections,
        "active_connections": status.active_count,
        "idle_connections": status.idle_count,
        "evicting_connections": status.evicting_count,
        "queue_depth": status.queue_depth,
        "per_device_wait_s": status.per_device_wait_s,
        "watched_devices": ctx.watches.held_device_count,
    }


async def fleet_operation_status(ctx: AppContext, operation_id: str) -> dict[str, Any]:
    """Poll a long-running fleet_read/fleet_write batch by operation_id."""
    op = ctx.scheduler.get_operation(operation_id)
    if op is None:
        raise UnknownOperationError(f"no operation with id {operation_id!r}")
    return op.snapshot()
