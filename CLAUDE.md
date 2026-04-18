# Manifold

> Proxy mesh gateway that chains multiple LLM proxy services into a single transparent pipeline.

## Project Location

`/Users/jay/dev/ml/mcp/manifold/`

## What This Project Does

Manifold is a **control plane + thin entry proxy** that wires multiple LLM proxy services (each with OpenAI/Anthropic-compatible APIs) into a linear chain. An agent points at one port and its requests flow transparently through every service before reaching the cloud API.

**It is NOT another fat proxy.** Each service in the pipeline already does its own proxying. Manifold just:
1. Manages the topology (sets each service's upstream to the next service)
2. Starts/stops/monitors all services
3. Provides a single entry point for agents
4. Handles health checks and bypass when a service is down
5. Aggregates stats from all services

## The Problem It Solves

We have multiple LLM proxy tools (llm-redactor, local-splitter, hivemind, and more coming) that each spin their own HTTP server with OpenAI + Anthropic compatible endpoints. Without manifold, you either:
- Use MCP for all of them (100+ tool definitions in context, agent must reason about ordering = slow + error-prone)
- Manually configure each service's upstream to point to the next (fragile, manual, no health checks)

With manifold: `export ANTHROPIC_BASE_URL=http://127.0.0.1:9000` — done.

## Known Sibling Services

These are the services manifold will chain. They all live in `/Users/jay/dev/ml/mcp/`:

### llm-redactor (port 7789)
- **Function**: Scrubs PII/secrets from requests before cloud, restores placeholders in responses
- **Framework**: FastAPI/Uvicorn
- **Endpoints**: `/v1/chat/completions`, `/v1/messages`, `/v1/redactor/stats`, `/v1/redactor/config`
- **Health**: `/v1/redactor/config` (GET)
- **Stats**: `/v1/redactor/stats` (GET)
- **Config file**: `llm_redactor.yaml`
- **Upstream config key**: `cloud_target.endpoint` (YAML path)
- **Start command**: `uv run llm-redactor serve --port {port}`

### local-splitter (port 7788)
- **Function**: Routes trivial requests to local model, compresses context, caches responses
- **Framework**: FastAPI/Uvicorn
- **Endpoints**: `/v1/chat/completions`, `/v1/messages`, `/v1/models`, `/v1/splitter/stats`, `/healthz`
- **Health**: `/healthz` (GET)
- **Stats**: `/v1/splitter/stats` (GET)
- **Config file**: `config.yaml` (or via `--config` flag)
- **Upstream config key**: `models.cloud.endpoint` (YAML path)
- **Start command**: `uv run local-splitter serve-http --config config.yaml --port {port}`

### hivemind (port 8765)
- **Function**: Admission control, rate limiting, AIMD backpressure, token budgets, priority queue
- **Framework**: Starlette/Uvicorn
- **Endpoints**: `/{path:path}` (catch-all proxy), `/_health`, `/_stats`
- **Health**: `/_health` (GET)
- **Stats**: `/_stats` (GET)
- **Upstream config**: `--upstream` CLI flag
- **Start command**: `uv run hivemind proxy --port {port} --upstream {upstream_url}`

## Architecture Overview

```
Agent → Gateway (:9000) → Service A → Service B → Service C → Cloud API
```

The gateway:
1. Reads `manifold.yaml` to learn about all services and their pipeline order
2. Patches each service's config so its upstream points to the next service in the chain
3. Starts all services (subprocess management)
4. Reverse-proxies agent requests to the first service
5. Monitors health of all services; if one is down, rewires the chain to bypass it
6. Exposes aggregated stats from all services on `/_manifold/stats`

### Chain Wiring Logic
- Service N's upstream = `http://127.0.0.1:{service_N+1_port}`
- Last service's upstream = its own configured upstream (e.g., `https://api.anthropic.com`) — manifold does NOT touch it
- If service N goes down, service N-1's upstream is temporarily rewired to service N+1

### Request/Response Flow
```
REQUEST:  Agent → Redactor(scrub) → Splitter(compress/route) → HiveMind(schedule/throttle) → Cloud
RESPONSE: Cloud → HiveMind(track tokens) → Splitter(cache) → Redactor(restore) → Agent
```

## Tech Stack
- Python 3.12+
- `uv` for package management (consistent with sibling projects)
- `httpx` for async reverse proxy
- `asyncio` for subprocess management
- `pyyaml` for config
- `typer` for CLI
- No heavy frameworks — this is a thin control plane, not a fat proxy

## Config Schema (manifold.yaml)

```yaml
gateway:
  host: 127.0.0.1
  port: 9000

pipeline:   # order matters — first service receives agent requests
  - name: llm-redactor
    directory: /Users/jay/dev/ml/mcp/llm-redactor
    command: "uv run llm-redactor serve --port {port}"
    port: 7789
    health: /v1/redactor/config
    stats: /v1/redactor/stats
    config_file: llm_redactor.yaml        # relative to directory
    upstream_key: cloud_target.endpoint    # dot-path into YAML config to patch
    enabled: true

  - name: local-splitter
    directory: /Users/jay/dev/ml/mcp/local-splitter
    command: "uv run local-splitter serve-http --config config.yaml --port {port}"
    port: 7788
    health: /healthz
    stats: /v1/splitter/stats
    config_file: config.yaml
    upstream_key: models.cloud.endpoint
    enabled: true

  - name: hivemind
    directory: /Users/jay/dev/ml/mcp/hivemind
    command: "uv run hivemind proxy --port {port} --upstream {upstream}"
    port: 8765
    health: /_health
    stats: /_stats
    upstream_key: null                     # uses {upstream} in command template instead
    upstream_via: cli_arg                  # "config_file" or "cli_arg"
    enabled: true
```

## Implementation Modules

```
src/manifold/
├── __init__.py
├── cli.py              # Typer CLI: start, stop, status, stats
├── config.py           # Load + validate manifold.yaml
├── gateway.py          # Thin httpx reverse proxy to first service
├── chain.py            # Chain wiring logic: patch configs, compute upstreams
├── health.py           # Periodic health checks, bypass/rewire on failure
├── process.py          # Start/stop/restart service subprocesses
├── stats.py            # Aggregate stats from all services
└── models.py           # Dataclasses: ServiceConfig, GatewayConfig, PipelineState
```

## Build Instructions

### Phase 1: Core (MVP)
1. `config.py` — Load and validate `manifold.yaml`, resolve service configs
2. `models.py` — Dataclasses for all config types
3. `chain.py` — Chain wiring: compute upstream URLs, patch service YAML configs (deep dot-path set), handle `upstream_via: cli_arg` by templating `{upstream}` in command strings
4. `gateway.py` — Async httpx reverse proxy: receive request on `:9000`, forward to first service, stream response back. Must support SSE streaming (critical for LLM responses)
5. `process.py` — Start services as subprocesses (`asyncio.create_subprocess_exec`), capture stdout/stderr, track PIDs, graceful shutdown
6. `cli.py` — `manifold up` (start all + gateway), `manifold down` (stop all), `manifold status` (show service states)

### Phase 2: Resilience
7. `health.py` — Background task that pings each service's health endpoint every 5s. On failure: mark service unhealthy, rewire chain to bypass it. On recovery: rewire back in
8. Chain bypass logic in `chain.py` — When a service is bypassed, update the previous service's upstream to skip it
9. Graceful degradation — If ALL services are down, gateway forwards directly to cloud API (needs a fallback upstream in config)

### Phase 3: Observability
10. `stats.py` — Hit each service's stats endpoint, merge into unified JSON response
11. `/_manifold/stats` endpoint on the gateway
12. `/_manifold/health` endpoint showing all service states
13. `/_manifold/config` endpoint showing current pipeline topology

### Phase 4: Nice-to-haves
14. `manifold add <name>` — Interactive service registration
15. Hot-reload on `manifold.yaml` changes (watchdog)
16. MCP server that exposes `manifold.status`, `manifold.stats`, `manifold.enable/disable` tools
17. Log aggregation from all service subprocesses

## Key Design Decisions
- **No request modification** — Manifold never inspects or modifies request/response bodies. It's a topology manager + entry proxy only.
- **Config patching is file-based** — Manifold writes to each service's YAML config file before starting it. This is simple and works with all services' existing config loading.
- **Streaming is mandatory** — The gateway proxy MUST support SSE streaming pass-through since all LLM responses stream.
- **Process management is simple** — Just subprocesses with PID tracking. No containers, no systemd. Keep it simple.
- **Each service owns its own port** — No port multiplexing. Each service gets its own port as defined in the config.

## Testing
- Unit tests for config loading, chain wiring, upstream computation
- Integration tests that start mock services and verify chain forwarding
- Use `pytest` + `pytest-asyncio` + `httpx` for async testing
- Mock services can be simple FastAPI apps that echo requests with a service identifier header

## Dependencies
- `httpx` — async HTTP client for proxying and health checks
- `pyyaml` — config file loading and patching
- `typer` — CLI framework
- `uvicorn` — ASGI server for the gateway
- `starlette` — lightweight ASGI framework for the gateway app (lighter than FastAPI, we don't need OpenAPI docs)
