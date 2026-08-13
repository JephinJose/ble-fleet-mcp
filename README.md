# ble-fleet-mcp

An MCP server that lets an AI agent manage a **fleet** of constrained-connection
devices — 10, 100, or 1000+ of them — without ever having to know or reason about how
many the underlying radio can actually hold open at once.

BLE radios typically support somewhere between 3 and 7 simultaneous connections.
Existing BLE-to-MCP bridges expose `scan` / `connect` / `read` / `write` / `subscribe`
as direct tools and leave connection management to the agent. That breaks down the
moment a task involves more devices than the radio can hold open: *"check the
temperature on all 40 sensors in the warehouse"* turns into forty manual
connect/read/disconnect cycles, with the agent doing the bookkeeping itself — wasted
tokens, wasted latency, and a real failure mode when it gets the bookkeeping wrong.

`ble-fleet-mcp` hides all of that. The agent asks for fleet-level outcomes — *"read
all sensors"*, *"set brightness on every light in Zone 2"* — and the server handles
connection pooling, scheduling, retries, and partial failures underneath, inside a
hard concurrency cap it enforces itself.

```
Agent ──MCP──▶ fleet_read("warehouse", "temperature_c")
                       │
                       ▼
        [ scheduler batches 40 devices behind a 4-connection pool,
          retries failures with backoff, circuit-breaks the
          unresponsive ones, evicts idle connections to make room ]
                       │
                       ▼
        { "SIM:0001": 21.4, "SIM:0002": "unreachable", ... }
```

## Safety

- **Read-only by default.** `fleet_write` refuses to run unless the server is started
  with `FLEET_ALLOW_WRITES=1` — same posture as `ble-mcp-server`.
- **Verify-after-write.** Every write is followed by a readback; the result reports
  both `acknowledged` (the device accepted the write) and `converged` (the readback
  actually matches what was requested) rather than trusting the ack alone.
- **Risk tiering.** Devices registered as `safety_critical` (e.g. a lock, in a fleet
  that also has light bulbs) are excluded from `fleet_write` batches by default and
  reported as `confirmation_required`, unless their address is explicitly listed in
  `confirm_addresses` on that call. A bulk write to "everything in Zone 2" can never
  silently include a lock.
- **Nothing is silently dropped.** Every device in a fleet operation resolves to
  `success`, a specific error (`unreachable`, `timeout`, `write_rejected`), or
  `still_queued` (pollable via `fleet_operation_status`) — never just missing.
- **One bad device can't take down the fleet.** Per-device timeouts and a circuit
  breaker isolate unresponsive devices; see [docs/architecture.md](docs/architecture.md).

## Quick start

```bash
pip install ble-fleet-mcp
```

Or with `uv`:

```bash
uv add ble-fleet-mcp
```

Run it directly to sanity-check your BLE setup:

```bash
fleet-mcp
```

It speaks MCP over stdio, so in practice it's launched by your MCP client (see below),
not run standalone. A minimal session, once connected from an agent:

```
fleet_register(name="warehouse", name_pattern="sensor")
  -> {"fleet": "warehouse", "device_count": 40, "addresses": [...]}

fleet_read(fleet="warehouse", resource="temperature_c")
  -> {"operation_id": "…", "status": "completed", "total": 40, "completed": 40,
      "results": {"AA:BB:...:01": {"status": "success", "value": 21.4}, ...}}
```

No BLE hardware handy? [`examples/simulated_fleet/`](examples/simulated_fleet/) runs
the exact same tool layer against a simulated fleet of virtual peripherals:

```bash
uv run python examples/simulated_fleet/demo.py --devices 50 --cap 4
```

```
Registering 50 simulated sensors (radio cap: 4)...
Reading temperature_c across the whole fleet...

Done in 1.64s over 50 devices with only 4 connections.
Status breakdown: {
  "success": 50
}
```

## Adding it to your client

`ble-fleet-mcp` speaks standard MCP over stdio, so it works with any MCP-compatible
client. Writes stay off unless you explicitly set `FLEET_ALLOW_WRITES=1`.

### Claude Code

```bash
claude mcp add fleet-mcp -- fleet-mcp
```

Or add it to `.mcp.json` directly:

```json
{
  "mcpServers": {
    "fleet-mcp": {
      "command": "fleet-mcp",
      "env": {
        "FLEET_MAX_CONNECTIONS": "4",
        "FLEET_ALLOW_WRITES": "0"
      }
    }
  }
}
```

### Claude Desktop

Add to your `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "fleet-mcp": {
      "command": "fleet-mcp",
      "env": {
        "FLEET_MAX_CONNECTIONS": "4",
        "FLEET_ALLOW_WRITES": "0"
      }
    }
  }
}
```

### Cursor

