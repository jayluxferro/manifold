"""Integration-style checks against a real local HTTP server (stdlib only)."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from manifold.health import wait_for_services_ready
from manifold.models import (
    GatewayConfig,
    PipelineState,
    ServiceConfig,
    ServiceState,
    UpstreamVia,
)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/healthz"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: N802
        return


@pytest.fixture
def health_port() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


@pytest.mark.asyncio
async def test_wait_for_services_ready_against_real_http(health_port: int):
    cfg = ServiceConfig(
        name="stub",
        directory="/tmp",
        command="echo",
        port=health_port,
        health="/healthz",
        upstream_via=UpstreamVia.CLI_ARG,
    )
    pipeline = PipelineState(services=[ServiceState(config=cfg)])
    gw = GatewayConfig(
        startup_health_timeout=5.0,
        startup_health_poll_interval=0.05,
        startup_health_strict=True,
    )
    await wait_for_services_ready(pipeline, gw)
