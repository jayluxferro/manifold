# Architecture

## System Overview

Manifold is a **control plane** with a **thin entry proxy**. It does NOT process, inspect, or modify HTTP request/response bodies. It manages the topology of a linear chain of LLM proxy services and provides a single entry point.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              MANIFOLD                                       │
│                                                                             │
│  ┌───────────┐    ┌───────────────────────────────────────────────────┐     │
│  │  Gateway   │    │              Control Plane                       │     │
│  │  (:9000)   │    │  ┌─────────┐ ┌─────────┐ ┌────────┐ ┌───────┐  │     │
│  │            │    │  │ Config  │ │ Chain   │ │ Health │ │ Stats │  │     │
│  │ httpx fwd  │    │  │ Loader  │ │ Wirer  │ │ Monitor│ │ Aggr. │  │     │
│  │ to first   │    │  └─────────┘ └─────────┘ └────────┘ └───────┘  │     │
│  │ healthy    │    │  ┌──────────────────────────────────────────┐    │     │
│  │ service    │    │  │         Process Manager                  │    │     │
│  └─────┬──────┘    │  │  start / stop / restart / monitor PIDs   │    │     │
│        │           │  └──────────────────────────────────────────┘    │     │
│        │           └───────────────────────────────────────────────────┘     │
└────────┼────────────────────────────────────────────────────────────────────┘
         │
         ▼
   ┌───────────┐        ┌───────────┐        ┌───────────┐
   │ Service A │───────▶│ Service B │───────▶│ Service C │───────▶ Cloud API
   │ (redactor)│        │ (splitter)│        │ (hivemind)│
   │ :7789     │        │ :7788     │        │ :8765     │
   └───────────┘        └───────────┘        └───────────┘
```

## Components

### 1. Config Loader (`config.py`)

Reads `manifold.yaml` and produces typed dataclasses. Validates:
- No duplicate service names
- No duplicate ports
- All referenced directories exist
- Each service has either `upstream_key` (config file patching) or `upstream_via: cli_arg` (command template)
- At least one service is enabled

Config resolution order:
1. `--config` CLI flag
2. `$MANIFOLD_CONFIG` env var
3. `./manifold.yaml` in working directory

### 2. Chain Wirer (`chain.py`)

Computes the upstream URL for each service based on pipeline order, then applies it.

**Algorithm:**
```
enabled_services = [s for s in pipeline if s.enabled]
for i, service in enumerate(enabled_services):
    if i < len(enabled_services) - 1:
        next_service = enabled_services[i + 1]
        service.resolved_upstream = f"http://127.0.0.1:{next_service.port}"
    else:
        # Last service — don't touch its upstream
        service.resolved_upstream = None  # leave as-is
```

**Two upstream strategies:**

1. **Config file patching** (`upstream_key` is set):
   - Read the service's YAML config file
   - Navigate the dot-path (e.g., `models.cloud.endpoint`)
   - Set the value to the computed upstream URL
   - Write the file back
   - Important: preserve YAML formatting/comments where possible (use `ruamel.yaml` or careful write)

2. **CLI argument** (`upstream_via: cli_arg`):
   - Template `{upstream}` in the service's `command` string with the computed upstream URL
   - No file modification needed

**Bypass rewiring:**
When health monitor marks a service unhealthy:
```
Before: A → B → C → Cloud
B goes down:
After:  A → C → Cloud
```
- If B used config file patching: rewrite A's config to point to C's port, then signal A to reload (or restart A)
- If B used CLI arg: restart A with updated command
- If the first service goes down: gateway forwards directly to the second service
- If all services go down: gateway needs a fallback (the last service's original upstream)

### 3. Gateway (`gateway.py`)

A Starlette ASGI app with httpx async client. Two responsibilities:

**Proxy handler** (`/` catch-all):
- Receives any request from the agent
- Forwards to the first healthy service in the pipeline using `httpx.AsyncClient.send()`
- Streams the response back (critical: must handle SSE/chunked transfer encoding)
- Adds `x-manifold-pipeline` response header listing which services are active

**Management endpoints:**
- `/_manifold/health` — GET, returns health state of all services
- `/_manifold/stats` — GET, aggregated stats from all services
- `/_manifold/config` — GET, current pipeline topology

**Streaming implementation:**
```python
async def proxy_request(request: Request) -> StreamingResponse:
    target_url = f"http://127.0.0.1:{first_service_port}{request.url.path}"
    
    # Build upstream request
    req = client.build_request(
        method=request.method,
        url=target_url,
        headers=filter_hop_by_hop(request.headers),
        content=request.stream(),
        params=request.query_params,
    )
    
    # Stream response
    upstream_resp = await client.send(req, stream=True)
    return StreamingResponse(
        upstream_resp.aiter_raw(),
        status_code=upstream_resp.status_code,
        headers=dict(upstream_resp.headers),
    )
