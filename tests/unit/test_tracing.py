from __future__ import annotations

import json

import pytest

from fleet_mcp.core.pool import ConnectionPoolManager
from fleet_mcp.core.scheduler import JobKind, Scheduler
from fleet_mcp.core.tracing import NullTracer, Tracer
from fleet_mcp.core.types import DeviceHandle
from fleet_mcp.transports.fake import FakePeripheral, FakeTransport


def _read_events(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_null_tracer_writes_nothing(tmp_path) -> None:
    tracer = NullTracer()
    assert tracer.enabled is False
    tracer.emit("anything", foo="bar")  # must not raise even without a path


def test_disabled_tracer_writes_nothing(tmp_path) -> None:
    path = tmp_path / "trace.jsonl"
    tracer = Tracer(path=path, enabled=False)
    tracer.emit("event")
    assert not path.exists()


def test_tracer_writes_one_json_object_per_line(tmp_path) -> None:
    path = tmp_path / "traces" / "trace.jsonl"
    tracer = Tracer(path=path, enabled=True)
    tracer.emit("test.event", address="d0", count=3)
    tracer.emit("test.event", address="d1", count=5)

    events = _read_events(path)
    assert len(events) == 2
    assert events[0]["event"] == "test.event"
    assert events[0]["address"] == "d0"
    assert "ts" in events[0]


@pytest.mark.asyncio
async def test_pool_and_scheduler_emit_trace_events(tmp_path) -> None:
    path = tmp_path / "trace.jsonl"
    tracer = Tracer(path=path, enabled=True)

    transport = FakeTransport(max_concurrent=1)
    transport.add_peripheral(FakePeripheral(address="d0", resources={"r": 1}))
    transport.add_peripheral(FakePeripheral(address="d1", resources={"r": 2}))
    pool = ConnectionPoolManager(transport, max_connections=1, tracer=tracer)
    sched = Scheduler(pool, transport, device_timeout_s=1.0, tracer=tracer)

    devices = [
        DeviceHandle(address="d0", transport_kind="fake"),
        DeviceHandle(address="d1", transport_kind="fake"),
    ]
    op = await sched.submit(devices, JobKind.READ, "r", timeout_s=5.0)
    await op.done_event.wait()
    await sched.stop()

    events = _read_events(path)
    event_names = {e["event"] for e in events}
    assert "scheduler.submit" in event_names
    assert "pool.connect" in event_names
    assert "scheduler.job_success" in event_names
    # cap=1 with 2 devices forces an eviction
    assert "pool.evict" in event_names
