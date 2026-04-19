"""Tests for manifold.models module."""

from manifold.models import (
    PipelineState,
    ServiceConfig,
    ServiceState,
    ServiceStatus,
)


def _state(name: str, status: ServiceStatus, enabled: bool = True) -> ServiceState:
    return ServiceState(
        config=ServiceConfig(
            name=name,
            directory="/tmp/test",
            command=f"echo {name}",
            port=7000,
            health="/healthz",
            enabled=enabled,
        ),
        status=status,
    )


def test_healthy_services():
    pipeline = PipelineState(
        services=[
            _state("a", ServiceStatus.HEALTHY),
            _state("b", ServiceStatus.UNHEALTHY),
            _state("c", ServiceStatus.HEALTHY),
        ]
    )
    healthy = pipeline.healthy_services
    assert len(healthy) == 2
    assert {s.config.name for s in healthy} == {"a", "c"}


def test_active_services():
    pipeline = PipelineState(
        services=[
            _state("a", ServiceStatus.HEALTHY),
            _state("b", ServiceStatus.STOPPED),
            _state("c", ServiceStatus.STARTING),
            _state("d", ServiceStatus.UNHEALTHY),
            _state("e", ServiceStatus.HEALTHY, enabled=False),
        ]
    )
    active = pipeline.active_services
    assert len(active) == 2
    assert {s.config.name for s in active} == {"a", "c"}


def test_get_service_found():
    pipeline = PipelineState(
        services=[
            _state("a", ServiceStatus.STOPPED),
            _state("b", ServiceStatus.HEALTHY),
        ]
    )
    svc = pipeline.get_service("b")
    assert svc is not None
    assert svc.config.name == "b"


def test_get_service_not_found():
    pipeline = PipelineState(services=[_state("a", ServiceStatus.STOPPED)])
    assert pipeline.get_service("missing") is None
