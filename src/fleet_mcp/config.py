"""Environment-variable configuration. See README.md for the full reference table."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None or not val.strip():
        return default
    return int(val)


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    if val is None or not val.strip():
        return default
    return float(val)


@dataclass(frozen=True, slots=True)
class Settings:
    max_connections: int = 4
    allow_writes: bool = False
    require_confirmation_safety_critical: bool = True

    device_timeout_s: float = 10.0
    fleet_operation_timeout_s: float = 60.0

    backoff_initial_s: float = 0.5
    backoff_max_s: float = 30.0
    backoff_multiplier: float = 2.0

    circuit_breaker_failure_threshold: int = 3
    circuit_breaker_cooldown_s: float = 60.0

    trace_enabled: bool = True
    trace_path: Path = Path(".fleet_mcp/traces/trace.jsonl")

    log_level: str = "INFO"

    dashboard_port: int | None = None
    """Unset by default: the dashboard is opt-in, not an always-open port."""
    dashboard_host: str = "127.0.0.1"

    @classmethod
    def from_env(cls) -> Settings:
        dashboard_port_raw = os.environ.get("FLEET_DASHBOARD_PORT", "").strip()
        return cls(
            max_connections=_env_int("FLEET_MAX_CONNECTIONS", 4),
            allow_writes=_env_bool("FLEET_ALLOW_WRITES", False),
            require_confirmation_safety_critical=_env_bool(
                "FLEET_REQUIRE_CONFIRMATION_SAFETY_CRITICAL", True
            ),
            device_timeout_s=_env_float("FLEET_DEVICE_TIMEOUT_S", 10.0),
            fleet_operation_timeout_s=_env_float("FLEET_OPERATION_TIMEOUT_S", 60.0),
            backoff_initial_s=_env_float("FLEET_BACKOFF_INITIAL_S", 0.5),
            backoff_max_s=_env_float("FLEET_BACKOFF_MAX_S", 30.0),
            backoff_multiplier=_env_float("FLEET_BACKOFF_MULTIPLIER", 2.0),
            circuit_breaker_failure_threshold=_env_int("FLEET_CIRCUIT_BREAKER_THRESHOLD", 3),
            circuit_breaker_cooldown_s=_env_float("FLEET_CIRCUIT_BREAKER_COOLDOWN_S", 60.0),
            trace_enabled=_env_bool("FLEET_TRACE_ENABLED", True),
            trace_path=Path(os.environ.get("FLEET_TRACE_PATH", ".fleet_mcp/traces/trace.jsonl")),
            log_level=os.environ.get("FLEET_LOG_LEVEL", "INFO").upper(),
            dashboard_port=int(dashboard_port_raw) if dashboard_port_raw else None,
            dashboard_host=os.environ.get("FLEET_DASHBOARD_HOST", "127.0.0.1"),
        )
