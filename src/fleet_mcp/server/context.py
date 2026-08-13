"""Wiring: builds the pool/scheduler/registry/watch stack once at server startup and
hands the tool layer a single shared context."""

from __future__ import annotations

from dataclasses import dataclass

from fleet_mcp.config import Settings
from fleet_mcp.core.circuit_breaker import CircuitBreaker
from fleet_mcp.core.fleet_registry import FleetRegistry
from fleet_mcp.core.pool import ConnectionPoolManager
from fleet_mcp.core.scheduler import Scheduler
from fleet_mcp.core.tracing import Tracer
from fleet_mcp.core.types import Transport
from fleet_mcp.core.watch import WatchManager


@dataclass(slots=True)
class AppContext:
    settings: Settings
    transport: Transport
    transport_kind: str
    pool: ConnectionPoolManager
    scheduler: Scheduler
    registry: FleetRegistry
    watches: WatchManager
    tracer: Tracer


def build_context(
    transport: Transport, transport_kind: str, settings: Settings | None = None
) -> AppContext:
    settings = settings or Settings.from_env()
    tracer = Tracer(path=settings.trace_path, enabled=settings.trace_enabled)
    pool = ConnectionPoolManager(transport, max_connections=settings.max_connections, tracer=tracer)
    circuit_breaker = CircuitBreaker(
        failure_threshold=settings.circuit_breaker_failure_threshold,
        cooldown_s=settings.circuit_breaker_cooldown_s,
    )
    scheduler = Scheduler(
        pool,
        transport,
        circuit_breaker=circuit_breaker,
        device_timeout_s=settings.device_timeout_s,
        backoff_initial_s=settings.backoff_initial_s,
        backoff_max_s=settings.backoff_max_s,
        backoff_multiplier=settings.backoff_multiplier,
        tracer=tracer,
    )
    return AppContext(
        settings=settings,
        transport=transport,
        transport_kind=transport_kind,
        pool=pool,
        scheduler=scheduler,
        registry=FleetRegistry(),
        watches=WatchManager(pool, transport, tracer=tracer),
        tracer=tracer,
    )
