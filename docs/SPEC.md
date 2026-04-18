# Implementation Specification

## Module Reference

### `src/manifold/models.py`

All dataclasses. No logic.

```python
from dataclasses import dataclass, field
from typing import Literal
from datetime import datetime

@dataclass
class ServiceConfig:
    """One entry from the pipeline list in manifold.yaml."""
    name: str
    directory: str                          # absolute path to service project
    command: str                            # template with {port}, {upstream}
    port: int
    health: str                             # health endpoint path
    stats: str | None = None                # stats endpoint path
    config_file: str | None = None          # relative to directory
    upstream_key: str | None = None         # dot-path into YAML config
    upstream_via: Literal["config_file", "cli_arg"] = "config_file"
    enabled: bool = True
    
    # Resolved at runtime by chain wirer
    resolved_upstream: str | None = None    # computed URL to next service
    resolved_command: str | None = None     # command with templates filled

@dataclass
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 9000

@dataclass
class ManifoldConfig:
    gateway: GatewayConfig
    pipeline: list[ServiceConfig]

@dataclass
class ServiceState:
    """Runtime state for a managed service."""
    config: ServiceConfig
    status: Literal["stopped", "starting", "running", "unhealthy", "crashed"] = "stopped"
    pid: int | None = None
    restart_count: int = 0
    last_started: datetime | None = None
    last_health_check: datetime | None = None
    consecutive_failures: int = 0
```

### `src/manifold/config.py`

```python
def load_config(path: str | None = None) -> ManifoldConfig:
    """
    Load manifold.yaml from:
    1. Explicit path argument
    2. $MANIFOLD_CONFIG env var
    3. ./manifold.yaml
    
    Validates:
    - No duplicate names
    - No duplicate ports
    - gateway.port doesn't conflict with service ports
    - All directories exist
    - Each service has upstream_key XOR upstream_via=cli_arg
    - At least one enabled service
    
    Returns ManifoldConfig with fully parsed ServiceConfig list.
    """

def validate_config(config: ManifoldConfig) -> list[str]:
    """Returns list of validation errors (empty = valid)."""
```

### `src/manifold/chain.py`

```python
def compute_chain(services: list[ServiceConfig]) -> list[ServiceConfig]:
    """
    Given ordered list of ENABLED services, compute resolved_upstream for each.
    
    - services[0..n-1].resolved_upstream = http://127.0.0.1:{next_service.port}
    - services[n].resolved_upstream = None (keep existing upstream)
    
    Returns the same list with resolved_upstream set.
    """

def apply_chain(services: list[ServiceConfig]) -> None:
    """
    Apply computed upstreams to each service:
    
    For upstream_via == "config_file":
      - Read service's config_file (YAML)
      - Navigate dot-path upstream_key
      - Set value to resolved_upstream
      - Write file back
    
    For upstream_via == "cli_arg":
      - Template {upstream} in command string
      - Store in resolved_command
    
    Skip last service (resolved_upstream is None).
    """

def patch_yaml_dotpath(filepath: str, dotpath: str, value: str) -> None:
    """
    Read YAML file, set nested key by dot-path, write back.
    
    Example: patch_yaml_dotpath("config.yaml", "models.cloud.endpoint", "http://127.0.0.1:8765")
    Sets config["models"]["cloud"]["endpoint"] = "http://127.0.0.1:8765"
    """

def bypass_service(
    all_services: list[ServiceConfig], 
    service_name: str,
) -> list[ServiceConfig]:
    """
    Remove a service from the active chain and rewire neighbors.
    Returns the new active chain (excluding the bypassed service).
    """

def restore_service(
    all_services: list[ServiceConfig],
    service_name: str,
) -> list[ServiceConfig]:
    """
    Re-add a previously bypassed service and rewire.
    Returns the full chain with the service back in its original position.
    """
```

### `src/manifold/process.py`

