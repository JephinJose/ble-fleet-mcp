"""Minimal, opt-in web dashboard over the same telemetry fleet_pool_status/fleet_status
already expose. No new required dependency: it's a stdlib http.server instance running
in a daemon thread alongside the MCP stdio loop, reading straight from the same
in-memory AppContext the MCP tools use.

Off by default — set FLEET_DASHBOARD_PORT to enable it. Binds to localhost only
unless FLEET_DASHBOARD_HOST is overridden; there is no authentication, so don't
expose it beyond localhost without putting something in front of it.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from fleet_mcp.__about__ import __version__
from fleet_mcp.server.context import AppContext

_INDEX_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>fleet-mcp dashboard</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f7f7f8; --panel: #ffffff; --border: #e2e2e6;
    --text: #1a1a1e; --muted: #6b6b75; --accent: #3b6ff2;
    --good: #1a8a4a; --bad: #c4362e; --warn: #b5750c;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #131317; --panel: #1b1b21; --border: #2c2c34;
      --text: #eceef2; --muted: #9a9aa6; --accent: #7ea2ff;
      --good: #3ecb78; --bad: #ff6b62; --warn: #e2a53a;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 2rem; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  h1 { font-size: 1.25rem; margin: 0 0 0.25rem; }
  .sub { color: var(--muted); margin: 0 0 1.5rem; font-size: 0.85rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 1rem 1.25rem; }
  .panel h2 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); margin: 0 0 0.75rem; }
  .stat { font-size: 1.8rem; font-weight: 600; }
  .stat-row { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 0.25rem; }
  .bar { height: 8px; border-radius: 4px; background: var(--border); overflow: hidden; display: flex; margin-top: 0.5rem; }
  .bar > span { display: block; height: 100%; }
  .bar .active { background: var(--accent); }
  .bar .idle { background: var(--good); opacity: 0.5; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { text-align: left; padding: 0.4rem 0.5rem; border-bottom: 1px solid var(--border); white-space: nowrap; }
  th { color: var(--muted); font-weight: 500; font-size: 0.75rem; text-transform: uppercase; }
  .pill { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px; font-size: 0.75rem; font-weight: 600; }
  .pill.healthy, .pill.success, .pill.completed { background: color-mix(in srgb, var(--good) 20%, transparent); color: var(--good); }
  .pill.unhealthy, .pill.error, .pill.unreachable, .pill.timeout, .pill.write_rejected { background: color-mix(in srgb, var(--bad) 20%, transparent); color: var(--bad); }
  .pill.running, .pill.still_queued, .pill.timed_out { background: color-mix(in srgb, var(--warn) 20%, transparent); color: var(--warn); }
  .empty { color: var(--muted); font-style: italic; padding: 0.5rem 0; }
  .section-title { font-size: 1rem; font-weight: 600; margin: 2rem 0 0.75rem; }
  code { font-size: 0.8rem; background: var(--border); padding: 0.05rem 0.35rem; border-radius: 4px; }
</style>
</head>
<body>
  <h1>fleet-mcp dashboard</h1>
  <p class="sub" id="meta">v__VERSION__ &middot; connecting&hellip;</p>

  <div class="grid">
    <div class="panel">
      <h2>Connection pool</h2>
      <div class="stat-row"><span class="stat" id="pool-active">&mdash;</span><span class="sub" id="pool-cap"></span></div>
      <div class="bar" id="pool-bar"><span class="active" style="width:0%"></span><span class="idle" style="width:0%"></span></div>
      <p class="sub" style="margin-top:0.6rem">queue depth: <span id="pool-queue">0</span></p>
    </div>
    <div class="panel">
      <h2>Watches</h2>
      <div class="stat" id="watch-count">0</div>
      <p class="sub">devices pinned by fleet_watch</p>
    </div>
    <div class="panel">
      <h2>Fleets</h2>
      <div class="stat" id="fleet-count">0</div>
      <p class="sub" id="device-count-sub">0 devices registered</p>
    </div>
  </div>

  <div class="section-title">Devices</div>
  <div class="panel"><table id="devices-table"><thead><tr>
    <th>Fleet</th><th>Address</th><th>Name</th><th>Risk</th><th>Connected</th><th>Health</th><th>Failures</th>
  </tr></thead><tbody></tbody></table><div class="empty" id="devices-empty" style="display:none">No devices registered yet.</div></div>

  <div class="section-title">Recent operations</div>
  <div class="panel"><table id="ops-table"><thead><tr>
    <th>Operation</th><th>Status</th><th>Progress</th><th>Age</th>
  </tr></thead><tbody></tbody></table><div class="empty" id="ops-empty" style="display:none">No operations yet.</div></div>

  <div class="section-title">Active watches</div>
  <div class="panel"><table id="watches-table"><thead><tr>
    <th>Address</th><th>Resource</th><th>Buffered</th><th>Last value</th>
  </tr></thead><tbody></tbody></table><div class="empty" id="watches-empty" style="display:none">No active watches.</div></div>

<script>
function pill(text) {
  return '<span class="pill ' + text + '">' + text + '</span>';
}
function fmtAge(ts) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  return Math.round(s / 3600) + 'h ago';
}
async function refresh() {
  let data;
  try {
    const res = await fetch('/api/status');
    data = await res.json();
  } catch (e) {
    document.getElementById('meta').textContent = 'v__VERSION__ · connection lost, retrying…';
    return;
  }

  document.getElementById('meta').textContent =
    'v' + data.server_version + ' · updated ' + new Date(data.generated_at * 1000).toLocaleTimeString();

  const p = data.pool;
  document.getElementById('pool-active').textContent = p.active + ' / ' + p.max_connections;
  document.getElementById('pool-cap').textContent = p.idle + ' idle, ' + p.evicting + ' evicting';
  document.getElementById('pool-queue').textContent = p.queue_depth;
  const activePct = p.max_connections ? (100 * p.active / p.max_connections) : 0;
  const idlePct = p.max_connections ? (100 * p.idle / p.max_connections) : 0;
  const bar = document.getElementById('pool-bar').children;
  bar[0].style.width = activePct + '%';
  bar[1].style.width = idlePct + '%';

  document.getElementById('watch-count').textContent = data.watches.watched_devices;
  document.getElementById('fleet-count').textContent = data.fleets.length;
  const deviceCount = data.fleets.reduce((n, f) => n + f.devices.length, 0);
  document.getElementById('device-count-sub').textContent = deviceCount + ' devices registered';

  const devicesBody = document.querySelector('#devices-table tbody');
  devicesBody.innerHTML = '';
  let anyDevice = false;
  for (const fleet of data.fleets) {
    for (const d of fleet.devices) {
      anyDevice = true;
      const tr = document.createElement('tr');
      tr.innerHTML = '<td>' + fleet.name + '</td><td><code>' + d.address + '</code></td>' +
        '<td>' + (d.name || '') + '</td><td>' + d.risk_tier + '</td>' +
        '<td>' + (d.connected ? 'yes' : 'no') + '</td><td>' + pill(d.health) + '</td>' +
        '<td>' + d.consecutive_failures + '</td>';
      devicesBody.appendChild(tr);
    }
  }
  document.getElementById('devices-empty').style.display = anyDevice ? 'none' : 'block';

  const opsBody = document.querySelector('#ops-table tbody');
  opsBody.innerHTML = '';
  for (const op of data.operations) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td><code>' + op.operation_id.slice(0, 8) + '</code></td>' +
      '<td>' + pill(op.status) + '</td><td>' + op.completed + ' / ' + op.total + '</td>' +
      '<td>' + fmtAge(op.created_at) + '</td>';
    opsBody.appendChild(tr);
  }
  document.getElementById('ops-empty').style.display = data.operations.length ? 'none' : 'block';

  const watchesBody = document.querySelector('#watches-table tbody');
  watchesBody.innerHTML = '';
  for (const w of data.watches.watches) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td><code>' + w.address + '</code></td><td>' + w.resource + '</td>' +
      '<td>' + w.buffered + '</td><td>' + JSON.stringify(w.last_value) + '</td>';
    watchesBody.appendChild(tr);
  }
  document.getElementById('watches-empty').style.display = data.watches.watches.length ? 'none' : 'block';
}
refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
""".replace("__VERSION__", __version__)


