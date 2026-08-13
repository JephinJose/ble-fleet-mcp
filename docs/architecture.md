# Architecture

```
                    ┌─────────────────────────┐
   Agent  ──MCP──▶  │      Tool Layer          │   fleet_register, fleet_scan,
                    │  (fleet_mcp.server)      │   fleet_read, fleet_write,
                    └───────────┬─────────────┘   fleet_watch, fleet_status,
                                │                  fleet_pool_status,
                                │                  fleet_operation_status
                    ┌───────────▼─────────────┐
                    │   Scheduler / Queue      │   priority queue, per-device
                    │  (fleet_mcp.core.        │   backoff, batching,
                    │   scheduler)             │   operation timeout
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │  Connection Pool Manager │   hard concurrency cap,
                    │  (fleet_mcp.core.pool)   │   LRU eviction, connection state
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │   Transport Plugin(s)    │   BLE (bleak) in v1,
                    │  (fleet_mcp.transports)  │   Zigbee/Thread later
                    └───────────┬─────────────┘
                                │
                          Physical devices
```

Everything above the transport layer — pool manager, scheduler, circuit breaker,
fleet registry, MCP tools — depends only on the `Transport` protocol in
[`fleet_mcp.core.types`](../src/fleet_mcp/core/types.py). None of it imports `bleak`
or anything else BLE-specific. That's what makes a second transport a plugin rather
than a rewrite (see [CONTRIBUTING.md](../CONTRIBUTING.md)).

## Connection pool manager

[`fleet_mcp/core/pool.py`](../src/fleet_mcp/core/pool.py)

`ConnectionPoolManager` owns at most `max_connections` live transport connections at
any time (`FLEET_MAX_CONNECTIONS`, defaulting to the transport's own
`max_concurrent_connections()`). Callers borrow a connection via
`pool.acquire(device)`, an async context manager:

1. If the device already has a connection, reuse it (marked `ACTIVE` for the
   duration of the `with` block).
2. Otherwise, if the pool has room, connect.
3. Otherwise, evict the least-recently-used **idle** connection and connect in its
   place. An `ACTIVE` (in-flight) connection is never evicted — eviction only ever
   considers `IDLE` connections.
4. If the pool is full and every connection is `ACTIVE`, the caller waits until one
   is released.

Connection states: `idle` → `active` (borrowed) → back to `idle` on release, or
`evicting` while an idle connection is being torn down to make room.

### Why connect() runs outside the lock

The pool's internal bookkeeping (`_connections`, a reserved-slot counter) is only
ever mutated while holding a lock — but the actual `await transport.connect(...)` I/O
call always happens with that lock **released**. A slot is *reserved* under the lock,
the slow I/O happens without it, then the result is recorded back under the lock.

