"""Exceptions shared across the scheduler and MCP tool layer."""

from __future__ import annotations


class FleetMCPError(Exception):
    """Base class for all fleet-mcp errors."""


class WritesDisabledError(FleetMCPError):
    """Raised when a write is attempted but FLEET_ALLOW_WRITES is not set."""


class UnknownFleetError(FleetMCPError):
    pass


class UnknownOperationError(FleetMCPError):
    pass


class SafetyCriticalConfirmationRequired(FleetMCPError):
    """Raised when a write batch includes a safety-critical device without explicit
    per-device confirmation."""

    def __init__(self, addresses: list[str]) -> None:
        self.addresses = addresses
        super().__init__(
            f"Safety-critical device(s) require explicit confirmation before write: {addresses}"
        )
