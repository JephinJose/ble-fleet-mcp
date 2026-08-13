"""Subscription/watch manager backing the `fleet_watch` tool.

A watched device needs a *held* connection (subscriptions are only useful while
connected), which is a different lifecycle from the borrow-and-release pattern
`ConnectionPoolManager.acquire()` uses for read/write jobs — so watching a device
pins one of the pool's connection slots for as long as the watch is active. Watching
more devices than the pool has slots for is refused outright (a specific, reported
error) rather than silently starving fleet_read/fleet_write of connections.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from fleet_mcp.core.pool import ConnectionPoolManager
from fleet_mcp.core.tracing import NullTracer, Tracer
from fleet_mcp.core.types import Connection, DeviceHandle, Reading, Transport


class WatchCapacityExceeded(Exception):
    """Raised when starting a new watch would pin more connections than the pool has."""


@dataclass(slots=True)
class WatchEntry:
    reading: Reading
    at: float = field(default_factory=time.time)


@dataclass(slots=True)
class ResourceWatch:
    address: str
    resource: str
    debounce_s: float
    buffer: deque[WatchEntry] = field(default_factory=lambda: deque(maxlen=500))
    last_buffered_at: float = 0.0
    last_value: Any = None

    def offer(self, reading: Reading) -> None:
        now = time.time()
        if self.last_buffered_at and (now - self.last_buffered_at) < self.debounce_s:
            return
        self.buffer.append(WatchEntry(reading=reading, at=now))
        self.last_buffered_at = now
        self.last_value = reading.value

    def drain(self) -> list[WatchEntry]:
        entries = list(self.buffer)
        self.buffer.clear()
        return entries


class WatchManager:
    def __init__(
        self, pool: ConnectionPoolManager, transport: Transport, tracer: Tracer | None = None
    ) -> None:
        self._pool = pool
        self._transport = transport
        self._tracer = tracer or NullTracer()
        self._watches: dict[tuple[str, str], ResourceWatch] = {}
        self._held_connections: dict[str, Connection] = {}
        self._held_leases: dict[str, ConnectionPoolManager._Lease] = {}
        # Guards the check-then-acquire sequence in subscribe()/unsubscribe(): without
        # it, two concurrent subscribe() calls for the same not-yet-watched device
        # could both see "not held yet" and each open their own connection to it,
        # leaking one. Watch churn is rare, so serializing it costs nothing real.
        self._lock = asyncio.Lock()

    @property
    def held_device_count(self) -> int:
        return len(self._held_connections)

    def is_watching(self, address: str, resource: str) -> bool:
        return (address, resource) in self._watches

    async def subscribe(self, device: DeviceHandle, resource: str, debounce_s: float = 0.5) -> None:
        key = (device.address, resource)
        if key in self._watches:
            return

        async with self._lock:
            if key in self._watches:
                return
            if device.address not in self._held_connections:
                if self.held_device_count >= self._pool.max_connections:
                    raise WatchCapacityExceeded(
                        f"cannot watch {device.address}: {self.held_device_count} device(s) "
                        f"already watched against a pool cap of {self._pool.max_connections}; "
                        "unwatch a device first"
                    )
                lease = self._pool.acquire(device)
                conn = await lease.__aenter__()
                self._held_connections[device.address] = conn
                self._held_leases[device.address] = lease

            conn = self._held_connections[device.address]
            watch = ResourceWatch(address=device.address, resource=resource, debounce_s=debounce_s)
            self._watches[key] = watch

            def _callback(reading: Reading) -> None:
                watch.offer(reading)

            await self._transport.subscribe(conn, resource, _callback)
            self._tracer.emit("watch.subscribe", address=device.address, resource=resource)

    async def unsubscribe(self, address: str, resource: str) -> None:
        async with self._lock:
            key = (address, resource)
            self._watches.pop(key, None)
            self._tracer.emit("watch.unsubscribe", address=address, resource=resource)
            if not any(k[0] == address for k in self._watches):
                lease = self._held_leases.pop(address, None)
                self._held_connections.pop(address, None)
                if lease is not None:
                    await lease.__aexit__(None, None, None)

    def poll(self, address: str, resource: str) -> list[WatchEntry]:
        watch = self._watches.get((address, resource))
        if watch is None:
            return []
        return watch.drain()

    def status(self) -> dict[str, Any]:
        return {
            "watched_devices": self.held_device_count,
            "watches": [
                {
                    "address": addr,
                    "resource": res,
                    "buffered": len(w.buffer),
                    "last_value": w.last_value,
                }
                for (addr, res), w in self._watches.items()
            ],
        }
