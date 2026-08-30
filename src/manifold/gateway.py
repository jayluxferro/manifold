"""Async reverse proxy gateway — entry point for agent requests."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from manifold.models import GatewayConfig, PipelineState

log = logging.getLogger(__name__)

# Set MANIFOLD_DEBUG_HEADERS=1 to log inbound + outbound headers (with bearer
# tokens / x-api-key truncated).  Useful for diagnosing auth or beta-header
# stripping problems in the chain.
_DEBUG_HEADERS = os.environ.get("MANIFOLD_DEBUG_HEADERS") == "1"


def _redact_secret(value: str, keep: int = 12) -> str:
    if len(value) <= keep:
        return value
    return f"{value[:keep]}…<{len(value) - keep} more chars>"


def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of headers with secret values truncated for logging."""
    out: dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk == "authorization" and v.lower().startswith("bearer "):
            out[k] = f"Bearer {_redact_secret(v[7:])}"
        elif lk in ("x-api-key", "api-key", "cookie", "set-cookie"):
            out[k] = _redact_secret(v)
        else:
            out[k] = v
    return out


# Module-level state set by create_app
_pipeline: PipelineState | None = None
_gateway_config: GatewayConfig | None = None
_http_client: httpx.AsyncClient | None = None
# Callbacks injected by the orchestrator
_get_entry_url: Callable[[], str | None] | None = None
_get_stats: Callable[[], dict] | None = None
_get_health: Callable[[], dict] | None = None


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

    # Auth normalization.  Anthropic accepts two different auth methods,
    # and they are NOT interchangeable:
    #
    #   * Console API keys (``sk-ant-api…``) → ``x-api-key`` header.
    #   * OAuth access tokens (``sk-ant-oat…``, used by Claude Code) →
    #     ``Authorization: Bearer`` header.
    #
    # If we blindly copy a Bearer token into ``x-api-key``, Anthropic
    # returns 401 for OAuth tokens.  So: leave ``Authorization`` alone,
    # and only mirror it into ``x-api-key`` when the token is clearly a
    # console API key.
    if "x-api-key" not in headers:
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:]
            if not token.startswith("sk-ant-oat"):
                headers["x-api-key"] = token

    # Stream the request body to avoid buffering large payloads in memory
    body = request.stream()

    log.debug("Proxying %s %s → %s", request.method, request.url.path, url)
    if _DEBUG_HEADERS:
        log.warning(
            "MANIFOLD_DEBUG_HEADERS inbound %s %s%s headers=%s",
            request.method,
            request.url.path,
            f"?{request.url.query}" if request.url.query else "",
            _sanitize_headers(dict(request.headers)),
        )
        log.warning(
            "MANIFOLD_DEBUG_HEADERS outbound → %s headers=%s",
            url,
            _sanitize_headers(headers),
        )

    try:
        upstream_req = _http_client.build_request(
            method=request.method,
            url=url,
            headers=headers,
            content=body,
        )
        upstream_resp = await _http_client.send(upstream_req, stream=True)
    except (httpx.ConnectError, httpx.TransportError) as exc:
        # str(exc) is EMPTY for several transport exceptions (ReadError
        # wrapping anyio.EndOfStream, ConnectTimeout) — always prefix the
        # type and name the target or the log line is a bare colon.
        detail = f"{type(exc).__name__}: {exc}".rstrip(": ")
        log.error("Upstream connect error to %s: %s", url, detail)
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
        log.error(
            "Upstream timeout: %s %s → %s (client may have hit a rate/account limit "
            "— try the request directly to see the original error)",
            request.method,
            request.url.path,
            url,
        )
        return JSONResponse(
            {
                "error": {
                    "type": "proxy_error",
                    "message": (
                        f"Upstream timeout: {target} — if this persists, check "
                        "whether you've hit a rate or account limit by sending "
                        "a request directly to the cloud API"
                    ),
                }
            },
            status_code=504,
        )

    resp_headers = dict(upstream_resp.headers)
    resp_headers.pop("transfer-encoding", None)
    resp_headers.pop("content-length", None)
    resp_headers.pop("content-encoding", None)

    content_type = upstream_resp.headers.get("content-type", "")
    is_streaming = "text/event-stream" in content_type

    # Log non-2xx responses so upstream errors are visible in manifold logs
    # even when the client doesn't surface the body.
    if upstream_resp.status_code >= 400:
        if is_streaming:
            log.warning(
                "Upstream error: %s %s → %s returned %d (streaming; body not logged)",
                request.method,
                request.url.path,
                url,
                upstream_resp.status_code,
            )
        else:
            # Peek at the body to log it, then we'll still return it to the client.
            error_body = await upstream_resp.aread()
            snippet = error_body[:1024].decode("utf-8", errors="replace")
            log.warning(
                "Upstream error: %s %s → %s returned %d: %s",
                request.method,
                request.url.path,
                url,
                upstream_resp.status_code,
                snippet,
            )
            await upstream_resp.aclose()
            return Response(
                content=error_body,
                status_code=upstream_resp.status_code,
                headers=resp_headers,
            )

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
                # Registry fields: adopted services are shared with another
                # gateway (I1 — this one must never kill or restart them);
                # owner_port is the gateway port that owns the processes.
                "adopted": s.adopted,
                "owner_port": s.owner_port,
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
    get_entry_url: Callable[[], str | None] | None = None,
    get_stats: Callable[[], dict] | None = None,
    get_health: Callable[[], dict] | None = None,
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
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0),
            # Empty default user-agent so httpx doesn't inject python-httpx/X.Y.Z;
            # the agent's original user-agent flows through via forwarded headers.
            headers={"user-agent": ""},
        )
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
