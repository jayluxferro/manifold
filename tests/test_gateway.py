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
    # Registry fields are always present; _make_pipeline's service is owned
    # by this gateway, so it is not adopted and has no owner_port.
    assert data["pipeline"][0]["adopted"] is False
    assert data["pipeline"][0]["owner_port"] is None


def test_manifold_config_reports_adopted_service():
    """Adopted services expose their owner gateway's port in /_manifold/config."""
    svc = ServiceConfig(
        name="adopted-svc",
        directory="/tmp",
        command="echo",
        port=7002,
        health="/h",
        upstream_via=UpstreamVia.CLI_ARG,
    )
    state = ServiceState(
        config=svc,
        status=ServiceStatus.HEALTHY,
        pid=4242,
        adopted=True,
        owner_port=9001,
    )
    state.upstream_url = "https://api.anthropic.com"
    pipeline = PipelineState(services=[state])
    app = create_app(
        pipeline=pipeline,
        gateway_config=GatewayConfig(),
        get_entry_url=lambda: "http://127.0.0.1:7002",
    )

    with TestClient(app) as c:
        resp = c.get("/_manifold/config")
        assert resp.status_code == 200
        svc_data = resp.json()["pipeline"][0]
        assert svc_data["adopted"] is True
        assert svc_data["owner_port"] == 9001


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


def test_proxy_empty_str_connect_error_is_typed_in_log(caplog):
    """Regression: transport exceptions often have an EMPTY str() — the log
    line must still name the type AND the target URL, not end at the colon."""
    import logging

    import manifold.gateway as gw_mod

    pipeline = _make_pipeline()
    gw = GatewayConfig()
    app = create_app(
        pipeline=pipeline,
        gateway_config=gw,
        get_entry_url=lambda: "http://127.0.0.1:7001",
    )

    with TestClient(app) as c:
        orig_client = gw_mod._http_client
        mock_send = AsyncMock(side_effect=httpx.ConnectError(""))
        orig_client.send = mock_send  # type: ignore[method-assign]

        with caplog.at_level(logging.ERROR, logger="manifold.gateway"):
            resp = c.post("/v1/messages", json={"model": "test"})
        assert resp.status_code == 502

    assert any(
        "ConnectError" in r.getMessage() and "7001" in r.getMessage()
        for r in caplog.records
    )


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


def test_per_port_scope_stamps_bucket_header_and_overwrites_client_value():
    """rate_limit_scope=port: every proxied request carries
    x-hivemind-agent-id: gateway-<port>, overwriting any client value —
    the gateway is the trust boundary for per-port budgets."""
    pipeline = _make_pipeline()
    app = create_app(
        pipeline=pipeline,
        gateway_config=GatewayConfig(port=9001, rate_limit_scope="port"),
        get_entry_url=lambda: "http://127.0.0.1:7001",
    )

    with TestClient(app) as c:
        captured = _install_capture_transport()
        resp = c.post(
            "/v1/messages",
            json={"model": "test"},
            headers={"x-hivemind-agent-id": "client-chosen-bucket"},
        )

    assert resp.status_code == 200
    assert captured["headers"]["x-hivemind-agent-id"] == "gateway-9001"


def test_session_scope_does_not_stamp_bucket_header():
    """Default scope=session: the gateway must not add x-hivemind-agent-id —
    hivemind keeps its per-session bucketing."""
    pipeline = _make_pipeline()
    app = create_app(
        pipeline=pipeline,
        gateway_config=GatewayConfig(rate_limit_scope="session"),
        get_entry_url=lambda: "http://127.0.0.1:7001",
    )

    with TestClient(app) as c:
        captured = _install_capture_transport()
        resp = c.post("/v1/messages", json={"model": "test"})

    assert resp.status_code == 200
    assert "x-hivemind-agent-id" not in captured["headers"]


# --- per-request bypass observability (x-manifold-bypassed / x-manifold-shim)


def _two_service_pipeline():
    """a (down) in front of b (healthy) — the classic redactor-out shape."""
    svc_a = ServiceConfig(
        name="llm-redactor",
        directory="/tmp",
        command="echo",
        port=7001,
        health="/h",
        upstream_via=UpstreamVia.CLI_ARG,
    )
    svc_b = ServiceConfig(
        name="hivemind",
        directory="/tmp",
        command="echo",
        port=7002,
        health="/h",
        upstream_via=UpstreamVia.CLI_ARG,
    )
    a = ServiceState(config=svc_a, status=ServiceStatus.UNHEALTHY)
    b = ServiceState(config=svc_b, status=ServiceStatus.HEALTHY)
    a.upstream_url = "http://127.0.0.1:7002"
    b.upstream_url = "https://api.anthropic.com"
    return PipelineState(services=[a, b])


def test_entry_bypass_sets_bypassed_header():
    """A request whose entry hop skipped a down service says so on the
    response — silent degradation is the worst kind."""
    app = create_app(
        pipeline=_two_service_pipeline(),
        gateway_config=GatewayConfig(),
        get_entry_url=lambda: "http://127.0.0.1:7002",
        get_entry_route=lambda: ("http://127.0.0.1:7002", ["llm-redactor"]),
    )

    with TestClient(app) as c:
        _install_capture_transport()
        resp = c.post("/v1/messages", json={"model": "test"})

    assert resp.status_code == 200
    assert resp.headers["x-manifold-bypassed"] == "llm-redactor"