```

### 4. Process Manager (`process.py`)

Manages service lifecycles using `asyncio.create_subprocess_exec`.

**Per-service state:**
```python
@dataclass
class ServiceProcess:
    name: str
    process: asyncio.subprocess.Process | None
    pid: int | None
    status: Literal["stopped", "starting", "running", "unhealthy", "crashed"]
    restart_count: int
    last_started: datetime | None
    last_crashed: datetime | None
```

**Startup sequence:**
1. Chain wirer patches all configs / resolves commands
2. Start services in pipeline order (first to last)
3. Wait for each service's health endpoint to respond (with timeout)
4. Start the gateway last

**Shutdown sequence:**
1. Stop the gateway (stop accepting new requests)
2. Stop services in reverse pipeline order (last to first)
3. Send SIGTERM, wait 5s, then SIGKILL if still alive

**Crash recovery:**
- Monitor each subprocess for unexpected exit
- On crash: mark unhealthy, trigger chain bypass, attempt restart (max 3 retries with backoff)
- Log stderr output for debugging

### 5. Health Monitor (`health.py`)

Background asyncio task that runs every 5 seconds.

**Per-service health check:**
```python
async def check_service(service: ServiceConfig) -> bool:
    try:
        resp = await client.get(
            f"http://127.0.0.1:{service.port}{service.health}",
            timeout=3.0,
        )
        return resp.status_code < 500
    except (httpx.ConnectError, httpx.TimeoutException):
        return False
```

**State machine per service:**
```
running ──(3 consecutive failures)──▶ unhealthy ──(1 success)──▶ running
                                          │
                                     trigger chain
                                       bypass
```

3 consecutive failures before marking unhealthy (avoid flapping). 1 success to mark healthy again (fast recovery).

On state change:
- `running → unhealthy`: trigger chain bypass rewire
- `unhealthy → running`: trigger chain restore rewire

### 6. Stats Aggregator (`stats.py`)

Hits each service's stats endpoint concurrently (asyncio.gather), merges into:

```json
{
  "manifold": {
    "uptime_seconds": 3600,
    "total_requests_proxied": 1234,
    "active_services": 3,
    "pipeline": ["llm-redactor", "local-splitter", "hivemind"]
  },
  "services": {
    "llm-redactor": { /* raw response from /v1/redactor/stats */ },
    "local-splitter": { /* raw response from /v1/splitter/stats */ },
    "hivemind": { /* raw response from /_stats */ }
  }
}
```

No schema normalization — each service's stats are included as-is.

### 7. Service Registry & Leases (`registry.py`)

Manifold runs a small filesystem registry so multiple gateways can **share**
one set of service processes instead of each spawning a full duplicate chain.
State lives under `~/.manifold/run/` (derived lazily at call time from
`paths.PID_DIR`, so tests keep patching `manifold.paths.PID_DIR`):

```
~/.manifold/run/
  services/<identity>.json     # one per running service; exactly one owning gateway
  leases/gateway-<port>.json   # one per running gateway; identities it uses
  locks/<identity>.lock        # O_EXCL spawn lock; content = locking pid (debug)
