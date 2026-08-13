# Contributing

Thanks for considering a contribution to `ble-fleet-mcp`. This document covers local
setup, the test/lint workflow, and — the most likely reason you're here — how to add a
new transport plugin.

## Local setup

```bash
git clone https://github.com/fleet-mcp/ble-fleet-mcp.git
cd ble-fleet-mcp
uv sync --extra dev
uv run pre-commit install
```

## Workflow

```bash
uv run pytest tests/unit                 # fast, no BLE / hardware required
uv run pytest tests/integration -m soak   # simulated-fleet soak test
uv run pytest tests/integration -m chaos  # simulated-fleet chaos test
uv run ruff check .
uv run ruff format .
uv run mypy src/fleet_mcp
```

CI runs the same commands on every PR across Linux, macOS, and Windows. A PR won't
merge unless all of them are green.

## Adding a new transport plugin

The pool manager, scheduler, circuit breaker, and MCP tool layer never import anything
transport-specific — they only depend on the `Transport` protocol in
[`src/fleet_mcp/core/types.py`](src/fleet_mcp/core/types.py). A new transport (Zigbee,
Thread, a proprietary radio, whatever) is a new module under `src/fleet_mcp/transports/`
that implements that protocol; nothing in `core/` should need to change.

```python
class Transport(Protocol):
    async def discover(self, filter: DiscoveryFilter) -> list[DeviceHandle]: ...
    async def connect(self, device: DeviceHandle) -> Connection: ...
    async def disconnect(self, conn: Connection) -> None: ...
    async def read(self, conn: Connection, resource: str) -> Reading: ...
    async def write(self, conn: Connection, resource: str, value: Any) -> WriteResult: ...
    async def subscribe(self, conn: Connection, resource: str, callback) -> Subscription: ...
    def max_concurrent_connections(self) -> int: ...
```

Steps:

1. Create `src/fleet_mcp/transports/<name>/transport.py` implementing every method
   above. Look at `src/fleet_mcp/transports/ble/transport.py` for a real example, or
   `src/fleet_mcp/transports/fake.py` for the simplest possible one.
2. `max_concurrent_connections()` should return the transport's real hard limit (for a
   radio-based transport, this is usually a fixed number the underlying stack imposes).
   The pool manager treats this as authoritative unless `FLEET_MAX_CONNECTIONS`
   overrides it.
3. Raise the specific exceptions from `fleet_mcp.core.types`
   (`DeviceUnreachable`, `OperationTimeout`, `WriteRejected`) rather than generic
   exceptions — the scheduler uses the exception type to decide whether to retry and
   what status to report back to the agent.
4. `resource` strings are transport-defined (e.g. a BLE GATT characteristic UUID, a
   Zigbee cluster/attribute pair) — document the convention your transport uses in its
   module docstring.
5. Add unit tests for your transport's own logic (parsing, resource-name mapping,
   error translation), plus one integration-style test that runs a small operation
   through `ConnectionPoolManager` + `Scheduler` end to end, the way
   `tests/integration/` does for the fake transport.
6. Reuse `FakeTransport` (`src/fleet_mcp/transports/fake.py`) as a template for a
   simulated version of your new transport if hardware-in-the-loop testing isn't
   practical for every PR — that's exactly what it's there for.
7. Update `docs/architecture.md` and `README.md`'s transport table with the new plugin.

You do **not** need to touch `core/pool.py`, `core/scheduler.py`, or
`core/circuit_breaker.py` — if you find yourself doing so, that's a sign the
`Transport` protocol is missing something generic, which is worth raising as an issue
before writing the plugin.

## Commit / PR conventions

- Keep PRs focused; a transport plugin PR shouldn't also refactor the scheduler.
- Add a `CHANGELOG.md` entry under `[Unreleased]` for any user-visible change.
- Run `uv run pre-commit run --all-files` before pushing.