def test_healthy_chain_has_no_bypass_headers():
    app = create_app(
        pipeline=_make_pipeline(),
        gateway_config=GatewayConfig(),
        get_entry_url=lambda: "http://127.0.0.1:7001",
        get_entry_route=lambda: ("http://127.0.0.1:7001", []),
    )

    with TestClient(app) as c:
        _install_capture_transport()
        resp = c.post("/v1/messages", json={"model": "test"})

    assert resp.status_code == 200
    assert "x-manifold-bypassed" not in resp.headers
    assert "x-manifold-shim" not in resp.headers


def test_streaming_response_carries_bypass_header():
    """The stamp is set before body streaming starts, so SSE responses carry
    it too."""

    def sse_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"data: hi\n\n",
        )

    app = create_app(
        pipeline=_two_service_pipeline(),
        gateway_config=GatewayConfig(),
        get_entry_url=lambda: "http://127.0.0.1:7002",
        get_entry_route=lambda: ("http://127.0.0.1:7002", ["llm-redactor"]),
    )

    with TestClient(app) as c:
        import manifold.gateway as gw_mod

        gw_mod._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(sse_handler)
        )
        resp = c.post("/v1/messages", json={"model": "test"})

    assert resp.status_code == 200
    assert resp.headers["x-manifold-bypassed"] == "llm-redactor"


def test_shimmed_service_in_path_sets_shim_header():
    """Serving through a crash shim (gateway-local state) is stamped with the
    shimmed service's name."""
    import manifold.shim as shim
    from manifold.shim import ShimHandle

    pipeline = _two_service_pipeline()
    pipeline.services[0].status = ServiceStatus.HEALTHY  # entry is now "a"
    pipeline.services[1].status = ServiceStatus.UNHEALTHY
    app = create_app(
        pipeline=pipeline,
        gateway_config=GatewayConfig(),
        get_entry_url=lambda: "http://127.0.0.1:7001",
        get_entry_route=lambda: ("http://127.0.0.1:7001", []),
    )

    shim._shims[7002] = ShimHandle(  # b's port is a crash shim
        listen_port=7002, target_host="127.0.0.1", target_port=7003
    )
    try:
        with TestClient(app) as c:
            _install_capture_transport()
            resp = c.post("/v1/messages", json={"model": "test"})
        assert resp.status_code == 200
        assert resp.headers["x-manifold-shim"] == "hivemind"
    finally:
        shim._shims.pop(7002, None)


def test_shim_before_entry_is_not_reported():
    """A shim on a service the entry hop already skips is not in this
    request's path."""
    import manifold.shim as shim
    from manifold.shim import ShimHandle

    app = create_app(
        pipeline=_two_service_pipeline(),  # entry resolves to b (index 1)
        gateway_config=GatewayConfig(),
        get_entry_url=lambda: "http://127.0.0.1:7002",
        get_entry_route=lambda: ("http://127.0.0.1:7002", ["llm-redactor"]),
    )

    shim._shims[7001] = ShimHandle(  # shimmed BEFORE the entry hop
        listen_port=7001, target_host="127.0.0.1", target_port=7003
    )
    try:
        with TestClient(app) as c:
            _install_capture_transport()
            resp = c.post("/v1/messages", json={"model": "test"})
        assert resp.status_code == 200
        assert "x-manifold-shim" not in resp.headers
    finally:
        shim._shims.pop(7001, None)


def test_error_response_carries_bypass_header():
    """A 502 from a bypassed chain is exactly when you're debugging — the
    stamp rides error responses too."""
    import manifold.gateway as gw_mod

    app = create_app(
        pipeline=_two_service_pipeline(),
        gateway_config=GatewayConfig(),
        get_entry_url=lambda: "http://127.0.0.1:7002",
        get_entry_route=lambda: ("http://127.0.0.1:7002", ["llm-redactor"]),
    )

    with TestClient(app) as c:
        orig_client = gw_mod._http_client
        mock_send = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        orig_client.send = mock_send  # type: ignore[method-assign]

        resp = c.post("/v1/messages", json={"model": "test"})
        assert resp.status_code == 502
        assert resp.headers["x-manifold-bypassed"] == "llm-redactor"


def test_fully_down_503_carries_degradation_stamps():
    """Round-seven minor closed: the no-route 503 is precisely when the
    bypass/shim signal matters — it must carry the stamps, not return
    before they're computed."""
    from unittest.mock import patch

    from manifold.gateway import app

    from fastapi.testclient import TestClient

    with (
        patch(
            "manifold.gateway._get_entry_route", return_value=(None, ["llm-redactor"])
        ),
        patch("manifold.gateway._shimmed_service_names", return_value=["veritas"]),
    ):
        r = TestClient(app).post("/v1/messages", content=b"{}")
    assert r.status_code == 503
    assert r.headers.get("x-manifold-bypassed") == "llm-redactor"
    assert r.headers.get("x-manifold-shim") == "veritas"
