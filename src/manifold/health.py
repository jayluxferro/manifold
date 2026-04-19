"""Periodic health checks and automatic bypass/rewire on failure."""

from __future__ import annotations

import asyncio
import logging

import httpx

from manifold.chain import rewire_around
from manifold.models import GatewayConfig, PipelineState, ServiceState, ServiceStatus

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = 5.0
FAILURE_THRESHOLD = 3  # consecutive failures before marking unhealthy
HEALTH_TIMEOUT = 3.0


class StartupHealthTimeoutError(Exception):
    """Raised when ``startup_health_strict`` is true and services never become healthy."""


async def check_service_health(
    state: ServiceState,
    client: httpx.AsyncClient,
) -> bool:
    """Ping a single service's health endpoint. Returns True if healthy."""
    svc = state.config
    url = f"http://127.0.0.1:{svc.port}{svc.health}"
    try:
        resp = await client.get(url, timeout=HEALTH_TIMEOUT)
        if resp.status_code < 400:
            return True
        log.debug("%s health check returned %s", svc.name, resp.status_code)
        return False
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        log.debug("%s health check failed: %s", svc.name, exc)
        return False


async def wait_for_services_ready(
    pipeline: PipelineState,
    gateway: GatewayConfig,
) -> None:
    """Poll enabled services until each passes ``health`` or the gateway timeout elapses.

    Uses ``gateway.startup_health_timeout``, ``startup_health_poll_interval``, and
    ``startup_health_strict``. On timeout: if strict, raises
    :exc:`StartupHealthTimeoutError`; otherwise logs a warning and returns so the
    gateway can still start.
    """
    enabled = [s for s in pipeline.services if s.config.enabled]
    if not enabled:
        return

    timeout = gateway.startup_health_timeout
    poll_interval = gateway.startup_health_poll_interval
    strict = gateway.startup_health_strict

    log.info("Waiting for services to become healthy (timeout %ss)...", int(timeout))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    async with httpx.AsyncClient() as client:
        while loop.time() < deadline:
            results = await asyncio.gather(
                *[check_service_health(s, client) for s in enabled],
            )
            if all(results):
                for svc in enabled:
                    svc.status = ServiceStatus.HEALTHY
                    svc.consecutive_failures = 0
                log.info("All enabled services passed initial health check")
                return
            await asyncio.sleep(poll_interval)

    names = ", ".join(s.config.name for s in enabled)
    msg = (
        f"Timed out after {int(timeout)}s waiting for initial health on gateway "
        f"{gateway.host}:{gateway.port}; services: {names}"
    )
    if strict:
        raise StartupHealthTimeoutError(msg)
    log.warning("%s — starting gateway anyway", msg)


async def run_health_checks(
    pipeline: PipelineState,
    gateway: GatewayConfig,
    client: httpx.AsyncClient,
) -> bool:
    """Run one round of health checks on all enabled services.

    Returns True if any service changed state (chain may need rewiring).
    """
    changed = False

    for state in pipeline.services:
        if not state.config.enabled or state.status == ServiceStatus.STOPPED:
            continue

        healthy = await check_service_health(state, client)

        if healthy:
            if state.status != ServiceStatus.HEALTHY:
                log.info("%s is now healthy", state.config.name)
                state.status = ServiceStatus.HEALTHY
                state.consecutive_failures = 0
                changed = True
            else:
                state.consecutive_failures = 0
        else:
            state.consecutive_failures += 1
            if (
                state.consecutive_failures >= FAILURE_THRESHOLD
                and state.status == ServiceStatus.HEALTHY
            ):
                log.warning(
                    "%s marked unhealthy after %d consecutive failures",
                    state.config.name,
                    state.consecutive_failures,
                )
                state.status = ServiceStatus.UNHEALTHY
                changed = True

    if changed:
        rewire_around(pipeline, gateway)

    return changed


async def health_loop(
    pipeline: PipelineState,
    gateway: GatewayConfig,
    interval: float = DEFAULT_INTERVAL,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Background loop that checks service health and rewires the chain."""
    async with httpx.AsyncClient() as client:
        while True:
            if stop_event and stop_event.is_set():
                break
            try:
                await run_health_checks(pipeline, gateway, client)
            except Exception:
                log.exception("Health check loop error")
            try:
                if stop_event:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                    break
                else:
                    await asyncio.sleep(interval)
            except asyncio.TimeoutError:
                pass