```

**Identity computation.** A service entry is keyed by a canonical identity:
`sha256(json.dumps([name, directory, port, upstream_url,
resolve_command(svc, upstream_url)]))` with compact separators. `upstream_url`
includes `upstream_path` (already applied by `compute_upstreams`); health and
stats paths are deliberately excluded. Two gateways share a service iff their
computed identities are byte-identical.

**Entry / lease shape.**
- Service entry: `{schema_version, identity, name, directory, command
  (resolved), port, upstream, pid, pgid, owner_port, owner_pid, started_at}`.
- Lease: `{schema_version, gateway_port, gateway_pid, isolated, identities[],
  updated_at}`.

**Ownership & lease semantics** (invariants I1–I7, see SPEC-shared-pipeline.md):
- **I1 single writer** — only the entry's `owner_port` gateway may
  spawn/kill/restart/patch a service. Adopters are read-only: they never
  spawn, patch, forward logs, killpg, or restart.
- **I3 kill authority** — `owner_port`/`owner_pid` authorize kills; a lease
  identity list alone never authorizes killing a service another live lease
  still lists.
- **I4 verified kill** — killpg only when `os.getpgid(entry["pid"]) ==
  entry["pgid"]` (guards pid/pgid reuse); on mismatch the entry is removed but
  the process is left alone.
- **I6 sharing boundary** — adoption only on exact identity match.
- **I7 stop on last leave** — services stop when their last leasing gateway
  leaves; no warm pool.

**Adoption flow.** On `manifold up` (shared mode), each service is planned as
ADOPT / PROMOTE / SPAWN / ERROR. A live entry with a live owner → **adopt**
(reuse the process, set `adopted=True`, record `owner_port`); live entry with
dead owner → **promote** (kill the orphaned process set per I4, then spawn as
owner); no entry → **spawn** (port must be free in shared mode, else a hard
error naming the conflicting identity). Spawns serialize on an O_EXCL
`locks/<identity>.lock`; a concurrent up waiting on the lock re-adopts if an
entry appears. The gateway writes its **lease before the first spawn** so a
`down` racing `up` sees the intended ownership, then refreshes it after the
plan loop. `--isolated` skips adoption entirely: it spawns a full
delta-offset duplicate chain (old behavior), still registry-tracked.

**Teardown.** `manifold down --port <port>` snapshots the lease and legacy pid
file first, signals the gateway (SIGTERM → poll → SIGKILL), then reaps
directly from the registry: each snapshotted identity is either transferred to
a surviving live lease holder (handoff, no kill) or killed via
`kill_entry_processes`; an owner-port sweep catches crash-mid-startup orphans.
Finally it removes the lease, pid file, and port file. Pre-registry instances
(pid file only, no lease) fall back to the legacy lsof port scan inside
`manifold down` (needs `--config`). `down --all` runs this over every
discovered port, then `sweep_stale()` cleans dead entries, orphaned services,
and dead leases. The health monitor additionally fires an
`on_adopted_unhealthy` callback when an adopted service flips
HEALTHY→UNHEALTHY so the owning gateway can be prompted to recover it — an
adopter never touches the process itself.

## Data Flow

### Normal Operation
```
1. Agent sends POST /v1/chat/completions to gateway :9000
2. Gateway forwards to first service (redactor :7789)
3. Redactor scrubs PII, forwards to its upstream (splitter :7788)
4. Splitter compresses context, forwards to its upstream (hivemind :8765)
5. HiveMind applies admission/rate limiting, forwards to cloud API
6. Cloud responds
7. HiveMind tracks tokens, passes response back
8. Splitter caches response, passes back
9. Redactor restores PII placeholders, passes back
10. Gateway streams response to agent
```

### Service Failure
```
1. Health monitor detects splitter :7788 is down (3 consecutive failures)
2. Health monitor calls chain.bypass("local-splitter")
3. Chain wirer rewrites redactor's upstream from :7788 to :8765
4. Chain wirer restarts redactor (or signals reload if supported)
5. Traffic now flows: Agent → Redactor → HiveMind → Cloud (splitter skipped)
6. Health monitor detects splitter recovers
7. Chain wirer restores original topology
```

## Error Handling

- **Gateway can't reach first service**: Return 502 with JSON error body
- **Service crashes during request**: Client sees connection reset (unavoidable); health monitor will bypass for next request
- **Config file parse error**: Fail fast on startup with clear error message
- **Port conflict**: Detect on startup, fail with which ports conflict

## Security

- All services bind to `127.0.0.1` only — no external exposure
- Manifold doesn't handle auth — each service manages its own API key forwarding
- Config files may contain paths but never secrets (API keys come from env vars in each service)
