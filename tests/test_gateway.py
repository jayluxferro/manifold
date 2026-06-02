"""Tests for manifold.gateway module."""

from unittest.mock import AsyncMock

import httpx
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


def test_proxy_returns_502_when_upstream_down():
    """Proxy returns 502 when connection to upstream service fails."""
    import manifold.gateway as gw_mod

    pipeline = _make_pipeline()
    gw = GatewayConfig()
    app = create_app(
        pipeline=pipeline,
        gateway_config=gw,
        get_entry_url=lambda: "http://127.0.0.1:7001",
    )

    with TestClient(app) as c:
        # _http_client was created during lifespan startup.  Replace its
        # send() method with a mock that raises ConnectError so the proxy
        # behaves as if the upstream is unreachable.
        orig_client = gw_mod._http_client
        mock_send = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        orig_client.send = mock_send  # type: ignore[method-assign]

        resp = c.post("/v1/messages", json={"model": "test"})
        assert resp.status_code == 502
        data = resp.json()
        assert data["error"]["type"] == "proxy_error"


def _install_capture_transport():
    """Replace the gateway's httpx client with a MockTransport that captures requests."""
    import manifold.gateway as gw_mod

    captured: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200, content=b"{}", headers={"content-type": "application/json"}
        )

    transport = httpx.MockTransport(handler)
    gw_mod._http_client = httpx.AsyncClient(transport=transport)
    return captured


def test_oauth_bearer_token_is_not_normalized_to_x_api_key():
    """Claude Code OAuth tokens (sk-ant-oat...) must stay in Authorization only."""
    pipeline = _make_pipeline()
    app = create_app(
        pipeline=pipeline,
        gateway_config=GatewayConfig(),
        get_entry_url=lambda: "http://127.0.0.1:7001",
    )

    with TestClient(app) as c:
        captured = _install_capture_transport()
        token = "sk-ant-oat01-abcdef-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        c.post(
            "/v1/messages",
            json={"model": "test"},
            headers={"authorization": f"Bearer {token}"},
        )

    headers = captured["headers"]
    assert headers.get("authorization") == f"Bearer {token}"
    assert "x-api-key" not in {k.lower() for k in headers.keys()}


def test_console_api_key_bearer_is_mirrored_to_x_api_key():
    """Console API keys (sk-ant-api...) sent as Bearer get copied into x-api-key."""
    pipeline = _make_pipeline()
    app = create_app(
        pipeline=pipeline,
        gateway_config=GatewayConfig(),
        get_entry_url=lambda: "http://127.0.0.1:7001",
    )

    with TestClient(app) as c:
        captured = _install_capture_transport()
        token = "sk-ant-api03-abcdef-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        c.post(
            "/v1/messages",
            json={"model": "test"},
            headers={"authorization": f"Bearer {token}"},
        )

    headers = captured["headers"]
    assert headers.get("x-api-key") == token


def test_explicit_x_api_key_is_not_overwritten():
    """If the client sent x-api-key directly, normalization must leave it alone."""
    pipeline = _make_pipeline()
    app = create_app(
        pipeline=pipeline,
        gateway_config=GatewayConfig(),
        get_entry_url=lambda: "http://127.0.0.1:7001",
    )

    with TestClient(app) as c:
        captured = _install_capture_transport()
        c.post(
            "/v1/messages",
            json={"model": "test"},
            headers={
                "authorization": "Bearer should-not-leak",
                "x-api-key": "the-real-one",
            },
        )

    assert captured["headers"].get("x-api-key") == "the-real-one"


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
