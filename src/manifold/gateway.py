"""Async reverse proxy gateway — entry point for agent requests."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from manifold.models import GatewayConfig, PipelineState

log = logging.getLogger(__name__)

# Module-level state set by create_app
_pipeline: PipelineState | None = None
_gateway_config: GatewayConfig | None = None
_http_client: httpx.AsyncClient | None = None
# Callbacks injected by the orchestrator
_get_entry_url: callable = None
_get_stats: callable = None
_get_health: callable = None


def _target_url() -> str | None:
    """Resolve the current entry URL for proxying.

    Returns None when no pipeline service is available — the gateway must
    never bypass the pipeline and send directly to the cloud API.
    """
    if _get_entry_url is not None:
        return _get_entry_url()
    return None


async def _proxy(request: Request) -> Response:
    """Forward an incoming request to the first pipeline service."""
    target = _target_url()
    if target is None:
        return JSONResponse(
            {
                "error": {
                    "type": "proxy_error",
                    "message": "Pipeline unavailable: no healthy services",
                }
            },
            status_code=503,
        )

    url = f"{target}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("transfer-encoding", None)
    headers.pop("content-length", None)

    # Normalize auth: if client sends Authorization Bearer but not x-api-key,
    # extract the token and set x-api-key so downstream services and the
    # Anthropic API (which only checks x-api-key) can authenticate.
    if "x-api-key" not in headers:
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            headers["x-api-key"] = auth[7:]

    # Stream the request body to avoid buffering large payloads in memory
    body = request.stream()

    log.debug("Proxying %s %s → %s", request.method, request.url.path, url)

    try:
        upstream_req = _http_client.build_request(
            method=request.method,
            url=url,
            headers=headers,
            content=body,
        )
        upstream_resp = await _http_client.send(upstream_req, stream=True)
    except (httpx.ConnectError, httpx.TransportError) as exc:
        log.error("Upstream connect error: %s", exc)
        return JSONResponse(
            {
                "error": {
                    "type": "proxy_error",
                    "message": f"Upstream unreachable: {target}",
                }
            },
            status_code=502,
        )
    except httpx.TimeoutException:
        return JSONResponse(
            {"error": {"type": "proxy_error", "message": "Upstream timeout"}},
            status_code=504,
        )

    resp_headers = dict(upstream_resp.headers)
    resp_headers.pop("transfer-encoding", None)
    resp_headers.pop("content-length", None)
    resp_headers.pop("content-encoding", None)

    content_type = upstream_resp.headers.get("content-type", "")
    is_streaming = "text/event-stream" in content_type

    if is_streaming:

        async def stream_body():
            try:
                async for chunk in upstream_resp.aiter_bytes():
                    yield chunk
            finally:
                await upstream_resp.aclose()

        return StreamingResponse(
            stream_body(),
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type="text/event-stream",
        )

    # Non-streaming: read full body then close
    body_bytes = await upstream_resp.aread()
    await upstream_resp.aclose()
    return Response(
        content=body_bytes,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
    )


async def _manifold_health(request: Request) -> JSONResponse:
    if _get_health:
        return JSONResponse(await _get_health())
    return JSONResponse({"status": "ok"})


async def _manifold_stats(request: Request) -> JSONResponse:
    if _get_stats:
        return JSONResponse(await _get_stats())
    return JSONResponse({})


async def _manifold_config(request: Request) -> JSONResponse:
    if _pipeline is None:
        return JSONResponse({"error": "not initialized"}, status_code=503)
    services = []
    for s in _pipeline.services:
        services.append(
            {
                "name": s.config.name,
                "port": s.config.port,
                "enabled": s.config.enabled,
                "status": s.status.value,
                "upstream": s.upstream_url,
                "pid": s.pid,
            }
        )
    return JSONResponse(
        {
            "gateway": {
                "host": _gateway_config.host,
                "port": _gateway_config.port,
            },
            "pipeline": services,
        }
    )


def create_app(
    pipeline: PipelineState,
    gateway_config: GatewayConfig,
    get_entry_url: callable | None = None,
    get_stats: callable | None = None,
    get_health: callable | None = None,
) -> Starlette:
    """Create the Starlette ASGI gateway application."""
    global _pipeline, _gateway_config, _http_client
    global _get_entry_url, _get_stats, _get_health

    _pipeline = pipeline
    _gateway_config = gateway_config
    _get_entry_url = get_entry_url
    _get_stats = get_stats
    _get_health = get_health

    @asynccontextmanager
    async def lifespan(app):
        global _http_client
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
        yield
        await _http_client.aclose()

    routes = [
        Route("/_manifold/health", _manifold_health, methods=["GET"]),
        Route("/_manifold/stats", _manifold_stats, methods=["GET"]),
        Route("/_manifold/config", _manifold_config, methods=["GET"]),
        Route(
            "/{path:path}",
            _proxy,
            methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
        ),
    ]

    return Starlette(routes=routes, lifespan=lifespan)