Add to `.cursor/mcp.json` in your project (or the global `~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "fleet-mcp": {
      "command": "fleet-mcp",
      "env": {
        "FLEET_MAX_CONNECTIONS": "4",
        "FLEET_ALLOW_WRITES": "0"
      }
    }
  }
}
```

### VS Code (Copilot)

Add to `.vscode/mcp.json`:

```json
{
  "servers": {
    "fleet-mcp": {
      "type": "stdio",
      "command": "fleet-mcp",
      "env": {
        "FLEET_MAX_CONNECTIONS": "4",
        "FLEET_ALLOW_WRITES": "0"
      }
    }
  }
}
```

## Tools

| Tool | Purpose |
| --- | --- |
| `fleet_register` | Register a device or group (address list, name pattern, or service UUIDs) into a named fleet. |
| `fleet_scan` | Discover devices matching a filter, without connecting or registering. |
| `fleet_read` | Read one resource across a fleet (or subset). Per-device results, not all-or-nothing. |
| `fleet_write` | Write one resource across a fleet. Requires `FLEET_ALLOW_WRITES=1`; verify-after-write on every device. |
| `fleet_watch` | Subscribe to / unsubscribe from / poll buffered, debounced notifications for a resource across a fleet. |
| `fleet_status` | Per-device health: healthy/unhealthy, consecutive failures, connected. |
| `fleet_pool_status` | Connection pool telemetry: active/idle/evicting counts, queue depth, per-device wait times. |
| `fleet_operation_status` | Poll a `fleet_read`/`fleet_write` batch by `operation_id` for partial or complete results. |

Full input/output schemas: [docs/tools.md](docs/tools.md). Design rationale for the
pool manager, scheduler, and circuit breaker: [docs/architecture.md](docs/architecture.md).

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `FLEET_MAX_CONNECTIONS` | `4` | Hard cap on simultaneous BLE connections. Match this to what you measured for your adapter — see [docs/hardware-validation.md](docs/hardware-validation.md). |
| `FLEET_ALLOW_WRITES` | `0` | Set to `1` to allow `fleet_write`. Off by default. |
| `FLEET_REQUIRE_CONFIRMATION_SAFETY_CRITICAL` | `1` | Whether `safety_critical`-tiered devices require explicit `confirm_addresses` on writes. |
| `FLEET_DEVICE_TIMEOUT_S` | `10.0` | Per-device timeout (connect + operation) before it's treated as a failure. |
| `FLEET_OPERATION_TIMEOUT_S` | `60.0` | Default overall timeout for a `fleet_read`/`fleet_write` batch, after which unresolved devices report `timeout` and the rest of the batch is returned. |
| `FLEET_BACKOFF_INITIAL_S` | `0.5` | Initial per-device retry backoff. |
| `FLEET_BACKOFF_MAX_S` | `30.0` | Cap on per-device retry backoff. |
| `FLEET_BACKOFF_MULTIPLIER` | `2.0` | Exponential backoff multiplier. |
| `FLEET_CIRCUIT_BREAKER_THRESHOLD` | `3` | Consecutive failures before a device is marked unhealthy and stops being retried. |
| `FLEET_CIRCUIT_BREAKER_COOLDOWN_S` | `60.0` | How long an unhealthy device is skipped before a single probe attempt is allowed through. |
| `FLEET_TRACE_ENABLED` | `1` | Structured JSONL tracing of pool/scheduler events. |
| `FLEET_TRACE_PATH` | `.fleet_mcp/traces/trace.jsonl` | Where trace events are written. |
| `FLEET_LOG_LEVEL` | `INFO` | Log level; logs always go to stderr so stdout stays clean for the MCP stdio transport. |

## Transports

| Transport | Status |
| --- | --- |
| BLE (via [`bleak`](https://github.com/hbldh/bleak)) | v1, shipped |
| Zigbee coordinator | Roadmap, not started |
| Thread border router | Roadmap, not started |

The pool manager, scheduler, and MCP tool layer never import anything
transport-specific — a new transport is a plugin, not a rewrite. See
[CONTRIBUTING.md](CONTRIBUTING.md#adding-a-new-transport-plugin).

## Roadmap (not blocking 1.0.0)

- A second transport plugin (Zigbee or Thread) to prove the plugin interface
  generalizes beyond BLE.
- A minimal web dashboard over the same telemetry `fleet_pool_status`/`fleet_status`
  already expose.
- A "fleet template" registry for shareable configs of common device populations.

## Development

```bash
git clone https://github.com/JephinJose/ble-fleet-mcp.git
cd ble-fleet-mcp
uv sync --extra dev
uv run pre-commit install
uv run pytest tests/unit
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow and how to add a
transport plugin.

## License

MIT — see [LICENSE](LICENSE).
