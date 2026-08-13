"""Connection pool manager: enforces the hard concurrency cap and evicts LRU-idle
connections to make room for new ones. Never interrupts an in-flight operation.

Protocol-agnostic: depends only on fleet_mcp.core.types.Transport / DeviceHandle / Connection.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass

from fleet_mcp.core.tracing import Tracer
from fleet_mcp.core.types import Connection, ConnState, DeviceHandle, Transport


class PoolExhaustedError(Exception):
    """Raised when no idle connection can be evicted to satisfy a new request."""


@dataclass(frozen=True, slots=True)
class PoolStatus:
    max_connections: int
    active_count: int
    idle_count: int
    evicting_count: int
    queue_depth: int
    per_device_wait_s: dict[str, float]


class ConnectionPoolManager:
    """Owns at most `max_connections` live transport connections at any time.

    Callers acquire a connection via `acquire(device)` (an async context manager) which:
      1. Returns an existing connection if the device is already connected.
      2. Otherwise connects, evicting the least-recently-used *idle* connection first
         if the pool is at capacity.
      3. Marks the connection ACTIVE for the duration of the `with` block and IDLE after.

    An in-flight (ACTIVE) connection is never evicted — eviction only considers IDLE
    connections, and if none exist the caller waits (see `_capacity_available`).
    """

    def __init__(
        self,
        transport: Transport,
        max_connections: int | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        cap = max_connections or transport.max_concurrent_connections()
        if cap < 1:
            raise ValueError("max_connections must be >= 1")
        self._transport = transport
        self._max_connections = cap
        self._tracer = tracer or Tracer(path=None, enabled=False)

        # address -> Connection, ordered by last_used_at ascending (LRU at front)
        self._connections: OrderedDict[str, Connection] = OrderedDict()
        self._reserved = 0
        """Slots claimed for an in-flight connect() that hasn't landed in _connections yet."""
        self._lock = asyncio.Lock()
        self._capacity_available = asyncio.Condition(self._lock)
        self._wait_started_at: dict[str, float] = {}

    @property
    def max_connections(self) -> int:
        return self._max_connections

    def status(self) -> PoolStatus:
        active = sum(1 for c in self._connections.values() if c.state == ConnState.ACTIVE)
        idle = sum(1 for c in self._connections.values() if c.state == ConnState.IDLE)
        evicting = sum(1 for c in self._connections.values() if c.state == ConnState.EVICTING)
        now = time.monotonic()
        waits = {addr: now - start for addr, start in self._wait_started_at.items()}
        return PoolStatus(
            max_connections=self._max_connections,
            active_count=active,
            idle_count=idle,
            evicting_count=evicting,
            queue_depth=len(self._wait_started_at),
            per_device_wait_s=waits,
        )

    def _find_lru_idle_locked(self) -> Connection | None:
        for conn in self._connections.values():
            if conn.state == ConnState.IDLE:
                return conn
        return None

    async def _acquire_connection(self, device: DeviceHandle) -> Connection:
        """Reserve-then-connect: bookkeeping (dict + reserved count) is only ever
        mutated while holding `_lock`, but the actual transport I/O (connect/disconnect,
        which can be slow, or in the worst case hang) always runs with the lock released
        so one device's slow connect can't block acquisition of a *different* device.
        """
        self._wait_started_at.setdefault(device.address, time.monotonic())
        try:
            while True:
                to_evict: Connection | None = None
                async with self._lock:
                    existing = self._connections.get(device.address)
                    if existing is not None:
                        existing.state = ConnState.ACTIVE
                        existing.touch()
                        self._connections.move_to_end(device.address)
                        return existing

                    in_use = len(self._connections) + self._reserved
                    if in_use < self._max_connections:
                        self._reserved += 1
                        break  # fall through to connect, below, outside the lock

                    to_evict = self._find_lru_idle_locked()
                    if to_evict is not None:
                        to_evict.state = ConnState.EVICTING
                        del self._connections[to_evict.device.address]
                    else:
                        # Pool is full and every connection is in-flight; wait for a release.
                        self._tracer.emit("pool.wait", address=device.address)
                        await self._capacity_available.wait()
                        continue

                # Reached only via the `to_evict is not None` branch above.
                self._tracer.emit("pool.evict", address=to_evict.device.address)
                await self._transport.disconnect(to_evict)
                async with self._lock:
                    self._capacity_available.notify_all()
                continue

            # Reserved a slot; connect outside the lock.
            try:
                conn = await self._transport.connect(device)
            except Exception:
                async with self._lock:
                    self._reserved -= 1
                    self._capacity_available.notify_all()
                raise
            async with self._lock:
                self._reserved -= 1
                conn.state = ConnState.ACTIVE
                self._connections[device.address] = conn
                self._tracer.emit("pool.connect", address=device.address)
            return conn
        finally:
            self._wait_started_at.pop(device.address, None)

    async def _release_connection(self, device: DeviceHandle) -> None:
        async with self._lock:
            conn = self._connections.get(device.address)
            if conn is not None:
                conn.state = ConnState.IDLE
                conn.touch()
            self._capacity_available.notify_all()

    class _Lease:
        def __init__(self, pool: ConnectionPoolManager, device: DeviceHandle) -> None:
            self._pool = pool
            self._device = device
            self._conn: Connection | None = None

        async def __aenter__(self) -> Connection:
            self._conn = await self._pool._acquire_connection(self._device)
            return self._conn

        async def __aexit__(self, *exc_info: object) -> None:
            await self._pool._release_connection(self._device)

    def acquire(self, device: DeviceHandle) -> ConnectionPoolManager._Lease:
        """Async context manager yielding a live Connection for `device`."""
        return ConnectionPoolManager._Lease(self, device)

    def is_connected(self, address: str) -> bool:
        return address in self._connections

    async def close_all(self) -> None:
        async with self._lock:
            for conn in list(self._connections.values()):
                await self._transport.disconnect(conn)
            self._connections.clear()
