# Simulated fleet demo

Runs a fleet read against a simulated fleet of virtual BLE peripherals, through the
exact same MCP tool-layer code (`fleet_mcp.server.tools`) a real agent would call —
no BLE hardware, no radio, no adapter required.

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

Final pool status: {
  "max_connections": 4,
  "active_connections": 0,
  "idle_connections": 4,
  "evicting_connections": 0,
  "queue_depth": 0,
  "per_device_wait_s": {},
  "watched_devices": 0
}
```

Flags:

| Flag         | Default | Meaning                                                        |
| ------------ | ------- | ---------------------------------------------------------------- |
| `--devices`  | `50`    | Number of simulated peripherals in the fleet                     |
| `--cap`      | `4`     | Connection pool cap (stand-in for `FLEET_MAX_CONNECTIONS`)       |
| `--chaos`    | `0.0`   | Fraction (0.0–1.0) of devices to make unreachable, to see the circuit breaker and per-device error reporting in action |

Try `--devices 200 --cap 4 --chaos 0.15` to see a fleet an order of magnitude larger
than the radio's connection limit get drained cleanly, with unreachable devices
reported individually rather than stalling the healthy majority — this is exactly
what `tests/integration/test_soak.py` and `tests/integration/test_chaos.py` assert on
automatically.

The virtual peripherals themselves live in [`fleet_mcp.transports.fake`](../../src/fleet_mcp/transports/fake.py)
(`FakeTransport` / `FakePeripheral` / `make_simulated_fleet`) — the same fixture the
unit and integration test suites use. Swapping `FakeTransport` for `BleTransport` in
`build_context(...)` is the only change needed to point this demo at real hardware.
