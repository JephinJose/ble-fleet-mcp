"""Named groups of devices ("fleets") that MCP tool calls operate on."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from fleet_mcp.core.exceptions import UnknownFleetError
from fleet_mcp.core.types import DeviceHandle


@dataclass(slots=True)
class Fleet:
    name: str
    devices: dict[str, DeviceHandle] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def resolve(self, addresses: list[str] | None) -> list[DeviceHandle]:
        """All devices in the fleet, or just the given subset if `addresses` is given."""
        if addresses is None:
            return list(self.devices.values())
        missing = [a for a in addresses if a not in self.devices]
        if missing:
            raise KeyError(f"address(es) not registered in fleet {self.name!r}: {missing}")
        return [self.devices[a] for a in addresses]


class FleetRegistry:
    def __init__(self) -> None:
        self._fleets: dict[str, Fleet] = {}

    def register(self, name: str, devices: list[DeviceHandle], merge: bool = True) -> Fleet:
        """Create or update a named fleet. By default new devices are merged into any
        existing fleet of the same name (re-running fleet_register widens the fleet
        rather than replacing it); pass merge=False to replace it outright."""
        fleet = self._fleets.get(name)
        if fleet is None or not merge:
            fleet = Fleet(name=name)
            self._fleets[name] = fleet
        for device in devices:
            fleet.devices[device.address] = device
        return fleet

    def get(self, name: str) -> Fleet:
        fleet = self._fleets.get(name)
        if fleet is None:
            raise UnknownFleetError(f"no fleet named {name!r}; call fleet_register first")
        return fleet

    def list(self) -> list[Fleet]:
        return list(self._fleets.values())
