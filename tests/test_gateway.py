"""Tests for manifold.gateway module."""

from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from manifold.gateway import create_app
from manifold.models import (
    GatewayConfig,
    PipelineState,
    ServiceConfig,
    ServiceState,
    ServiceStatus,
    UpstreamVia,
)


def _make_pipeline():
    svc = ServiceConfig(
        name="test-svc",
        directory="/tmp",
        command="echo",
        port=7001,
        health="/h",
        upstream_via=UpstreamVia.CLI_ARG,
    )
    state = ServiceState(config=svc, status=ServiceStatus.HEALTHY, pid=1234)
    state.upstream_url = "https://api.anthropic.com"
    return PipelineState(services=[state])


@pytest.fixture
def client():
    pipeline = _make_pipeline()
    gw = GatewayConfig()
    app = create_app(
        pipeline=pipeline,
        gateway_config=gw,
        get_entry_url=lambda: "http://127.0.0.1:7001",
        get_stats=None,
        get_health=None,
    )
    # Use context manager to trigger lifespan (creates _http_client)
    with TestClient(app) as c:
        yield c


def test_manifold_config_endpoint(client):
    resp = client.get("/_manifold/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "pipeline" in data
    assert len(data["pipeline"]) == 1
    assert data["pipeline"][0]["name"] == "test-svc"
    assert data["pipeline"][0]["status"] == "healthy"
    assert data["gateway"]["port"] == 9000


def test_manifold_health_returns_ok(client):
    resp = client.get("/_manifold/health")
    assert resp.status_code == 200


def test_manifold_stats_returns_empty_without_callback(client):
    resp = client.get("/_manifold/stats")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_proxy_returns_502_when_upstream_down(client):
    resp = client.post("/v1/messages", json={"model": "test"})
    assert resp.status_code == 502


def test_lifespan_shutdown_stops_pipeline_and_clears_pid_files(tmp_path):
    """Teardown must run during ASGI shutdown (before uvicorn re-raises signals)."""
    pipeline = _make_pipeline()
    gw = GatewayConfig()
    app = create_app(
        pipeline=pipeline,
        gateway_config=gw,
        get_entry_url=lambda: "http://127.0.0.1:7001",
    )
    pid_file = tmp_path / "manifold.pid"
    port_file = tmp_path / "manifold.port"
    pid_file.write_text("1")
    port_file.write_text("127.0.0.1:9000")
    with (
        patch("manifold.gateway.stop_all", new_callable=AsyncMock) as stop_all_mock,
        patch("manifold.gateway.PID_FILE", pid_file),
        patch("manifold.gateway.PORT_FILE", port_file),
    ):
        with TestClient(app):
            pass
        stop_all_mock.assert_awaited_once_with(pipeline.services)
        assert pipeline.gateway_running is False
    assert not pid_file.exists()
    assert not port_file.exists()
