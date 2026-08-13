from __future__ import annotations

from pathlib import Path

from fleet_mcp.config import Settings


def test_defaults() -> None:
    settings = Settings()
    assert settings.max_connections == 4
    assert settings.allow_writes is False
    assert settings.trace_enabled is True
    assert settings.trace_path == Path(".fleet_mcp/traces/trace.jsonl")
    assert settings.log_level == "INFO"


def test_from_env_reads_overrides(monkeypatch) -> None:
    monkeypatch.setenv("FLEET_MAX_CONNECTIONS", "7")
    monkeypatch.setenv("FLEET_ALLOW_WRITES", "true")
    monkeypatch.setenv("FLEET_DEVICE_TIMEOUT_S", "2.5")
    monkeypatch.setenv("FLEET_TRACE_ENABLED", "0")
    monkeypatch.setenv("FLEET_LOG_LEVEL", "debug")

    settings = Settings.from_env()

    assert settings.max_connections == 7
    assert settings.allow_writes is True
    assert settings.device_timeout_s == 2.5
    assert settings.trace_enabled is False
    assert settings.log_level == "DEBUG"


def test_from_env_defaults_when_unset(monkeypatch) -> None:
    for var in (
        "FLEET_MAX_CONNECTIONS",
        "FLEET_ALLOW_WRITES",
        "FLEET_DEVICE_TIMEOUT_S",
        "FLEET_TRACE_ENABLED",
        "FLEET_LOG_LEVEL",
    ):
        monkeypatch.delenv(var, raising=False)

    settings = Settings.from_env()
    assert settings == Settings()
