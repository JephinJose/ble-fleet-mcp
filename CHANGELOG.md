# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Scheduler caught the builtin `TimeoutError` where `asyncio.wait_for()` raises
  `asyncio.TimeoutError` — the same class on Python 3.11+, but a distinct,
  silently-uncaught one on 3.10. Affected the scheduler, the MCP tool layer's
  quick-wait, and the BLE transport's timeout handling.
- `WatchManager.subscribe()`/`unsubscribe()` had a check-then-acquire race: two
  concurrent watch calls for the same not-yet-watched device could each open their
  own connection to it, leaking one. Now serialized under a lock.
- A job that completed right as its operation's timeout fired could overwrite an
  already-reported `timeout` result back to `success` after the caller had already
  observed the operation as `timed_out`. First resolution now wins.
- Removed two unused, never-wired-up exception classes (`PoolExhaustedError`,
  `SafetyCriticalConfirmationRequired`).

### Added

- End-to-end test driving the real MCP server over a real stdio subprocess and real
  JSON-RPC framing (`tests/integration/test_mcp_stdio.py`), rather than only the
  in-process tool-call shortcut the rest of the suite uses.
- Minimal, opt-in (`FLEET_DASHBOARD_PORT`) read-only web dashboard over the same
  telemetry `fleet_pool_status`/`fleet_status` expose: live pool gauges, per-device
  health, recent operations, and active watches. Stdlib `http.server`, no new
  required dependency. `examples/simulated_fleet/dashboard_demo.py` demos it against
  a simulated fleet.
- `Scheduler.list_operations()`, and a bound (`max_operation_history`, default 500)
  on how many completed operations the scheduler keeps in memory — previously
  unbounded, a slow memory leak on a long-running server.

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

[Unreleased]: https://github.com/JephinJose/ble-fleet-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/JephinJose/ble-fleet-mcp/releases/tag/v0.1.0
