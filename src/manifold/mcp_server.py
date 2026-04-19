"""MCP server exposing manifold status, stats, and control tools."""

from __future__ import annotations

import json
import logging

import httpx
from mcp.server.fastmcp import FastMCP

from manifold.config import ConfigError, find_config, load_config
from manifold.logs import list_logs, tail_log
from manifold.paths import PORT_FILE

log = logging.getLogger(__name__)

mcp = FastMCP(
    "manifold",
    instructions="Control plane for the Manifold LLM proxy pipeline",
)

# Default gateway address — overridden if PID/port files exist
_DEFAULT_ADDR = "127.0.0.1:9000"


def _gateway_addr() -> str:
    """Resolve the running gateway address."""
    if PORT_FILE.exists():
        return PORT_FILE.read_text().strip()
    return _DEFAULT_ADDR


def _get(path: str, timeout: float = 5.0) -> dict:
    """GET a JSON endpoint from the running gateway."""
    addr = _gateway_addr()
    url = f"http://{addr}{path}"
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
def manifold_status() -> str:
    """Get the current status of all pipeline services — health, PIDs, ports, and upstreams."""
    try:
        data = _get("/_manifold/health")
        return json.dumps(data, indent=2)
    except httpx.ConnectError:
        return "Error: Cannot connect to manifold gateway. Is it running? (manifold up)"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def manifold_stats() -> str:
    """Get aggregated statistics from all pipeline services."""
    try:
        data = _get("/_manifold/stats")
        return json.dumps(data, indent=2)
    except httpx.ConnectError:
        return "Error: Cannot connect to manifold gateway. Is it running? (manifold up)"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def manifold_config() -> str:
    """Get the current pipeline topology — which services are active, their ports and upstreams."""
    try:
        data = _get("/_manifold/config")
        return json.dumps(data, indent=2)
    except httpx.ConnectError:
        return "Error: Cannot connect to manifold gateway. Is it running? (manifold up)"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def manifold_validate(config_path: str | None = None) -> str:
    """Validate a manifold.yaml configuration file without starting anything.

    Args:
        config_path: Optional path to manifold.yaml. If not provided, searches default locations.
    """
    try:
        cfg = load_config(config_path)
        enabled = [s.name for s in cfg.pipeline if s.enabled]
        return json.dumps(
            {
                "valid": True,
                "services": len(cfg.pipeline),
                "enabled": len(enabled),
                "chain": enabled,
                "gateway": f"{cfg.gateway.host}:{cfg.gateway.port}",
            },
            indent=2,
        )
    except ConfigError as e:
        return json.dumps({"valid": False, "error": str(e)}, indent=2)


@mcp.tool()
def manifold_enable(service_name: str) -> str:
    """Enable a service in the pipeline by updating manifold.yaml.

    Args:
        service_name: Name of the service to enable.
    """
    return _toggle_service(service_name, enabled=True)


@mcp.tool()
def manifold_disable(service_name: str) -> str:
    """Disable a service in the pipeline by updating manifold.yaml. The watcher will hot-reload the change.

    Args:
        service_name: Name of the service to disable.
    """
    return _toggle_service(service_name, enabled=False)


def _toggle_service(name: str, enabled: bool) -> str:
    """Toggle a service's enabled state in manifold.yaml."""
    import yaml

    try:
        config_path = find_config()
    except ConfigError as e:
        return f"Error: {e}"

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    found = False
    for entry in raw.get("pipeline", []):
        if entry.get("name") == name:
            entry["enabled"] = enabled
            found = True
            break

    if not found:
        return f"Error: Service '{name}' not found in {config_path}"

    with open(config_path, "w") as f:
        yaml.safe_dump(raw, f, default_flow_style=False, sort_keys=False)

    action = "enabled" if enabled else "disabled"
    return (
        f"Service '{name}' {action} in {config_path}. Hot-reload will apply the change."
    )


@mcp.tool()
def manifold_logs(service_name: str, lines: int = 50) -> str:
    """View recent log output from a pipeline service.

    Args:
        service_name: Name of the service to view logs for.
        lines: Number of recent lines to return (default 50).
    """
    log_lines = tail_log(service_name, lines)
    if not log_lines:
        return f"No logs found for '{service_name}'"
    return "\n".join(log_lines)


@mcp.tool()
def manifold_list_logs() -> str:
    """List all available service log files."""
    logs = list_logs()
    if not logs:
        return "No service logs found"
    return json.dumps(logs, indent=2)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
