"""Priority scheduler: decomposes a fleet operation into per-device jobs, dispatches
them against the connection pool with a bounded worker pool, retries with per-device
exponential backoff, and skips devices the circuit breaker has opened.

Protocol-agnostic: only depends on fleet_mcp.core.{types,pool,circuit_breaker,tracing}.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from fleet_mcp.core.circuit_breaker import CircuitBreaker
from fleet_mcp.core.pool import ConnectionPoolManager
from fleet_mcp.core.tracing import NullTracer, Tracer
from fleet_mcp.core.types import (
    DeviceHandle,
    DeviceUnreachable,
    OperationTimeout,
    Reading,
    Transport,
    WriteRejected,
    WriteResult,
)


class Priority(int, Enum):
    HIGH = 0
    NORMAL = 1
    LOW = 2


class JobKind(str, Enum):
    READ = "read"
    WRITE = "write"
    SUBSCRIBE = "subscribe"


class JobStatus(str, Enum):
    STILL_QUEUED = "still_queued"
    SUCCESS = "success"
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    WRITE_REJECTED = "write_rejected"
    ERROR = "error"


TERMINAL_STATUSES = frozenset(
    {
        JobStatus.SUCCESS,
        JobStatus.UNREACHABLE,
        JobStatus.TIMEOUT,
        JobStatus.WRITE_REJECTED,
        JobStatus.ERROR,
    }
)

# Failure classes worth retrying with backoff; WriteRejected is treated as deterministic
# and is not retried (retrying an invalid write value won't change the outcome).
#
# Use asyncio.TimeoutError explicitly rather than the builtin: they're the same class
# on Python 3.11+, but asyncio.wait_for() raises a *distinct* asyncio.TimeoutError on
# 3.10, which a bare `except TimeoutError` silently fails to catch there.
_RETRYABLE = (DeviceUnreachable, OperationTimeout, asyncio.TimeoutError)


@dataclass(slots=True)
class Job:
    device: DeviceHandle
    kind: JobKind
    resource: str
    op_id: str
    value: Any = None
    priority: Priority = Priority.NORMAL
    verify_write: bool = True
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.monotonic)
    attempt: int = 0
    max_attempts: int = 3
    next_ready_at: float = 0.0


@dataclass(slots=True)
class JobResult:
    address: str
    resource: str
    kind: JobKind
    status: JobStatus = JobStatus.STILL_QUEUED
    attempts: int = 0
    error: str | None = None
    reading: Reading | None = None
    write_result: WriteResult | None = None
    finished_at: float | None = None


@dataclass(slots=True)
class FleetOperation:
    op_id: str
    jobs: dict[str, Job]
    results: dict[str, JobResult]
    total: int
    created_at: float = field(default_factory=time.time)
    timeout_s: float = 60.0
    running: bool = True
    timed_out: bool = False
    finished_at: float | None = None
    done_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def done_count(self) -> int:
        return sum(1 for r in self.results.values() if r.status in TERMINAL_STATUSES)

    @property
    def status(self) -> str:
        if self.running:
            return "running"
        return "timed_out" if self.timed_out else "completed"

    def snapshot(self) -> dict[str, Any]:
        return {
            "operation_id": self.op_id,
            "status": self.status,
            "total": self.total,
            "completed": self.done_count,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "results": {
                addr: {
                    "resource": r.resource,
                    "kind": r.kind.value,
                    "status": r.status.value,
                    "attempts": r.attempts,
                    "error": r.error,
                    "value": r.reading.value if r.reading else None,
                    "write": (
                        {
                            "acknowledged": r.write_result.acknowledged,
                            "converged": r.write_result.converged,
                            "readback_value": r.write_result.readback_value,
                        }
                        if r.write_result
                        else None
                    ),
                }
                for addr, r in self.results.items()
            },
        }


class Scheduler:
    def __init__(
        self,
        pool: ConnectionPoolManager,
        transport: Transport,
        circuit_breaker: CircuitBreaker | None = None,
        device_timeout_s: float = 10.0,
        backoff_initial_s: float = 0.5,
        backoff_max_s: float = 30.0,
        backoff_multiplier: float = 2.0,
        max_attempts: int = 3,
        tracer: Tracer | None = None,
        max_operation_history: int = 500,
    ) -> None:
        self._pool = pool
        self._transport = transport
        self._cb = circuit_breaker or CircuitBreaker()
        self._device_timeout_s = device_timeout_s
        self._backoff_initial_s = backoff_initial_s
        self._backoff_max_s = backoff_max_s
        self._backoff_multiplier = backoff_multiplier
        self._max_attempts = max_attempts
        self._tracer = tracer or NullTracer()
        self._max_operation_history = max_operation_history

        self._pending: list[Job] = []
        self._lock = asyncio.Lock()
        self._event = asyncio.Event()
        # Insertion-ordered so pruning can drop the oldest *completed* operations
        # first without scanning the whole history — a long-running server would
        # otherwise accumulate FleetOperation objects (and their per-device results)
        # forever, one per fleet_read/fleet_write call.
        self._operations: OrderedDict[str, FleetOperation] = OrderedDict()
        self._workers: list[asyncio.Task[None]] = []
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._cb

    def list_operations(self) -> list[FleetOperation]:
        """Most-recently-submitted operations first, bounded by max_operation_history."""
        return list(reversed(self._operations.values()))

    def _prune_operation_history(self) -> None:
        while len(self._operations) > self._max_operation_history:
            for op_id, op in self._operations.items():
                if not op.running:
                    del self._operations[op_id]
                    break
            else:
                break  # every tracked operation is still running; nothing safe to drop

    async def start(self) -> None:
        if self._workers:
            return
        worker_count = max(1, self._pool.max_connections)
        self._workers = [asyncio.create_task(self._worker_loop()) for _ in range(worker_count)]

    async def stop(self) -> None:
        self._closed = True
        self._event.set()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    def get_operation(self, op_id: str) -> FleetOperation | None:
        return self._operations.get(op_id)

    async def submit(
        self,
        devices: list[DeviceHandle],
        kind: JobKind,
        resource: str,
        value: Any = None,
        priority: Priority = Priority.NORMAL,
        verify_write: bool = True,
        timeout_s: float = 60.0,
        max_attempts: int = 3,
    ) -> FleetOperation:
        await self.start()
        op_id = uuid.uuid4().hex
        jobs: dict[str, Job] = {}
        results: dict[str, JobResult] = {}
        for device in devices:
            job = Job(
                device=device,
                kind=kind,
                resource=resource,
                value=value,
                op_id=op_id,
                priority=priority,
                verify_write=verify_write,
                max_attempts=max_attempts,
            )
            jobs[job.job_id] = job
            results[device.address] = JobResult(
                address=device.address, resource=resource, kind=kind
            )
        op = FleetOperation(
            op_id=op_id, jobs=jobs, results=results, total=len(jobs), timeout_s=timeout_s
        )
        self._operations[op_id] = op
        self._prune_operation_history()

        async with self._lock:
            self._pending.extend(jobs.values())
        self._event.set()
        self._tracer.emit("scheduler.submit", op_id=op_id, kind=kind.value, count=len(jobs))

        watch_task = asyncio.create_task(self._watch_operation(op))
        self._background_tasks.add(watch_task)
        watch_task.add_done_callback(self._background_tasks.discard)
        return op

    async def _watch_operation(self, op: FleetOperation) -> None:
        # `_finish()` is the authoritative place that flips op.running=False once every
        # job resolves — it does so synchronously with setting done_event, so any
        # caller awaiting done_event directly (e.g. the MCP tool layer's quick-wait)
        # never observes running=True after the event fires. This task only needs to
        # handle the timeout path.
        try:
            await asyncio.wait_for(op.done_event.wait(), timeout=op.timeout_s)
        except asyncio.TimeoutError:
            if not op.running:
                # _finish() already resolved every job right as the timeout fired.
                return
            op.timed_out = True
            self._tracer.emit("scheduler.operation_timeout", op_id=op.op_id)
            async with self._lock:
                self._pending = [j for j in self._pending if j.op_id != op.op_id]
            for result in op.results.values():
                if result.status == JobStatus.STILL_QUEUED:
                    result.status = JobStatus.TIMEOUT
                    result.error = "fleet operation timeout exceeded"
                    result.finished_at = time.time()
            op.running = False
            op.finished_at = time.time()
            op.done_event.set()

    async def _worker_loop(self) -> None:
        while not self._closed:
            job = await self._pick_job()
            if job is None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._event.wait(), timeout=0.25)
                self._event.clear()
                continue
            await self._execute(job)

    async def _pick_job(self) -> Job | None:
        async with self._lock:
            now = time.monotonic()
            best: Job | None = None
            best_key: tuple[int, int, float] | None = None
            for job in self._pending:
                if job.next_ready_at > now:
                    continue
                if not self._cb.is_available(job.device.address):
                    continue
                connected_bonus = 0 if self._pool.is_connected(job.device.address) else 1
                key = (job.priority.value, connected_bonus, job.created_at)
                if best_key is None or key < best_key:
                    best_key = key
                    best = job
            if best is not None:
                self._pending.remove(best)
            return best

    async def _do_kind(self, conn: Any, job: Job) -> Reading | WriteResult:
        if job.kind == JobKind.READ:
            return await self._transport.read(conn, job.resource)
        if job.kind == JobKind.WRITE:
            result = await self._transport.write(conn, job.resource, job.value)
            if not result.acknowledged:
                raise WriteRejected(f"write to {job.resource} not acknowledged")
            if job.verify_write:
                reading = await self._transport.read(conn, job.resource)
                converged = reading.value == job.value
                result = WriteResult(
                    address=result.address,
                    resource=result.resource,
                    requested_value=result.requested_value,
                    acknowledged=result.acknowledged,
                    converged=converged,
                    readback_value=reading.value,
                )
            return result
        raise ValueError(f"unsupported job kind for submit(): {job.kind}")

    async def _execute(self, job: Job) -> None:
        op = self._operations.get(job.op_id)
        if op is None or not op.running:
            return
        job.attempt += 1
        try:

            async def _acquire_and_run() -> Reading | WriteResult:
                async with self._pool.acquire(job.device) as conn:
                    return await self._do_kind(conn, job)

            # The timeout covers connection setup *and* the operation itself, so a
            # device that hangs on connect can't starve the pool slot indefinitely.
            outcome = await asyncio.wait_for(_acquire_and_run(), timeout=self._device_timeout_s)
            self._cb.record_success(job.device.address)
            self._tracer.emit(
                "scheduler.job_success",
                op_id=job.op_id,
                address=job.device.address,
                kind=job.kind.value,
                attempt=job.attempt,
            )
            self._finish(op, job, outcome=outcome)
        except _RETRYABLE as exc:
            self._cb.record_failure(job.device.address, reason=repr(exc))
            self._tracer.emit(
                "scheduler.job_failure",
                op_id=job.op_id,
                address=job.device.address,
                attempt=job.attempt,
                error=repr(exc),
                retryable=True,
            )
            if job.attempt < job.max_attempts and self._cb.is_available(job.device.address):
                delay = min(
                    self._backoff_initial_s * (self._backoff_multiplier ** (job.attempt - 1)),
                    self._backoff_max_s,
                )
                job.next_ready_at = time.monotonic() + delay
                async with self._lock:
                    self._pending.append(job)
                self._event.set()
            else:
                status = (
                    JobStatus.TIMEOUT
                    if isinstance(exc, (OperationTimeout, asyncio.TimeoutError))
                    else JobStatus.UNREACHABLE
                )
                self._finish(op, job, error=str(exc) or repr(exc), status=status)
        except WriteRejected as exc:
            self._cb.record_failure(job.device.address, reason=repr(exc))
            self._finish(op, job, error=str(exc), status=JobStatus.WRITE_REJECTED)
        except Exception as exc:
            self._cb.record_failure(job.device.address, reason=repr(exc))
            self._finish(op, job, error=str(exc), status=JobStatus.ERROR)

    def _finish(
        self,
        op: FleetOperation,
        job: Job,
        outcome: Reading | WriteResult | None = None,
        error: str | None = None,
        status: JobStatus | None = None,
    ) -> None:
        result = op.results[job.device.address]
        if result.status in TERMINAL_STATUSES:
            # Already resolved — e.g. the operation-level watchdog force-timed this
            # job out right as it was about to complete on its own. First resolution
            # wins rather than flip-flopping a result the caller may already be
            # holding (via fleet_operation_status) back to a different terminal state.
            return
        result.attempts = job.attempt
        result.finished_at = time.time()
        if outcome is not None:
            result.status = JobStatus.SUCCESS
            if isinstance(outcome, Reading):
                result.reading = outcome
            else:
                result.write_result = outcome
        else:
            result.status = status or JobStatus.ERROR
            result.error = error
        if op.done_count >= op.total:
            op.running = False
            op.finished_at = time.time()
            op.done_event.set()
