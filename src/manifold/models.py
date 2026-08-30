"""Dataclasses for Manifold configuration and runtime state."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class UpstreamVia(enum.Enum):
    """How a service receives its upstream URL."""

    CONFIG_FILE = "config_file"
    CLI_ARG = "cli_arg"


class ServiceStatus(enum.Enum):
    """Runtime status of a pipeline service."""

    STOPPED = "stopped"
    STARTING = "starting"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


@dataclass
class ServiceConfig:
    """Static configuration for a single pipeline service."""

    name: str
    directory: str
    command: str
    port: int
    health: str
    stats: str | None = None
    config_file: str | None = None
    upstream_key: str | None = None
    upstream_via: UpstreamVia = UpstreamVia.CONFIG_FILE
    upstream_path: str = ""
    enabled: bool = True


@dataclass
class GatewayConfig:
    """Configuration for the manifold gateway itself."""

    host: str = "127.0.0.1"
    port: int = 9000
    fallback_upstream: str = "https://api.anthropic.com"
    # After subprocesses start, poll each enabled service's health until ready or timeout.
    startup_health_timeout: float = 120.0
    startup_health_poll_interval: float = 0.25
    # If true, ``manifold up`` exits non-zero when startup health never succeeds.
    startup_health_strict: bool = False
    # Rate-limit budget scope in hivemind (shared-service chains):
    # "session" (default) keeps hivemind's per-session buckets;
    # "port" stamps x-hivemind-agent-id with this gateway's port so every
    # request from this gateway shares one budget bucket, independent of
    # other gateways on the same shared services.
    rate_limit_scope: str = "session"


@dataclass
class ManifoldConfig:
    """Top-level configuration loaded from manifold.yaml."""

    gateway: GatewayConfig
    pipeline: list[ServiceConfig]


@dataclass
class ServiceState:
    """Runtime state for a single service in the pipeline."""

    config: ServiceConfig
    status: ServiceStatus = ServiceStatus.STOPPED
    pid: int | None = None
    upstream_url: str | None = None
    consecutive_failures: int = 0
    # Registry fields (see registry.py): adopted services are owned by another
    # gateway and must never be killed by this one (I1).
    adopted: bool = False
    pgid: int | None = None
    identity: str | None = None
    owner_port: int | None = None


@dataclass
class PipelineState:
    """Runtime state of the entire pipeline."""

    services: list[ServiceState] = field(default_factory=list)
    gateway_running: bool = False

    @property
    def active_services(self) -> list[ServiceState]:
        """Services that are enabled and currently healthy or starting."""
        return [
            s
            for s in self.services
            if s.config.enabled
            and s.status not in (ServiceStatus.STOPPED, ServiceStatus.UNHEALTHY)
        ]

    @property
    def healthy_services(self) -> list[ServiceState]:
        """Services currently marked healthy."""
        return [s for s in self.services if s.status == ServiceStatus.HEALTHY]

    def get_service(self, name: str) -> ServiceState | None:
        for s in self.services:
            if s.config.name == name:
                return s
        return None
