"""Tests for manifold.gateway module."""


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


def test_lifespan_closes_http_client():
    """Lifespan teardown closes the HTTP client (service cleanup is in _run_pipeline)."""
    pipeline = _make_pipeline()
    gw = GatewayConfig()
    app = create_app(
        pipeline=pipeline,
        gateway_config=gw,
        get_entry_url=lambda: "http://127.0.0.1:7001",
    )
    import manifold.gateway as gw_mod

    with TestClient(app):
        # HTTP client should be created during lifespan startup
        assert gw_mod._http_client is not None
        assert not gw_mod._http_client.is_closed
    # After lifespan teardown, client is closed
    assert gw_mod._http_client.is_closed
