# Manifold

> Proxy mesh gateway that chains multiple LLM proxy services into a single transparent pipeline.

## What This Project Does

Manifold is a **control plane + thin entry proxy** that wires multiple LLM proxy services (each with OpenAI/Anthropic-compatible APIs) into a linear chain. An agent points at one port and its requests flow transparently through every service before reaching the cloud API.

**It is NOT another fat proxy.** Each service in the pipeline already does its own proxying. Manifold just:
1. Manages the topology (sets each service's upstream to the next service)
2. Starts/stops/monitors all services
3. Provides a single entry point for agents
4. Handles health checks and bypass when a service is down
5. Aggregates stats from all services

## The Problem It Solves

Multiple LLM proxy tools (redactors, routers, rate limiters, etc.) each spin their own HTTP server with OpenAI + Anthropic compatible endpoints. Without manifold, you either:
- Use MCP for all of them (100+ tool definitions in context, agent must reason about ordering = slow + error-prone)
- Manually configure each service's upstream to point to the next (fragile, manual, no health checks)

With manifold: `export ANTHROPIC_BASE_URL=http://127.0.0.1:9000` — done.

## Quick Start

```bash
# Install
uv sync

# Create your pipeline config (see Config Schema below)
cp manifold.yaml.example manifold.yaml
# Edit manifold.yaml — set directory paths and enable/disable services

# Start everything
manifold up

# Point your agent at the gateway
export ANTHROPIC_BASE_URL=http://127.0.0.1:9000    # Anthropic agents
export OPENAI_API_BASE=http://127.0.0.1:9000/v1    # OpenAI agents
```

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
- Last service's upstream = the `fallback_upstream` (e.g., `https://api.anthropic.com`) — manifold does NOT touch it
- If service N goes down, service N-1's upstream is temporarily rewired to service N+1

### Example Pipeline
```
REQUEST:  Agent → Redactor(scrub PII) → Splitter(route/compress) → RateLimiter(throttle) → Cloud
RESPONSE: Cloud → RateLimiter(track tokens) → Splitter(cache) → Redactor(restore PII) → Agent
```

## Config Schema (manifold.yaml)

```yaml
gateway:
  host: 127.0.0.1
  port: 9000
  fallback_upstream: https://api.anthropic.com  # or https://api.openai.com/v1
  # Optional — after starting subprocesses, poll each enabled service's health:
  startup_health_timeout: 120        # seconds (default 120)
  startup_health_poll_interval: 0.25 # seconds between rounds (default 0.25)
  startup_health_strict: false       # if true, `manifold up` exits non-zero on timeout

pipeline:   # order matters — first service receives agent requests
  - name: my-redactor
    directory: /path/to/my-redactor         # absolute path to the service repo
    command: "uv run my-redactor serve --port {port}"
    port: 7789
    health: /healthz                        # GET endpoint that returns 2xx when ready
    stats: /stats                           # optional GET endpoint for metrics
    config_file: config.yaml                # relative to directory
    upstream_key: cloud_target.endpoint     # dot-path into YAML config to patch
    upstream_via: config_file               # "config_file" or "cli_arg"
    upstream_path: /v1                      # optional path suffix appended to upstream URL
    enabled: true

  - name: my-router
    directory: /path/to/my-router
    command: "uv run my-router serve --port {port}"
    port: 7788
    health: /healthz
    config_file: config.yaml
    upstream_key: models.cloud.endpoint
    upstream_via: config_file
    enabled: true

  - name: my-rate-limiter
    directory: /path/to/my-rate-limiter
    command: "uv run my-limiter proxy --port {port} --upstream {upstream}"
    port: 8765
    health: /_health
    upstream_via: cli_arg                   # upstream injected via {upstream} template
    enabled: true
```

### Service Config Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Unique service identifier |
| `directory` | yes | Absolute path to the service repo |
| `command` | yes | Start command — use `{port}` and `{upstream}` templates |
| `port` | yes | Port the service listens on |
| `health` | yes | Health check endpoint path (must return 2xx) |
| `stats` | no | Stats endpoint path for aggregation |
| `config_file` | no | YAML config file to patch (relative to directory) |
| `upstream_key` | no | Dot-path into config file for upstream URL |
| `upstream_via` | no | `config_file` (default) or `cli_arg` |
| `upstream_path` | no | Path suffix appended to the upstream URL |
| `enabled` | no | `true` (default) or `false` to skip |

### Gateway-only fields

| Field | Required | Description |
|-------|----------|-------------|
| `startup_health_timeout` | no | Seconds to wait for every enabled service to return 2xx on `health` before starting the gateway (default `120`). |
| `startup_health_poll_interval` | no | Seconds between health polling rounds (default `0.25`; must be ≤ `startup_health_timeout`). |
| `startup_health_strict` | no | If `true`, `manifold up` exits with status 1 when the timeout elapses without all services healthy. Default `false` (warn and continue). |

On **Windows**, subprocess teardown uses `terminate`/`kill` on the shell process only; on **Unix**, Manifold uses a process group (`killpg`) so nested children started by the shell are stopped together. Fully detached children can still survive on Windows; use **WSL** or an external process-tree kill if you need Unix-like behavior.

## Adding Your Own Service

Any HTTP service that exposes OpenAI-compatible (`/v1/chat/completions`) or Anthropic-compatible (`/v1/messages`) endpoints can be added to the pipeline. Requirements:

1. Accept a configurable upstream URL (via config file or CLI flag)
2. Forward requests to that upstream after processing
3. Expose a health check endpoint
4. Preserve request/response headers (especially `x-api-key`, `Authorization`)

Register interactively: `manifold add`

## CLI Commands

```bash
manifold up [-c manifold.yaml] [-v]   # Start all services + gateway
manifold down                          # Stop a running instance
manifold status [-c manifold.yaml]     # Show pipeline config and chain
manifold stats [-c manifold.yaml]      # Fetch stats from running gateway
manifold validate [-c manifold.yaml]   # Validate config without starting
manifold add [-c manifold.yaml]        # Interactively add a new service
```

## Gateway Endpoints

| Endpoint | Description |
|----------|-------------|
| `/_manifold/health` | Health status of all pipeline services |
| `/_manifold/stats` | Aggregated stats from all services |
| `/_manifold/config` | Current pipeline topology and service states |
| `/{path}` | Proxy — forwards to the first healthy service |

## Implementation Modules

```
src/manifold/
├── cli.py              # Typer CLI: up, down, status, stats, add, validate
├── config.py           # Load + validate manifold.yaml
├── gateway.py          # Thin httpx reverse proxy to first service
├── chain.py            # Chain wiring: patch configs, compute upstreams, bypass
├── health.py           # Periodic health checks, auto-rewire on failure
├── process.py          # Subprocess lifecycle: start, stop, restart, crash recovery
├── watcher.py          # Hot-reload on manifold.yaml changes
├── stats.py            # Aggregate stats from all services
├── logs.py             # Per-service log files (~/.manifold/logs/)
├── models.py           # Dataclasses: ServiceConfig, GatewayConfig, PipelineState
└── mcp_server.py       # MCP tools: status, stats, enable/disable, logs
```

## Key Design Decisions
- **No request modification** — Manifold never inspects or modifies request/response bodies. It's a topology manager + entry proxy only.
- **Config patching is file-based** — Manifold writes to each service's YAML config file before starting it. This is simple and works with all services' existing config loading.
- **Streaming is mandatory** — The gateway proxy MUST support SSE streaming pass-through since all LLM responses stream.
- **Process management is simple** — Just subprocesses with PID tracking. No containers, no systemd. Keep it simple.
- **Each service owns its own port** — No port multiplexing. Each service gets its own port as defined in the config.
- **Auth header passthrough** — The gateway normalizes `Authorization: Bearer` to `x-api-key` and forwards all headers through the chain.

## Repo automation
- **CI** — `.github/workflows/ci.yml` runs **pre-commit** (**ruff** + **ruff format**), **pytest** with **coverage** (XML artifact; `mcp_server.py` omitted from the percentage gate), and `workflow_dispatch` for manual runs.
- **Release** — `.github/workflows/release.yml` runs on tags `v*.*.*`, runs `uv build`, and publishes `dist/*` to a GitHub Release.
- **Dependabot** — `.github/dependabot.yml` bumps **GitHub Actions** and **uv** dependencies weekly.
- **pre-commit** — `.pre-commit-config.yaml` mirrors CI lint/format locally (`pre-commit install` after `pip install pre-commit`).

## Tech Stack
- Python 3.12+, `uv` for package management
- `httpx` — async HTTP client for proxying and health checks
- `starlette` — lightweight ASGI framework for the gateway
- `uvicorn` — ASGI server
- `pyyaml` — config file loading and patching
- `typer` — CLI framework
