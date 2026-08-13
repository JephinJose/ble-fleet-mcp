# Tool reference

All eight tools are implemented in [`fleet_mcp/server/tools.py`](../src/fleet_mcp/server/tools.py)
and registered in [`fleet_mcp/server/app.py`](../src/fleet_mcp/server/app.py). Types
below are the JSON Schema types the MCP client sees; `?` marks an optional field.

---

## `fleet_register`

Register a device or group into a named fleet — either an explicit address list, or a
discovery filter (name pattern / service UUIDs) that's resolved immediately.

**Input**

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | `string` | — | Fleet name. Registering into an existing name merges devices in. |
| `addresses` | `string[]?` | `null` | Explicit device addresses. If given, no discovery scan runs. |
| `name_pattern` | `string?` | `null` | Substring match against advertised device name. Ignored if `addresses` is set. |
| `service_uuids` | `string[]?` | `null` | Only devices advertising at least one of these service UUIDs. Ignored if `addresses` is set. |
| `risk_tier` | `string` | `"read_only"` | One of `read_only`, `low_risk_actuator`, `safety_critical`. |
| `scan_timeout_s` | `number` | `10.0` | Discovery scan timeout, only used when scanning. |

**Output**

```json
{
  "fleet": "warehouse",
  "device_count": 40,
  "addresses": ["AA:BB:CC:DD:EE:01", "..."]
}
```

---

## `fleet_scan`

Discover devices matching a filter, without connecting to or registering them.

**Input**

| Field | Type | Default |
| --- | --- | --- |
| `name_pattern` | `string?` | `null` |
| `service_uuids` | `string[]?` | `null` |
| `addresses` | `string[]?` | `null` |
| `timeout_s` | `number` | `10.0` |

**Output**

```json
{
  "devices": [
    {"address": "AA:BB:...:01", "name": "sensor-01", "rssi": -58, "transport": "ble"}
  ]
}
```

---

## `fleet_read`

Read one resource across every device in a fleet, or a named subset.

**Input**

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `fleet` | `string` | — | Must already be registered via `fleet_register`. |
| `resource` | `string` | — | Transport-defined (GATT characteristic UUID for BLE). |
| `addresses` | `string[]?` | `null` (whole fleet) | Restrict to a subset of the fleet. |
| `priority` | `string` | `"normal"` | `high`, `normal`, or `low`. |
| `timeout_s` | `number?` | `FLEET_OPERATION_TIMEOUT_S` | Overall batch timeout. |

**Output** — an operation snapshot (see [Operation snapshot shape](#operation-snapshot-shape)
below). Returns after a short internal grace period (~1.5s); if every device
answered in that window the snapshot is already `completed`, otherwise poll
`fleet_operation_status` with the returned `operation_id`.

---

## `fleet_write`

Write one resource across a fleet. **Requires `FLEET_ALLOW_WRITES=1`** on the server;
otherwise every call raises an error.

**Input**

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `fleet` | `string` | — | |
| `resource` | `string` | — | |
| `value` | `any` | — | Transport-defined encoding (BLE: bytes, bool, int, or str — see [`transports/ble/transport.py`](../src/fleet_mcp/transports/ble/transport.py)). |
| `addresses` | `string[]?` | `null` (whole fleet) | |
| `confirm_addresses` | `string[]?` | `null` | Addresses of `safety_critical` devices you explicitly want included in this write. |
| `priority` | `string` | `"normal"` | |
| `timeout_s` | `number?` | `FLEET_OPERATION_TIMEOUT_S` | |

**Output** — an operation snapshot. Each successful result's `write` field is:

```json
{"acknowledged": true, "converged": true, "readback_value": 80}
```

A `safety_critical` device not listed in `confirm_addresses` gets
`"status": "confirmation_required"` instead of being silently written or silently
dropped, and does **not** count against the operation's connection/retry budget.

---

## `fleet_watch`

Subscribe to, unsubscribe from, or poll buffered notifications for a resource across
a fleet.

**Input**

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `fleet` | `string` | — | |
| `resource` | `string` | — | |
| `action` | `string` | `"subscribe"` | `subscribe`, `unsubscribe`, or `poll`. |
| `addresses` | `string[]?` | `null` (whole fleet) | |
| `debounce_s` | `number` | `0.5` | Minimum interval between buffered notifications per device, only used on `subscribe`. |

**Output** (`action="subscribe"`)

```json
{"action": "subscribe", "subscribed": ["D0", "D1"], "errors": {"D2": "cannot watch D2: ..."}}
```

Watching a device pins one connection-pool slot for as long as it's watched;
requesting more concurrently-watched devices than `FLEET_MAX_CONNECTIONS` reports
that device in `errors` rather than starving other fleet operations.

**Output** (`action="poll"`)

```json
{"action": "poll", "results": {"D0": [{"value": 22.1, "at": 1732400000.1}]}}
```

**Output** (`action="unsubscribe"`)

```json
{"action": "unsubscribe", "addresses": ["D0"]}
```

---

## `fleet_status`

Per-device health for a fleet.

**Input**: `fleet: string`

**Output**

```json
{
  "fleet": "warehouse",
  "device_count": 40,
  "devices": {
    "AA:BB:...:01": {
      "health": "healthy",
      "consecutive_failures": 0,
      "consecutive_successes": 12,
      "last_failure_reason": null,
      "connected": false
    }
  }
}
```

`health` is `"healthy"` or `"unhealthy"` (circuit breaker open).

---

## `fleet_pool_status`

Connection pool telemetry — the live debugging surface for the pool.

**Input**: none

**Output**

```json
{
  "max_connections": 4,
  "active_connections": 1,
  "idle_connections": 3,
  "evicting_connections": 0,
  "queue_depth": 0,
  "per_device_wait_s": {},
  "watched_devices": 0
}
```

---

## `fleet_operation_status`

Poll a `fleet_read`/`fleet_write` batch by `operation_id`.

**Input**: `operation_id: string`

**Output**: an operation snapshot (below). Raises an error if `operation_id` is
unknown (never returns silently-empty results for a bad ID).

---

## Operation snapshot shape

Returned by `fleet_read`, `fleet_write`, and `fleet_operation_status`:

```json
{
  "operation_id": "3393bca3575d484183f62588ff999c14",
  "status": "completed",
  "total": 40,
  "completed": 40,
  "created_at": 1732400000.1,
  "finished_at": 1732400001.6,
  "results": {
    "AA:BB:...:01": {
      "resource": "temperature_c",
      "kind": "read",
      "status": "success",
      "attempts": 1,
      "error": null,
      "value": 21.4,
      "write": null
    },
    "AA:BB:...:02": {
      "resource": "temperature_c",
      "kind": "read",
      "status": "unreachable",
      "attempts": 3,
      "error": "AA:BB:...:02: not found",
      "value": null,
      "write": null
    }
  }
}
```

`status` (top level): `running`, `completed`, or `timed_out` (operation-level timeout
hit — `results` still reflects everything that did resolve, and any leftover entries
are force-resolved to `"status": "timeout"`).

`results[address].status` (per-device): `still_queued`, `success`, `unreachable`,
`timeout`, `write_rejected`, `error`, or (write-only) `confirmation_required`.