This mattered in practice: an earlier version held the lock across the `connect()`
call, which meant one device that was slow (or hung) to connect blocked *every other
device's* acquisition attempt too — not just its own. `tests/unit/test_scheduler.py::
test_slow_device_does_not_stall_the_rest` exists specifically to catch a regression
here; the fake transport's `hang=True` peripheral hangs inside `connect()`, not just
inside a read.

## Scheduler / priority queue

[`fleet_mcp/core/scheduler.py`](../src/fleet_mcp/core/scheduler.py)

Every `fleet_read`/`fleet_write` call decomposes into one `Job` per target device,
all sharing an `op_id`. A fixed pool of worker coroutines (sized to
`pool.max_connections`) continuously pick the next ready job:

- **Priority** (`high` / `normal` / `low`) is the primary sort key — a high-priority
  safety read submitted mid-batch jumps ahead of whatever low-priority jobs are still
  queued.
- **Batching**: among jobs of equal priority, one whose device is *already connected*
  in the pool is preferred over one that would require a new connection (and
  possibly an eviction). This drains the currently-connected working set before
  churning the pool.
- **Backoff**: a failed job (unreachable/timeout) is re-queued with exponential
  backoff (`FLEET_BACKOFF_INITIAL_S` × `FLEET_BACKOFF_MULTIPLIER` ^ attempt, capped
  at `FLEET_BACKOFF_MAX_S`) rather than retried immediately.
- **Per-device timeout** (`FLEET_DEVICE_TIMEOUT_S`) wraps the *entire* acquire+operate
  sequence for a job, including connection setup — not just the read/write call —
  so a device that hangs on connect can't pin a pool slot indefinitely.
- **Operation timeout** (`FLEET_OPERATION_TIMEOUT_S`, overridable per call): once it
  elapses, any job still `still_queued` for that operation is force-resolved to a
  `timeout` result and the batch returns as `timed_out` with whatever completed —
  partial results, never a hung call.

Every job resolves to exactly one terminal state: `success`, `unreachable`,
`timeout`, `write_rejected`, or `error` — or remains `still_queued` while the
operation is still running. `fleet_operation_status` polls a `FleetOperation` by ID
for this snapshot at any point.

### Writes: verify-after-write

A `write` job isn't done when the transport acknowledges it — the scheduler reads the
resource back afterward and reports `converged: bool` (whether the readback matches
what was requested) alongside `acknowledged`. This mirrors the write/verify pattern
from the IETF CoAP-agent draft, and it's the same reason `fleet_write` treats
`WriteRejected` as non-retryable: a rejected write is usually a deterministic
mismatch (bad value, wrong permissions), not something backoff fixes.

## Circuit breaker / health tracking

[`fleet_mcp/core/circuit_breaker.py`](../src/fleet_mcp/core/circuit_breaker.py)

Tracks consecutive failures per device address. After
`FLEET_CIRCUIT_BREAKER_THRESHOLD` consecutive failures, a device is marked
`unhealthy` and the scheduler stops scheduling jobs against it until
`FLEET_CIRCUIT_BREAKER_COOLDOWN_S` has elapsed, at which point a single probe attempt
is allowed through (half-open). A success at any point resets the consecutive-failure
counter to zero.

This is deliberately per-device and independent of the pool/scheduler's retry logic:
retries-with-backoff handle a single job's failures; the circuit breaker handles a
device that's *chronically* bad across many operations, so the scheduler stops
wasting connection slots and retry budget on it. `fleet_status` surfaces this
directly (`health`, `consecutive_failures`, `last_failure_reason`) so the agent (or a
human) can tell "device X has been down for 3 attempts" instead of just seeing
another timeout.

## fleet_watch: subscriptions vs. the pool's lease model

[`fleet_mcp/core/watch.py`](../src/fleet_mcp/core/watch.py)

Read/write jobs borrow a connection and release it. A subscription is different — it
needs to stay connected indefinitely to keep receiving notifications, which is at
odds with the pool's LRU-idle eviction (an idle-but-subscribed connection would look
evictable to the pool if `WatchManager` didn't hold it open explicitly).

`WatchManager` handles this by acquiring a connection and **never releasing it**
(never calling `__aexit__`) for as long as a device is watched — pinning that slot.
Because this competes directly with `fleet_read`/`fleet_write` for the same capped
pool, starting a new watch that would exceed `pool.max_connections` watched devices
is refused outright (`WatchCapacityExceeded`, reported to the caller) rather than
silently starving other fleet operations of connections.

Notifications are buffered per `(address, resource)` and debounced (only buffered if
at least `debounce_s` has elapsed since the last buffered entry); `fleet_watch(action=
"poll")` drains the buffer. This mirrors the `watch_resource` pattern from the IETF
CoAP-agent draft without needing a persistent streaming channel back to the agent.

## Fleet registry

[`fleet_mcp/core/fleet_registry.py`](../src/fleet_mcp/core/fleet_registry.py)

A plain in-memory mapping of fleet name → `{address: DeviceHandle}`. `fleet_register`
either builds `DeviceHandle`s directly from an explicit address list (works even if
the device is offline right now) or runs `transport.discover()` against a name
pattern / service UUID filter. Registering into an existing fleet name merges devices
in rather than replacing the fleet.

## Observability

Every pool and scheduler event of note (`pool.connect`, `pool.evict`, `pool.wait`,
`scheduler.submit`, `scheduler.job_success`, `scheduler.job_failure`,
`scheduler.operation_timeout`, `watch.subscribe`, `watch.unsubscribe`) is emitted as
one JSON object per line to `FLEET_TRACE_PATH` (default
`.fleet_mcp/traces/trace.jsonl`) via [`fleet_mcp/core/tracing.py`](../src/fleet_mcp/core/tracing.py).
It's on by default and never touches stdout — set `FLEET_TRACE_ENABLED=0` to disable.
`fleet_pool_status` and `fleet_status` expose the same underlying state synchronously,
as the live-debugging surface for a human (or the agent) without needing to tail the
trace file.

## Web dashboard

[`fleet_mcp/server/dashboard.py`](../src/fleet_mcp/server/dashboard.py)

An opt-in (`FLEET_DASHBOARD_PORT`) read-only view over the exact same state
`fleet_pool_status`/`fleet_status` expose, for a human watching a running server
without going through an MCP client. Deliberately minimal:

- Plain stdlib `http.server.ThreadingHTTPServer` running in a daemon thread — no new
  required dependency, no ASGI framework. `GET /` serves a single self-contained HTML
  page (inline CSS/JS, no CDN calls); `GET /api/status` serves the JSON snapshot the
  page polls once a second.
- Reads directly from the same `AppContext` the MCP tools use (`pool.status()`,
  `registry.list()`, `scheduler.circuit_breaker.snapshot()`, `scheduler.list_operations()`,
  `watches.status()`) — there's no separate state to keep in sync, and no IPC.
- Runs on a different OS thread than the asyncio event loop the MCP stdio server and
  scheduler run on. All the status-gathering calls it uses are synchronous, read-only,
  and side-effect-free, so this is safe under CPython's GIL for a monitoring view; the
  request handler catches and reports any exception as a 500 rather than taking the
  thread down, since a rare read of in-flux state under concurrent mutation is an
  acceptable tradeoff here — this is not a control plane, and there's no write path.
- `Scheduler.list_operations()` is bounded by `max_operation_history` (default 500):
  the scheduler tracks `FleetOperation` objects (one per `fleet_read`/`fleet_write`
  call) in an insertion-ordered dict and prunes the oldest *completed* one whenever
  the count exceeds the bound, so a long-running server's operation history — which
  the dashboard's "Recent operations" panel reads from — doesn't grow forever.
