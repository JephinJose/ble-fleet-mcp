"""Protocol-agnostic data types shared by the pool manager, scheduler, and transports.

Nothing in this module may import a transport-specific package (bleak, zigpy, ...).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class RiskTier(str, Enum):
    """Risk classification for a device, used to gate bulk autonomous operations."""

    READ_ONLY = "read_only"
    LOW_RISK_ACTUATOR = "low_risk_actuator"
    SAFETY_CRITICAL = "safety_critical"


@dataclass(frozen=True, slots=True)
class DiscoveryFilter:
    """Criteria used to narrow a transport's discover() call."""

    name_pattern: str | None = None
    service_uuids: tuple[str, ...] = ()
    addresses: tuple[str, ...] = ()
    timeout_s: float = 10.0


@dataclass(frozen=True, slots=True)
class DeviceHandle:
    """A transport-agnostic reference to a device, before any connection exists."""

    address: str
    transport_kind: str
    name: str | None = None
    rssi: int | None = None
    risk_tier: RiskTier = RiskTier.READ_ONLY
    metadata: dict[str, Any] = field(default_factory=dict)


class ConnState(str, Enum):
    IDLE = "idle"
    ACTIVE = "active"
    DEGRADED = "degraded"
    EVICTING = "evicting"


@dataclass(slots=True)
class Connection:
    """A live (or recently live) connection to a device, owned by the pool manager."""

    device: DeviceHandle
    handle: Any
    state: ConnState = ConnState.IDLE
    connected_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_used_at = time.monotonic()


@dataclass(frozen=True, slots=True)
class Reading:
    address: str
    resource: str
    value: Any
    read_at: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class WriteResult:
    address: str
    resource: str
    requested_value: Any
    acknowledged: bool
    converged: bool | None = None
    """None until a verify-after-write readback has been attempted."""
    readback_value: Any = None


@dataclass(frozen=True, slots=True)
class Subscription:
    address: str
    resource: str
    subscription_id: str


class TransportError(Exception):
    """Base class for transport-level failures. Subclass per failure mode."""


class DeviceUnreachable(TransportError):
    pass


class OperationTimeout(TransportError):
    pass


class WriteRejected(TransportError):
    pass


class Transport(Protocol):
    """Minimal interface every transport plugin (BLE, Zigbee, Thread, ...) must implement.

    Implementations must be safe to call concurrently up to max_concurrent_connections()
    connections; the pool manager is responsible for never exceeding that cap.
    """

    async def discover(self, filter: DiscoveryFilter) -> list[DeviceHandle]: ...

    async def connect(self, device: DeviceHandle) -> Connection: ...

    async def disconnect(self, conn: Connection) -> None: ...

    async def read(self, conn: Connection, resource: str) -> Reading: ...

    async def write(self, conn: Connection, resource: str, value: Any) -> WriteResult: ...

    async def subscribe(self, conn: Connection, resource: str, callback: Any) -> Subscription: ...

    def max_concurrent_connections(self) -> int: ...