def build_status(ctx: AppContext) -> dict[str, Any]:
    """Snapshot the live server state. Synchronous and read-only — safe to call from
    the dashboard's request-handling thread without touching the asyncio event loop."""
    pool_status = ctx.pool.status()
    health = ctx.scheduler.circuit_breaker.snapshot()

    fleets = []
    for fleet in ctx.registry.list():
        devices = []
        for address, handle in fleet.devices.items():
            h = health.get(address)
            devices.append(
                {
                    "address": address,
                    "name": handle.name,
                    "risk_tier": handle.risk_tier.value,
                    "connected": ctx.pool.is_connected(address),
                    "health": h.state.value if h else "healthy",
                    "consecutive_failures": h.consecutive_failures if h else 0,
                }
            )
        fleets.append({"name": fleet.name, "device_count": len(fleet.devices), "devices": devices})

    operations = [
        {
            "operation_id": op.op_id,
            "status": op.status,
            "total": op.total,
            "completed": op.done_count,
            "created_at": op.created_at,
        }
        for op in ctx.scheduler.list_operations()[:50]
    ]

    return {
        "server_version": __version__,
        "generated_at": time.time(),
        "pool": {
            "max_connections": pool_status.max_connections,
            "active": pool_status.active_count,
            "idle": pool_status.idle_count,
            "evicting": pool_status.evicting_count,
            "queue_depth": pool_status.queue_depth,
        },
        "watches": ctx.watches.status(),
        "fleets": fleets,
        "operations": operations,
    }


def _make_handler(ctx: AppContext) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"fleet-mcp-dashboard/{__version__}"

        def log_message(self, fmt: str, *args: Any) -> None:
            pass  # keep stdout/stderr clean; use FLEET_LOG_LEVEL=DEBUG for real logs

        def do_GET(self) -> None:
            try:
                if self.path == "/" or self.path.startswith("/?"):
                    body = _INDEX_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/api/status":
                    body = json.dumps(build_status(ctx), default=str).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)
            except Exception as exc:
                self.send_error(500, str(exc))

    return Handler


def start_dashboard(ctx: AppContext, host: str, port: int) -> ThreadingHTTPServer:
    """Starts the dashboard HTTP server in a daemon thread and returns it immediately.
    Call `.shutdown()` on the returned server to stop it (mainly for tests — in normal
    operation it just dies with the process)."""
    server = ThreadingHTTPServer((host, port), _make_handler(ctx))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="fleet-mcp-dashboard")
    thread.start()
    return server
