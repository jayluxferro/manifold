"""Load and validate manifold.yaml configuration."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from manifold.models import (
    GatewayConfig,
    ManifoldConfig,
    ServiceConfig,
    UpstreamVia,
)

DEFAULT_CONFIG_PATHS = [
    Path("manifold.yaml"),
    Path("manifold.yml"),
    Path.home() / ".config" / "manifold" / "manifold.yaml",
]


class ConfigError(Exception):
    """Raised when configuration is invalid."""


def find_config(explicit_path: str | Path | None = None) -> Path:
    """Locate the manifold config file.

    If *explicit_path* is given, use it directly (error if missing).
    Otherwise search DEFAULT_CONFIG_PATHS in order.
    """
    if explicit_path is not None:
        p = Path(explicit_path)
        if not p.exists():
            raise ConfigError(f"Config file not found: {p}")
        return p

    env = os.environ.get("MANIFOLD_CONFIG")
    if env:
        p = Path(env)
        if not p.exists():
            raise ConfigError(f"MANIFOLD_CONFIG points to missing file: {p}")
        return p

    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.exists():
            return candidate

    raise ConfigError(
        "No manifold.yaml found. Create one or set MANIFOLD_CONFIG env var."
    )


def _parse_service(raw: dict) -> ServiceConfig:
    """Parse a single pipeline entry into a ServiceConfig."""
    required = {"name", "directory", "command", "port", "health"}
    missing = required - set(raw.keys())
    if missing:
        raise ConfigError(
            f"Service entry missing required fields: {', '.join(sorted(missing))}"
        )

    via_raw = raw.get("upstream_via", "config_file")
    try:
        upstream_via = UpstreamVia(via_raw)
    except ValueError:
        raise ConfigError(
            f"Service '{raw['name']}': upstream_via must be 'config_file' or 'cli_arg', got '{via_raw}'"
        )

    if upstream_via == UpstreamVia.CONFIG_FILE:
        if not raw.get("config_file"):
            raise ConfigError(
                f"Service '{raw['name']}': upstream_via=config_file requires config_file"
            )
        if not raw.get("upstream_key"):
            raise ConfigError(
                f"Service '{raw['name']}': upstream_via=config_file requires upstream_key"
            )

    return ServiceConfig(
        name=raw["name"],
        directory=raw["directory"],
        command=raw["command"],
        port=int(raw["port"]),
        health=raw["health"],
        stats=raw.get("stats"),
        config_file=raw.get("config_file"),
        upstream_key=raw.get("upstream_key"),
        upstream_via=upstream_via,
        upstream_path=raw.get("upstream_path", ""),
        enabled=raw.get("enabled", True),
    )


def _parse_gateway(raw: dict | None) -> GatewayConfig:
    raw = raw or {}
    return GatewayConfig(
        host=raw.get("host", "127.0.0.1"),
        port=int(raw.get("port", 9000)),
        fallback_upstream=raw.get("fallback_upstream", "https://api.anthropic.com"),
    )


def load_config(path: str | Path | None = None) -> ManifoldConfig:
    """Load and validate a manifold configuration file."""
    config_path = find_config(path)
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ConfigError(f"Config file must be a YAML mapping, got {type(raw).__name__}")

    gateway = _parse_gateway(raw.get("gateway"))

    pipeline_raw = raw.get("pipeline")
    if not pipeline_raw or not isinstance(pipeline_raw, list):
        raise ConfigError("Config must contain a 'pipeline' list with at least one service")

    services = [_parse_service(entry) for entry in pipeline_raw]

    # Validate no duplicate names or ports
    names = [s.name for s in services]
    if len(names) != len(set(names)):
        dupes = [n for n in names if names.count(n) > 1]
        raise ConfigError(f"Duplicate service names: {', '.join(set(dupes))}")

    ports = [s.port for s in services]
    if len(ports) != len(set(ports)):
        raise ConfigError("Duplicate service ports detected")

    if gateway.port in ports:
        raise ConfigError(
            f"Gateway port {gateway.port} conflicts with a service port"
        )

    # Warn about missing directories (non-fatal — service may be on another machine)
    for svc in services:
        if not Path(svc.directory).is_dir():
            import warnings
            warnings.warn(
                f"Service '{svc.name}': directory does not exist: {svc.directory}",
                stacklevel=2,
            )

    return ManifoldConfig(gateway=gateway, pipeline=services)
