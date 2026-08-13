"""BLE transport plugin implementing fleet_mcp.core.types.Transport via bleak.

`resource` strings are GATT characteristic UUIDs (or bleak's other accepted
`char_specifier` forms — a handle int-as-string, or a BleakGATTCharacteristic).

Values on read/subscribe are the raw `bytearray` bleak returns; write() accepts
`bytes`/`bytearray` as-is, an `int` (packed as a single little-endian byte, or more if
the value doesn't fit in one byte), or a `str` (UTF-8 encoded) — pick whatever matches
your device's characteristic encoding before calling fleet_write.

There is no single true answer for "how many BLE connections can this radio hold
open" — it depends on OS, adapter, and driver. bleak does not expose it, so this
transport defaults to a conservative 4 and expects the deployment to override it via
`FLEET_MAX_CONNECTIONS` (see docs/hardware-validation.md for how to measure yours).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakDeviceNotFoundError, BleakError

from fleet_mcp.core.types import (
    Connection,
    DeviceHandle,
    DeviceUnreachable,
    DiscoveryFilter,
    OperationTimeout,
    Reading,
    Subscription,
    WriteRejected,
    WriteResult,
)

logger = logging.getLogger("fleet_mcp.transports.ble")

DEFAULT_MAX_CONNECTIONS = 4


def _encode(value: Any) -> bytes:
    if isinstance(value, bytes | bytearray):
        return bytes(value)
    if isinstance(value, bool):
        return bytes([1 if value else 0])
    if isinstance(value, int):
        if value < 0:
            raise TypeError("negative ints are not supported; encode bytes explicitly")
        length = max(1, (value.bit_length() + 7) // 8)
        return value.to_bytes(length, byteorder="little")
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError(
        f"cannot encode {type(value).__name__} for a BLE write; pass bytes, int, or str"
    )


class BleTransport:
    def __init__(self, max_connections: int = DEFAULT_MAX_CONNECTIONS) -> None:
        self._max_connections = max_connections
        self._notify_tasks: set[asyncio.Task[None]] = set()

    def max_concurrent_connections(self) -> int:
        return self._max_connections

    async def discover(self, filter: DiscoveryFilter) -> list[DeviceHandle]:
        try:
            found = await BleakScanner.discover(timeout=filter.timeout_s, return_adv=True)
        except BleakError as exc:
            raise DeviceUnreachable(f"BLE scan failed: {exc}") from exc

        wanted_services = {u.lower() for u in filter.service_uuids}
        wanted_addresses = set(filter.addresses)

        handles: list[DeviceHandle] = []
        for address, (device, adv) in found.items():
            if wanted_addresses and address not in wanted_addresses:
                continue
            name = device.name or adv.local_name
            if filter.name_pattern and (not name or filter.name_pattern not in name):
                continue
            if wanted_services:
                adv_services = {u.lower() for u in (adv.service_uuids or [])}
                if not (wanted_services & adv_services):
                    continue
            handles.append(
                DeviceHandle(
                    address=address,
                    transport_kind="ble",
                    name=name,
                    rssi=adv.rssi,
                    metadata={"service_uuids": list(adv.service_uuids or [])},
                )
            )
        return handles

    async def connect(self, device: DeviceHandle) -> Connection:
        client = BleakClient(device.address)
        try:
            await client.connect()
        except BleakDeviceNotFoundError as exc:
            raise DeviceUnreachable(f"{device.address}: not found") from exc
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise OperationTimeout(f"{device.address}: connect timed out") from exc
        except BleakError as exc:
            raise DeviceUnreachable(f"{device.address}: {exc}") from exc
        return Connection(device=device, handle=client)

    async def disconnect(self, conn: Connection) -> None:
        client: BleakClient = conn.handle
        with contextlib.suppress(BleakError):
            await client.disconnect()

    async def read(self, conn: Connection, resource: str) -> Reading:
        client: BleakClient = conn.handle
        try:
            data = await client.read_gatt_char(resource)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise OperationTimeout(f"{conn.device.address}: read {resource} timed out") from exc
        except BleakError as exc:
            raise DeviceUnreachable(
                f"{conn.device.address}: read {resource} failed: {exc}"
            ) from exc
        return Reading(address=conn.device.address, resource=resource, value=bytes(data))

    async def write(self, conn: Connection, resource: str, value: Any) -> WriteResult:
        client: BleakClient = conn.handle
        payload = _encode(value)
        try:
            await client.write_gatt_char(resource, payload, response=True)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise OperationTimeout(f"{conn.device.address}: write {resource} timed out") from exc
        except BleakDeviceNotFoundError as exc:
            raise DeviceUnreachable(f"{conn.device.address}: not found") from exc
        except BleakError as exc:
            raise WriteRejected(f"{conn.device.address}: write {resource} rejected: {exc}") from exc
        return WriteResult(
            address=conn.device.address,
            resource=resource,
            requested_value=value,
            acknowledged=True,
        )

    async def subscribe(self, conn: Connection, resource: str, callback: Any) -> Subscription:
        client: BleakClient = conn.handle

        def _on_notify(_sender: Any, data: bytearray) -> None:
            reading = Reading(address=conn.device.address, resource=resource, value=bytes(data))
            result = callback(reading)
            if asyncio.iscoroutine(result):
                task = asyncio.ensure_future(result)
                self._notify_tasks.add(task)
                task.add_done_callback(self._notify_tasks.discard)

        try:
            await client.start_notify(resource, _on_notify)
        except BleakError as exc:
            raise DeviceUnreachable(
                f"{conn.device.address}: subscribe {resource} failed: {exc}"
            ) from exc
        return Subscription(
            address=conn.device.address,
            resource=resource,
            subscription_id=f"{conn.device.address}:{resource}",
        )