```python
class ProcessManager:
    """Manages service subprocesses."""
    
    def __init__(self, services: list[ServiceConfig]):
        self.services = services
        self.states: dict[str, ServiceState] = {}
    
    async def start_service(self, name: str) -> None:
        """
        Start a single service subprocess.
        
        1. Resolve the command (fill {port}, {upstream} templates)
        2. cwd = service.directory
        3. asyncio.create_subprocess_exec(shell=True, ...)
        4. Store PID, set status = "starting"
        5. Wait for health endpoint (poll every 500ms, timeout 30s)
        6. On healthy: set status = "running"
        7. On timeout: set status = "crashed", log stderr
        """
    
    async def stop_service(self, name: str) -> None:
        """
        Stop a service.
        1. Send SIGTERM
        2. Wait 5s for exit
        3. If still alive, SIGKILL
        4. Set status = "stopped"
        """
    
    async def start_all(self) -> None:
        """Start all enabled services in pipeline order."""
    
    async def stop_all(self) -> None:
        """Stop all services in reverse pipeline order."""
    
    async def restart_service(self, name: str) -> None:
        """Stop then start. Increment restart_count."""
    
    def get_states(self) -> dict[str, ServiceState]:
        """Return current state of all services."""
```

### `src/manifold/health.py`

```python
class HealthMonitor:
    """Background health checker."""
    
    def __init__(
        self,
        process_manager: ProcessManager,
        chain_wirer,  # callback to rewire chain
        interval: float = 5.0,
        failure_threshold: int = 3,
    ):
        self._task: asyncio.Task | None = None
    
    async def start(self) -> None:
        """Start the background health check loop."""
    
    async def stop(self) -> None:
        """Cancel the background task."""
    
    async def _loop(self) -> None:
        """
        Every `interval` seconds:
        1. For each running/unhealthy service, hit health endpoint
        2. Track consecutive failures
        3. On threshold breach: mark unhealthy, call chain bypass
        4. On recovery: mark running, call chain restore
        """
    
    async def check_one(self, service: ServiceConfig) -> bool:
        """
        GET http://127.0.0.1:{port}{health_path}
        Timeout: 3s
        Returns True if status < 500
        """
```

### `src/manifold/gateway.py`

```python
def create_app(config: ManifoldConfig, process_manager: ProcessManager) -> Starlette:
    """
    Build the Starlette ASGI app.
    
    Routes:
    - /_manifold/health  → health_handler
    - /_manifold/stats   → stats_handler  
    - /_manifold/config  → config_handler
    - /{path:path}       → proxy_handler (catch-all, must be last)
    
    The app holds a reference to process_manager to know
    which service is first/healthy for proxying.
    """

async def proxy_handler(request: Request) -> StreamingResponse:
    """
    Forward request to first healthy service, stream response back.
    
    CRITICAL: Must handle:
    - SSE streaming (text/event-stream content type)
    - Chunked transfer encoding
    - Large request bodies (POST with full conversation context)
    - All HTTP methods (GET, POST, PUT, DELETE, OPTIONS)
    - Query parameters
    - Request headers (filter hop-by-hop headers)
    
    Add response headers:
    - x-manifold-pipeline: comma-separated list of active services
    - x-manifold-served-by: name of first service in chain
    
    On connection error to first service:
    - Return 502 {"error": "service_unavailable", "service": "<name>"}
    """

async def health_handler(request: Request) -> JSONResponse:
    """Return health state of all services."""

async def stats_handler(request: Request) -> JSONResponse:
    """Aggregate stats from all services (concurrent fetch)."""

async def config_handler(request: Request) -> JSONResponse:
    """Return current pipeline topology."""
```

### `src/manifold/stats.py`

```python
async def aggregate_stats(
    services: list[ServiceConfig],
    states: dict[str, ServiceState],
) -> dict:
    """
    Hit each running service's stats endpoint concurrently.
    Return merged dict with per-service stats + manifold metadata.
    
    Services that are down or have no stats endpoint: include
    {"status": "unavailable"} instead.
    """
```

### `src/manifold/cli.py`

```python
"""
CLI commands:

  manifold up [--config PATH] [--detach]
    - Load config
    - Compute and apply chain
    - Start all services
    - Start gateway
    - If --detach: daemonize (write PID file to .manifold/manifold.pid)
    - Otherwise: run in foreground, Ctrl+C for graceful shutdown

  manifold down
    - Read PID file, send SIGTERM to manifold process
    - Manifold process handles shutdown of all services

  manifold status [--config PATH]
    - Hit /_manifold/health on running gateway
    - Display table: service name, port, status, PID, uptime

  manifold stats [--config PATH]
    - Hit /_manifold/stats on running gateway
    - Display formatted stats

  manifold check [--config PATH]
    - Validate config without starting anything
    - Show computed chain topology
"""
```

