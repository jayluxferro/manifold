# Manifold

**Chain multiple LLM proxy services into a single transparent pipeline.**

Manifold is a lightweight control plane that wires together LLM proxy services — each running their own HTTP server with OpenAI/Anthropic-compatible APIs — into a linear chain behind a single entry point.

## Why

You have multiple LLM tools that each spin up their own proxy server:
- **llm-redactor** — scrubs PII/secrets before they reach the cloud
- **local-splitter** — routes trivial requests to a local model, compresses context
- **hivemind** — rate limiting, admission control, token budgets

Each one is valuable. But your coding agent can only point at **one** `ANTHROPIC_BASE_URL`. You don't want 100+ MCP tool definitions cluttering context, and you don't want to manually wire upstream URLs between services.

Manifold solves this:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:9000
# That's it. All services chain transparently.
```

## How It Works

```
Agent (:9000)
  │
  ▼
┌─────────────┐  upstream → :7788
│  Redactor   │─────────────────────┐
│  :7789      │                     ▼
└─────────────┘             ┌─────────────┐  upstream → :8765
                            │  Splitter   │─────────────────────┐
                            │  :7788      │                     ▼
                            └─────────────┘             ┌─────────────┐  upstream → Cloud
                                                        │  HiveMind   │──────▶ api.anthropic.com
                                                        │  :8765      │
                                                        └─────────────┘
```

**Request:** Agent → Redactor (scrub) → Splitter (compress) → HiveMind (throttle) → Cloud API
**Response:** Cloud → HiveMind (track) → Splitter (cache) → Redactor (restore) → Agent

Each service already acts as a reverse proxy with a configurable upstream. Manifold simply sets each service's upstream to point to the next service in the pipeline.

## Quick Start

```bash
# Install
cd /Users/jay/dev/ml/mcp/manifold
uv sync

# Configure your pipeline
cp configs/default.yaml manifold.yaml
# Edit manifold.yaml to match your service locations

# Start everything
manifold up

# Point your agent at manifold
export ANTHROPIC_BASE_URL=http://127.0.0.1:9000
```

## Configuration

```yaml
gateway:
  host: 127.0.0.1
  port: 9000

pipeline:
  - name: llm-redactor
    directory: /Users/jay/dev/ml/mcp/llm-redactor
    command: "uv run llm-redactor serve --port {port}"
    port: 7789
    health: /v1/redactor/config
    stats: /v1/redactor/stats
    config_file: llm_redactor.yaml
    upstream_key: cloud_target.endpoint
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
    upstream_via: cli_arg
    enabled: true
```

### Pipeline Order

The order in the `pipeline` list is the order requests flow through. The first service receives requests from the gateway; the last service forwards to the cloud API.

### Adding a New Service

Add an entry to the `pipeline` list:

```yaml
  - name: my-new-tool
    directory: /path/to/my-new-tool
    command: "uv run my-new-tool serve --port {port}"
    port: 7787
    health: /health
    stats: /stats
    config_file: config.yaml
    upstream_key: upstream.endpoint    # dot-path to upstream URL in service's YAML config
    enabled: true
```

### Upstream Wiring

Manifold supports two ways to set a service's upstream:

1. **Config file patching** (`upstream_key`): Manifold writes the next service's URL into the service's YAML config file at the specified dot-path before starting it.

2. **CLI argument** (`upstream_via: cli_arg`): Manifold templates `{upstream}` in the `command` string with the next service's URL. Used when the service takes upstream as a CLI flag (e.g., hivemind).

The **last** service in the pipeline keeps its own upstream unchanged — manifold doesn't touch it.

## CLI

```bash
manifold up              # Start all services + gateway (foreground)
manifold up -v           # Verbose mode with debug logging
manifold down            # Stop a running manifold instance (via PID file)
manifold status          # Show pipeline config and computed chain topology
manifold stats           # Fetch live stats from running gateway
manifold validate        # Validate manifold.yaml without starting anything
manifold add             # Interactively register a new service into the pipeline
```

### Options

All commands accept `--config / -c` to specify a config file path (defaults to `./manifold.yaml`).

## Hot Reload

Manifold watches `manifold.yaml` for changes while running. When you edit the config (enable/disable a service, change ports, add a new service), manifold automatically applies the changes — no restart needed.

## Log Aggregation

Per-service logs are written to `~/.manifold/logs/<service-name>.log` alongside console output. View them with:

```bash
# Or via the MCP server
manifold-mcp  # then call manifold_logs(service_name="llm-redactor")
```

## MCP Server

Manifold ships an MCP server (`manifold-mcp`) that exposes pipeline control tools for LLM agents:

| Tool | Description |
|------|-------------|
| `manifold_status` | Health, PIDs, ports of all services |
| `manifold_stats` | Aggregated stats from all services |
| `manifold_config` | Current pipeline topology |
| `manifold_validate` | Validate a config file |
| `manifold_enable` | Enable a service (hot-reloaded) |
| `manifold_disable` | Disable a service (hot-reloaded) |
| `manifold_logs` | Tail recent logs for a service |
| `manifold_list_logs` | List available log files |

Add to your MCP client config:

```json
{
  "mcpServers": {
    "manifold": {
      "command": "manifold-mcp",
      "args": []
    }
  }
}
```

## Endpoints

The gateway exposes these management endpoints alongside the proxy:

| Endpoint | Description |
|----------|-------------|
| `/_manifold/health` | Health status of all pipeline services |
| `/_manifold/stats` | Aggregated stats from all services |
| `/_manifold/config` | Current pipeline topology |

All other requests are forwarded transparently to the first service in the pipeline.

## Design Principles

- **Manifold never touches request/response bodies** — it's a topology manager and entry proxy, not a middleware
- **Streaming first** — SSE pass-through is mandatory for LLM response streaming
- **Fail-open** — if a service goes down, manifold rewires the chain to bypass it
- **Hot-reloadable** — edit manifold.yaml while running, changes apply automatically
- **Simple process management** — subprocesses with PID tracking, no containers
- **Convention over configuration** — services follow the OpenAI/Anthropic proxy pattern

## Requirements

- Python 3.12+
- uv
