"""Structured JSONL tracing for scheduler jobs and pool events.

Mirrors ble-mcp-server's tracing pattern: on by default, one JSON object per line,
written to .fleet_mcp/traces/trace.jsonl (or FLEET_TRACE_PATH), never touches stdout.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class Tracer:
    """Append-only JSONL event sink. Safe to call from multiple asyncio tasks/threads."""

    def __init__(self, path: Path | None, enabled: bool = True) -> None:
        self._path = path
        self._enabled = enabled and path is not None
        self._lock = threading.Lock()
        if self._enabled and path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def emit(self, event: str, **fields: Any) -> None:
        if not self._enabled or self._path is None:
            return
        record = {"ts": time.time(), "event": event, **fields}
        line = json.dumps(record, default=str, separators=(",", ":"))
        with self._lock, self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


class NullTracer(Tracer):
    """A tracer that discards everything; used when FLEET_TRACE_ENABLED=0."""

    def __init__(self) -> None:
        super().__init__(path=None, enabled=False)
