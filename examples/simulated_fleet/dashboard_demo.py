#!/usr/bin/env python3
"""Runs the web dashboard against a lively simulated fleet: a background loop keeps
issuing fleet_read/fleet_write calls (with a couple of chaos devices and one watched
resource) so there's always something moving to look at.

    uv run python examples/simulated_fleet/dashboard_demo.py --devices 30 --cap 4
    # then open http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import random

from fleet_mcp.config import Settings
from fleet_mcp.server import tools as t
from fleet_mcp.server.context import build_context
from fleet_mcp.server.dashboard import start_dashboard
from fleet_mcp.transports.fake import make_simulated_fleet


async def _churn(ctx, device_count: int) -> None:
    """Keep the fleet busy: alternate reads and writes, occasionally hitting a
    chaos device, so the dashboard's pool/operations/health panels stay lively."""
    rng = random.Random(7)
    addresses = [f"SIM:{i:04d}" for i in range(device_count)]
    while True:
        subset = rng.sample(addresses, k=min(8, len(addresses)))
        if rng.random() < 0.5:
            await t.fleet_read(ctx, fleet="warehouse", resource="temperature_c", addresses=subset)
        else:
            await t.fleet_write(
                ctx,
                fleet="warehouse",
                resource="temperature_c",
                value=round(rng.uniform(15, 30), 1),
                addresses=subset[:3],
            )
        await asyncio.sleep(0.4)


WATCHED_ADDRESS = "SIM:0000"


async def run(device_count: int, cap: int, port: int, chaos_fraction: float) -> None:
    transport = make_simulated_fleet(device_count, max_concurrent=cap)
    if chaos_fraction:
        rng = random.Random(42)
        # Keep the watched device out of the chaos pool — otherwise this demo would
        # (deterministically, depending on device_count) fail to establish the watch
        # and silently show nothing in the "Active watches" panel.
        candidates = [a for a in transport.addresses if a != WATCHED_ADDRESS]
        for addr in rng.sample(candidates, k=int(device_count * chaos_fraction)):
            transport.peripheral(addr).unreachable = True

    settings = Settings(max_connections=cap, allow_writes=True, trace_enabled=False)
    ctx = build_context(transport, "fake", settings)
    await t.fleet_register(ctx, name="warehouse", name_pattern="sim-sensor")
    watch_result = await t.fleet_watch(
        ctx, fleet="warehouse", resource="temperature_c", addresses=[WATCHED_ADDRESS]
    )
    if watch_result["errors"]:
        print(f"warning: failed to establish watch: {watch_result['errors']}")

    start_dashboard(ctx, "127.0.0.1", port)
    print(f"Dashboard: http://127.0.0.1:{port}")
    print(f"Simulating {device_count} devices behind a {cap}-connection pool. Ctrl+C to stop.")

    async def _notify_watch() -> None:
        rng = random.Random(3)
        while True:
            await transport.notify(WATCHED_ADDRESS, "temperature_c", round(rng.uniform(15, 30), 1))
            await asyncio.sleep(1.0)

    await asyncio.gather(_churn(ctx, device_count), _notify_watch())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--devices", type=int, default=30, dest="device_count")
    parser.add_argument("--cap", type=int, default=4)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--chaos", type=float, default=0.1, dest="chaos_fraction")
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args.device_count, args.cap, args.port, args.chaos_fraction))


if __name__ == "__main__":
    main()
