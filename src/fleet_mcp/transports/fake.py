"""In-memory fake transport: the simulated-fleet test harness and the fixture used by
all scheduler/pool unit tests. Implements fleet_mcp.core.types.Transport exactly, so
anything that works against FakeTransport works against the real BLE transport too.

Also self-polices the connection cap: connect() raises if the pool manager ever asks
for more simultaneous connections than max_concurrent_connections() allows, which is
what the soak/chaos tests assert against.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fleet_mcp.core.types import (
    Connection,
    DeviceHandle,
    DeviceUnreachable,
    DiscoveryFilter,
    Reading,
    RiskTier,
    Subscription,
    WriteResult,
)

NotifyCallback = Callable[[Reading], Any]


@dataclass(slots=True)
class FakePeripheral:
    """A virtual BLE peripheral. Tune the *_latency_s / fail_* / hang fields to drive
    scheduler and pool behavior in tests without touching real hardware."""

    address: str
    name: str = ""
    resources: dict[str, Any] = field(default_factory=dict)
    risk_tier: RiskTier = RiskTier.READ_ONLY
    connect_latency_s: float = 0.005
    op_latency_s: float = 0.005
    fail_connect: bool = False
    unreachable: bool = False
    reject_writes: bool = False
    hang: bool = False
    """If True, every operation blocks "forever" (until the caller's timeout fires)."""


class ConcurrencyCapViolation(RuntimeError):
    """Raised if the pool manager ever opens more connections than the transport allows."""


class FakeTransport:
    def __init__(self, max_concurrent: int = 4) -> None:
        self._peripherals: dict[str, FakePeripheral] = {}
        self._max_concurrent = max_concurrent
        self._active = 0
        self._peak_active = 0
        self._lock = asyncio.Lock()
        self._subscriptions: dict[str, dict[str, NotifyCallback]] = {}

    def add_peripheral(self, peripheral: FakePeripheral) -> None:
        self._peripherals[peripheral.address] = peripheral

    def peripheral(self, address: str) -> FakePeripheral:
        return self._peripherals[address]

    @property
    def addresses(self) -> list[str]:
        return list(self._peripherals)

    @property
    def peak_active_connections(self) -> int:
        """High-water mark of concurrent open connections; tests assert this never
        exceeds the configured cap."""
        return self._peak_active

    async def discover(self, filter: DiscoveryFilter) -> list[DeviceHandle]:
        await asyncio.sleep(0)
        out = []
        for p in self._peripherals.values():
            if filter.addresses and p.address not in filter.addresses:
                continue
            if filter.name_pattern and filter.name_pattern not in (p.name or ""):
                continue
            out.append(
                DeviceHandle(
                    address=p.address, transport_kind="fake", name=p.name, risk_tier=p.risk_tier
                )
            )
        return out

    async def connect(self, device: DeviceHandle) -> Connection:
        p = self._peripherals[device.address]
        if p.unreachable:
            raise DeviceUnreachable(device.address)
        if p.hang:
            await asyncio.sleep(3600)
        await asyncio.sleep(p.connect_latency_s)
        if p.fail_connect:
            raise DeviceUnreachable(f"{device.address} refused connection")
        async with self._lock:
            self._active += 1
            self._peak_active = max(self._peak_active, self._active)
            if self._active > self._max_concurrent:
                self._active -= 1
                raise ConcurrencyCapViolation(
                    f"pool manager opened {self._active + 1} connections, cap is "
                    f"{self._max_concurrent}"
                )
        return Connection(device=device, handle=p)

    async def disconnect(self, conn: Connection) -> None:
        await asyncio.sleep(0.001)
        async with self._lock:
            self._active = max(0, self._active - 1)

    async def read(self, conn: Connection, resource: str) -> Reading:
        p: FakePeripheral = conn.handle
        if p.hang:
            await asyncio.sleep(3600)
        await asyncio.sleep(p.op_latency_s * (1 + random.random() * 0.2))
        if p.unreachable:
            raise DeviceUnreachable(conn.device.address)
        if resource not in p.resources:
            raise KeyError(f"{conn.device.address} has no resource {resource!r}")
        return Reading(address=conn.device.address, resource=resource, value=p.resources[resource])

    async def write(self, conn: Connection, resource: str, value: Any) -> WriteResult:
        p: FakePeripheral = conn.handle
        if p.hang:
            await asyncio.sleep(3600)
        await asyncio.sleep(p.op_latency_s)
        if p.unreachable:
            raise DeviceUnreachable(conn.device.address)
        if p.reject_writes:
            return WriteResult(
                address=conn.device.address,
                resource=resource,
                requested_value=value,
                acknowledged=False,
            )
        p.resources[resource] = value
        return WriteResult(
            address=conn.device.address,
            resource=resource,
            requested_value=value,
            acknowledged=True,
        )

    async def subscribe(
        self, conn: Connection, resource: str, callback: NotifyCallback
    ) -> Subscription:
        self._subscriptions.setdefault(conn.device.address, {})[resource] = callback
        return Subscription(
            address=conn.device.address,
            resource=resource,
            subscription_id=f"{conn.device.address}:{resource}",
        )

    def max_concurrent_connections(self) -> int:
        return self._max_concurrent

    async def notify(self, address: str, resource: str, value: Any) -> None:
        """Test/demo helper: push a notification to whoever subscribed."""
        cb = self._subscriptions.get(address, {}).get(resource)
        if cb is None:
            return
        result = cb(Reading(address=address, resource=resource, value=value))
        if asyncio.iscoroutine(result):
            await result


def make_simulated_fleet(
    count: int,
    *,
    max_concurrent: int = 4,
    resource: str = "temperature_c",
    value_fn: Callable[[int], Any] | None = None,
    address_prefix: str = "SIM",
) -> FakeTransport:
    """Build a FakeTransport pre-populated with `count` virtual peripherals, each
    exposing a single `resource`. Used by the simulated-fleet soak/chaos tests and
    the examples/simulated_fleet demo."""
    transport = FakeTransport(max_concurrent=max_concurrent)
    value_fn = value_fn or (lambda i: 20.0 + (i % 10))
    for i in range(count):
        addr = f"{address_prefix}:{i:04d}"
        transport.add_peripheral(
            FakePeripheral(
                address=addr,
                name=f"sim-sensor-{i}",
                resources={resource: value_fn(i)},
            )
        )
    return transport