## Config File Format

### manifold.yaml (full schema)

```yaml
# Gateway configuration
gateway:
  host: 127.0.0.1         # bind address (default: 127.0.0.1)
  port: 9000               # bind port (default: 9000)

# Pipeline services in chain order
pipeline:
  - name: string           # unique service identifier
    directory: string       # absolute path to service project root
    command: string         # start command, supports {port} and {upstream} templates
    port: integer           # port the service listens on
    health: string          # health check endpoint path (GET)
    stats: string | null    # stats endpoint path (GET), optional
    config_file: string | null  # YAML config filename, relative to directory
    upstream_key: string | null # dot-path to upstream URL in service's YAML config
    upstream_via: "config_file" | "cli_arg"  # how to set upstream (default: config_file)
    enabled: boolean        # whether to include in chain (default: true)
```

### Validation rules:
- `name` must be unique across all pipeline entries
- `port` must be unique across all pipeline entries AND different from `gateway.port`
- `directory` must exist on the filesystem
- If `upstream_via` is `config_file`: `config_file` and `upstream_key` must be set
- If `upstream_via` is `cli_arg`: `command` must contain `{upstream}`
- At least one service must have `enabled: true`

## CLI Entry Point

```toml
# In pyproject.toml
[project.scripts]
manifold = "manifold.cli:app"
```

## Startup Lifecycle

```
manifold up
    │
    ├── 1. Load manifold.yaml
    ├── 2. Validate config
    ├── 3. Filter enabled services
    ├── 4. Compute chain (resolve upstreams)
    ├── 5. Apply chain (patch configs / resolve commands)
    ├── 6. Start services in order (first → last)
    │      ├── For each: spawn subprocess, wait for health
    │      └── Fail if any service doesn't become healthy in 30s
    ├── 7. Start health monitor (background task)
    ├── 8. Start gateway (uvicorn ASGI server)
    └── 9. Log "Manifold ready on http://127.0.0.1:9000"
            Log "Pipeline: redactor → splitter → hivemind → [cloud]"
```

## Shutdown Lifecycle

```
Ctrl+C / SIGTERM
    │
    ├── 1. Stop accepting new gateway connections
    ├── 2. Stop health monitor
    ├── 3. Stop services in reverse order (last → first)
    │      ├── SIGTERM each
    │      ├── Wait 5s
    │      └── SIGKILL if needed
    ├── 4. Stop gateway
    └── 5. Exit
```

## Hop-by-Hop Headers to Strip

When proxying, remove these headers (they're connection-specific):
- `connection`
- `keep-alive`
- `proxy-authenticate`
- `proxy-authorization`
- `te`
- `trailers`
- `transfer-encoding`
- `upgrade`

Preserve `host` header (rewrite to upstream target).

## Testing Strategy

### Unit Tests

1. **config**: Load valid YAML, reject invalid (duplicate names, ports, missing dirs)
2. **chain**: Compute upstreams for 1, 2, 3+ services; verify last service untouched
3. **chain.patch_yaml_dotpath**: Nested dot-paths, create missing intermediate keys, preserve other keys
4. **chain.bypass/restore**: Rewire correctly, preserve original ordering

### Integration Tests

1. **Mock service chain**: Start 2-3 FastAPI echo services, run manifold, verify request flows through all
2. **Streaming**: Verify SSE responses stream correctly through the chain
3. **Health bypass**: Start chain, kill a middle service, verify traffic bypasses it
4. **Startup/shutdown**: Verify clean lifecycle with no orphan processes

### Mock Service for Testing

```python
# tests/mock_service.py
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import httpx, asyncio

app = FastAPI()
SERVICE_NAME = "mock-a"
UPSTREAM = "http://127.0.0.1:9999"  # next service or httpbin

@app.get("/healthz")
async def health():
    return {"status": "ok", "service": SERVICE_NAME}

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(request: Request, path: str):
    # Add header showing we processed this
    headers = dict(request.headers)
    headers[f"x-{SERVICE_NAME}-processed"] = "true"
    
    async with httpx.AsyncClient() as client:
        resp = await client.request(
            method=request.method,
            url=f"{UPSTREAM}/{path}",
            headers=headers,
            content=await request.body(),
        )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=dict(resp.headers),
    )
```
