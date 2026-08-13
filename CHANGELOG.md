# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-08-13

### Added

- Protocol-agnostic connection pool manager (`fleet_mcp.core.pool`) enforcing a hard
  concurrency cap with LRU-idle eviction; an in-flight connection is never interrupted.
- Priority scheduler (`fleet_mcp.core.scheduler`) that decomposes fleet operations into
  per-device jobs, dispatches them against the pool, retries failures with per-device
  exponential backoff, and batches already-connected devices ahead of ones that would
  require a new connection.
- Per-device circuit breaker / health tracking (`fleet_mcp.core.circuit_breaker`) that
  opens after N consecutive failures and cools down before re-probing.
- `Transport` protocol (`fleet_mcp.core.types`) as the plugin interface for any
  constrained-connection transport.
- `FakeTransport` in-memory transport (`fleet_mcp.transports.fake`) used by the unit
  test suite and the simulated-fleet harness; self-polices the connection cap.
- BLE transport plugin (`fleet_mcp.transports.ble`) built on `bleak`.
- MCP tool surface: `fleet_register`, `fleet_scan`, `fleet_read`, `fleet_write`,
  `fleet_watch`, `fleet_status`, `fleet_pool_status`, `fleet_operation_status`.
- Verify-after-write on every `fleet_write`: each write is followed by a readback and
  reports whether the device converged, not just whether it acknowledged.
- Risk tiering (`read_only` / `low_risk_actuator` / `safety_critical`) to gate bulk
  autonomous writes away from safety-critical devices without explicit confirmation.
- Structured JSONL tracing of scheduler and pool events to `.fleet_mcp/traces/trace.jsonl`.
- Simulated fleet test harness plus soak and chaos test suites.

[Unreleased]: https://github.com/fleet-mcp/ble-fleet-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/fleet-mcp/ble-fleet-mcp/releases/tag/v0.1.0
