"""Tests for manifold.stats module."""

import httpx

from manifold.models import (
    PipelineState,
    ServiceConfig,
    ServiceState,
    ServiceStatus,
)
from manifold.stats import aggregate_stats, fetch_service_stats


def _svc(
    name: str,
    port: int,
    stats: str | None = "/stats",
    enabled: bool = True,
) -> ServiceConfig:
    return ServiceConfig(
        name=name,
        directory="/tmp/test",
        command=f"echo {name}",
        port=port,
        health="/healthz",
        stats=stats,
        enabled=enabled,
    )


def _state(
    name: str,
    port: int,
    status: ServiceStatus = ServiceStatus.HEALTHY,
    stats: str | None = "/stats",
    enabled: bool = True,
    pid: int | None = 1234,
    upstream_url: str | None = "http://127.0.0.1:9999",
) -> ServiceState:
    return ServiceState(
        config=_svc(name, port, stats=stats, enabled=enabled),
        status=status,
        pid=pid,
        upstream_url=upstream_url,
    )


async def test_fetch_service_stats_success(httpx_mock):
    httpx_mock.add_response(
        url="http://127.0.0.1:7000/stats",
        json={"requests": 42},
    )
    async with httpx.AsyncClient() as client:
        result = await fetch_service_stats("svc-a", 7000, "/stats", client)
    assert result == {"requests": 42}


async def test_fetch_service_stats_http_error(httpx_mock):
    httpx_mock.add_response(
        url="http://127.0.0.1:7000/stats",
        status_code=500,
    )
    async with httpx.AsyncClient() as client:
        result = await fetch_service_stats("svc-a", 7000, "/stats", client)
    assert "error" in result
    assert "500" in result["error"]


async def test_fetch_service_stats_connect_error(httpx_mock):
    httpx_mock.add_exception(
        httpx.ConnectError("connection refused"),
        url="http://127.0.0.1:7000/stats",
    )
    async with httpx.AsyncClient() as client:
        result = await fetch_service_stats("svc-a", 7000, "/stats", client)
    assert "error" in result


async def test_fetch_service_stats_unexpected_error(httpx_mock):
    httpx_mock.add_exception(
        RuntimeError("boom"),
        url="http://127.0.0.1:7000/stats",
    )
    async with httpx.AsyncClient() as client:
        result = await fetch_service_stats("svc-a", 7000, "/stats", client)
    assert "error" in result
    assert "boom" in result["error"]


async def test_aggregate_stats_healthy_with_stats(httpx_mock):
    httpx_mock.add_response(
        url="http://127.0.0.1:7000/stats",
        json={"requests": 10},
    )
    pipeline = PipelineState(
        services=[_state("svc-a", 7000)],
    )
    async with httpx.AsyncClient() as client:
        result = await aggregate_stats(pipeline, client)

    assert result["pipeline_summary"]["total"] == 1
    assert result["pipeline_summary"]["healthy"] == 1
    assert result["services"]["svc-a"]["stats"] == {"requests": 10}
    assert result["services"]["svc-a"]["status"] == "healthy"


async def test_aggregate_stats_unhealthy_skips_stats():
    pipeline = PipelineState(
        services=[_state("svc-a", 7000, status=ServiceStatus.UNHEALTHY)],
    )
    async with httpx.AsyncClient() as client:
        result = await aggregate_stats(pipeline, client)

    assert "stats" not in result["services"]["svc-a"]
    assert result["pipeline_summary"]["healthy"] == 0


async def test_aggregate_stats_disabled_skips_stats():
    pipeline = PipelineState(
        services=[_state("svc-a", 7000, enabled=False)],
    )
    async with httpx.AsyncClient() as client:
        result = await aggregate_stats(pipeline, client)

    assert "stats" not in result["services"]["svc-a"]


async def test_aggregate_stats_no_stats_endpoint():
    pipeline = PipelineState(
        services=[_state("svc-a", 7000, stats=None)],
    )
    async with httpx.AsyncClient() as client:
        result = await aggregate_stats(pipeline, client)

    assert "stats" not in result["services"]["svc-a"]


async def test_aggregate_stats_creates_own_client():
    """aggregate_stats creates its own client when none is provided."""
    pipeline = PipelineState(services=[])
    result = await aggregate_stats(pipeline)
    assert result["pipeline_summary"]["total"] == 0


async def test_aggregate_stats_multiple_services(httpx_mock):
    httpx_mock.add_response(
        url="http://127.0.0.1:7000/stats",
        json={"requests": 5},
    )
    pipeline = PipelineState(
        services=[
            _state("svc-a", 7000, status=ServiceStatus.HEALTHY),
            _state("svc-b", 7001, status=ServiceStatus.STOPPED, pid=None),
        ],
    )
    async with httpx.AsyncClient() as client:
        result = await aggregate_stats(pipeline, client)

    assert result["pipeline_summary"]["total"] == 2
    assert result["pipeline_summary"]["healthy"] == 1
    assert "stats" in result["services"]["svc-a"]
    assert "stats" not in result["services"]["svc-b"]
