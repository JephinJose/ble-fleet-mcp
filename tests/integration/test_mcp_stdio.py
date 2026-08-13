"""End-to-end functional check: drives the real MCP server over a real stdio
subprocess and real JSON-RPC framing — not the in-process call_tool() shortcut the
unit tests use. This is the closest thing to "does this actually work when a real MCP
client like Claude Code/Desktop launches it" that's possible without a live client.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

_SERVER_SCRIPT = Path(__file__).parent / "_stdio_server_fake.py"


@pytest.mark.asyncio
async def test_real_stdio_server_handshake_and_tool_calls() -> None:
    params = StdioServerParameters(command=sys.executable, args=[str(_SERVER_SCRIPT)])

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init_result = await session.initialize()
        assert init_result.server_info.name == "fleet-mcp"

        tools = await session.list_tools()
        tool_names = {t.name for t in tools.tools}
        assert tool_names == {
            "fleet_register",
            "fleet_scan",
            "fleet_read",
            "fleet_write",
            "fleet_watch",
            "fleet_status",
            "fleet_pool_status",
            "fleet_operation_status",
        }

        register_result = await session.call_tool(
            "fleet_register", {"name": "all", "addresses": ["D0", "D1", "D2"]}
        )
        assert register_result.is_error is not True
        assert register_result.structured_content["device_count"] == 3

        read_result = await session.call_tool("fleet_read", {"fleet": "all", "resource": "temp"})
        assert read_result.is_error is not True
        payload = read_result.structured_content
        assert payload["status"] == "completed"
        assert payload["completed"] == 3
        for r in payload["results"].values():
            assert r["status"] == "success"

        write_result = await session.call_tool(
            "fleet_write",
            {"fleet": "all", "resource": "temp", "value": 99, "addresses": ["D0"]},
        )
        assert write_result.is_error is not True
        write_payload = write_result.structured_content
        assert write_payload["results"]["D0"]["write"]["converged"] is True

        pool_result = await session.call_tool("fleet_pool_status", {})
        assert pool_result.structured_content["max_connections"] == 2

        status_result = await session.call_tool("fleet_status", {"fleet": "all"})
        assert status_result.structured_content["device_count"] == 3


@pytest.mark.asyncio
async def test_real_stdio_server_rejects_write_when_disabled() -> None:
    params = StdioServerParameters(
        command=sys.executable, args=[str(_SERVER_SCRIPT)], env={"FLEET_ALLOW_WRITES": "0"}
    )

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        await session.call_tool("fleet_register", {"name": "all", "addresses": ["D0"]})
        result = await session.call_tool(
            "fleet_write", {"fleet": "all", "resource": "temp", "value": 1}
        )
        assert result.is_error is True


@pytest.mark.asyncio
async def test_real_stdio_server_reports_unknown_fleet_as_error() -> None:
    params = StdioServerParameters(command=sys.executable, args=[str(_SERVER_SCRIPT)])

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("fleet_read", {"fleet": "nope", "resource": "temp"})
        assert result.is_error is True
