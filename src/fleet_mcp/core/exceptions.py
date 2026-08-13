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
