"""Standalone entrypoint used only by test_mcp_stdio.py: runs the real MCP server
(real stdio transport, real JSON-RPC framing) against a small simulated fleet instead
of real BLE hardware. Launched as a subprocess, never imported directly.
"""

from __future__ import annotations

import os

from fleet_mcp.config import Settings
from fleet_mcp.server.app import create_app
from fleet_mcp.server.context import build_context
from fleet_mcp.transports.fake import FakePeripheral, FakeTransport


def main() -> None:
    transport = FakeTransport(max_concurrent=2)
    for i in range(3):
        transport.add_peripheral(
            FakePeripheral(address=f"D{i}", name=f"sensor-{i}", resources={"temp": 20 + i})
        )
    # Honor FLEET_ALLOW_WRITES from the subprocess env so tests can exercise both the
    # write-enabled and write-disabled paths through the real stdio protocol.
    settings = Settings(
        max_connections=2,
        allow_writes=os.environ.get("FLEET_ALLOW_WRITES", "1") not in ("0", "false", ""),
        trace_enabled=False,
    )
    ctx = build_context(transport, "fake", settings)
    app = create_app(ctx)
    app.run()


if __name__ == "__main__":
    main()
