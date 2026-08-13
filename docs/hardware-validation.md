# Hardware validation

> **Status: not yet run.** Everything in this repository has been validated against
> the simulated fleet harness (`tests/unit`, `tests/integration`, both green — see
> `CHANGELOG.md`). This document is the protocol for the real-hardware pass the
> project's definition-of-done requires, plus a place to record results once someone
> with physical BLE devices runs it. It is intentionally *not* filled in with
> fabricated numbers.

## Why this is a separate, later step

The simulated-fleet harness (`fleet_mcp.transports.fake.FakeTransport`) implements
the exact same `Transport` protocol the BLE transport does, and the soak/chaos test
suites exercise the pool manager and scheduler thoroughly against it. What it
*can't* validate is bleak's actual behavior against a real OS Bluetooth stack:

- The real number of simultaneous GATT connections your OS/adapter/driver combination
  actually supports before connections start failing or silently degrading — this is
  the number `FLEET_MAX_CONNECTIONS` should be set to, and it is not something bleak
  or the OS reports programmatically.
- Real connect/scan latency and failure modes (a device out of range, a device that's
  already connected to another host, a device that drops mid-operation).
- Cross-platform differences in bleak's backend (CoreBluetooth on macOS, BlueZ on
  Linux, WinRT on Windows).

## Protocol

Run this against **at least 2–3 real BLE peripherals** (any commodity BLE sensor, or
the `ble-mcp-server` demo peripheral pattern flashed to an ESP32 — see that project's
`examples/demo-device` for a cheap, controllable target).

### 1. Find your real connection limit

```bash
FLEET_LOG_LEVEL=DEBUG uv run python - <<'EOF'
import asyncio
from bleak import BleakClient, BleakScanner

async def main():
    devices = await BleakScanner.discover(timeout=10.0)
    print(f"found {len(devices)} devices")
    addrs = [d.address for d in devices][:8]  # try up to 8 at once

    clients = []
    for addr in addrs:
        c = BleakClient(addr)
        try:
            await c.connect()
            clients.append(c)
            print(f"OK  connected #{len(clients)}: {addr}")
        except Exception as exc:
            print(f"FAIL connecting #{len(clients) + 1} ({addr}): {exc}")
            break

    for c in clients:
        await c.disconnect()

asyncio.run(main())
EOF
```

Run this on each OS you support. Record where it actually starts failing — that's
your real `FLEET_MAX_CONNECTIONS`, not a number pulled from a spec sheet.

### 2. Run fleet-mcp against real devices

Register a fleet by explicit address (from step 1's scan output) and run
`fleet_read`/`fleet_write` through the real MCP server, with `FLEET_MAX_CONNECTIONS`
set to what you measured:

```bash
FLEET_MAX_CONNECTIONS=<measured> FLEET_ALLOW_WRITES=1 uv run fleet-mcp
```

Then, from an MCP client (or a short script using `fleet_mcp.server.app.create_app`
directly, the way `tests/unit/test_server_tools.py` does against the fake transport),
run the same read/write/watch scenarios the soak and chaos tests cover, but against
real devices:

- `fleet_read` across all registered devices — confirm every device resolves to
  `success` or a specific error, none left `still_queued` after the operation
  finishes.
- `fleet_write` with verify-after-write — confirm `converged` is actually `true` for
  a real characteristic write, not just `acknowledged`.
- Power off / walk one device out of range mid-batch — confirm the rest of the fleet
  still completes promptly (the chaos test's assertion, against real radio failure
  modes instead of `FakePeripheral(unreachable=True)`).
- Watch a resource that actually notifies (a button, a motion sensor) and confirm
  `fleet_watch(action="poll")` returns real buffered readings.

### 3. Record results here

| OS | Adapter | Real connection limit observed | Notes |
| --- | --- | --- | --- |
| macOS | _(fill in)_ | _(fill in)_ | |
| Linux | _(fill in)_ | _(fill in)_ | |
| Windows | _(fill in)_ | _(fill in)_ | |

| Device | Role | Read | Write + verify | Notes |
| --- | --- | --- | --- | --- |
| _(fill in)_ | | | | |
| _(fill in)_ | | | | |

## Contributing a validation pass

If you have BLE hardware and want to contribute this, open a PR updating this file
with your results (device models, OS/adapter, and the numbers from the protocol
above) — that's the whole deliverable, no code changes required unless you find a
real-hardware bug the simulated harness didn't catch.
