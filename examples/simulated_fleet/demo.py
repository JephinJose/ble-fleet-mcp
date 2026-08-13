#!/usr/bin/env python3
"""Runnable demo: read a resource across a simulated fleet through the exact same MCP
tool layer a real agent would call, with no BLE hardware involved.

    uv run python examples/simulated_fleet/demo.py --devices 50 --cap 4

This is the harness referenced by docs/architecture.md and the soak/chaos tests in
tests/integration/ — swap `FakeTransport` for `BleTransport` and everything else
(pool, scheduler, circuit breaker, MCP tools) is unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time

from fleet_mcp.config import Settings
from fleet_mcp.server import tools as t
from fleet_mcp.server.context import build_context
from fleet_mcp.transports.fake import make_simulated_fleet


async def run(device_count: int, cap: int, chaos_fraction: float) -> None:
    transport = make_simulated_fleet(device_count, max_concurrent=cap)

    if chaos_fraction:
        rng = random.Random(42)
        for addr in rng.sample(transport.addresses, k=int(device_count * chaos_fraction)):
            transport.peripheral(addr).unreachable = True

    settings = Settings(max_connections=cap, trace_enabled=False)
    ctx = build_context(transport, "fake", settings)

    print(f"Registering {device_count} simulated sensors (radio cap: {cap})...")
    await t.fleet_register(ctx, name="warehouse", name_pattern="sim-sensor")

    print("Reading temperature_c across the whole fleet...")
    start = time.monotonic()
    result = await t.fleet_read(ctx, fleet="warehouse", resource="temperature_c", quick_wait_s=30.0)
    elapsed = time.monotonic() - start

    statuses: dict[str, int] = {}
    for r in result["results"].values():
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1

    print(f"\nDone in {elapsed:.2f}s over {device_count} devices with only {cap} connections.")
    print(f"Status breakdown: {json.dumps(statuses, indent=2)}")

    pool_status = await t.fleet_pool_status(ctx)
    print(f"\nFinal pool status: {json.dumps(pool_status, indent=2)}")

    await ctx.scheduler.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--devices", type=int, default=50, dest="device_count")
    parser.add_argument("--cap", type=int, default=4)
    parser.add_argument(
        "--chaos",
        type=float,
        default=0.0,
        dest="chaos_fraction",
        help="fraction of devices to make unreachable (0.0-1.0)",
    )
    args = parser.parse_args()
    asyncio.run(run(args.device_count, args.cap, args.chaos_fraction))


if __name__ == "__main__":
    main()
