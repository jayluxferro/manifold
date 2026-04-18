"""Aggregate stats from all pipeline services."""

from __future__ import annotations

import logging

import httpx

from manifold.models import PipelineState, ServiceStatus

log = logging.getLogger(__name__)

STATS_TIMEOUT = 5.0


async def fetch_service_stats(
    name: str,
    port: int,
    stats_path: str,
    client: httpx.AsyncClient,
) -> dict:
    """Fetch stats from a single service."""
    url = f"http://127.0.0.1:{port}{stats_path}"
    try:
        resp = await client.get(url, timeout=STATS_TIMEOUT)
        if resp.status_code < 400:
            return resp.json()
        log.debug("Stats for %s returned %s", name, resp.status_code)
        return {"error": f"HTTP {resp.status_code}"}
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        log.debug("Stats for %s failed: %s", name, exc)
        return {"error": str(exc)}
    except Exception as exc:
        log.debug("Stats for %s unexpected error: %s", name, exc)
        return {"error": str(exc)}


async def aggregate_stats(
    pipeline: PipelineState,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Collect stats from all healthy services with stats endpoints."""
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient()

    try:
        result: dict = {
            "services": {},
            "pipeline_summary": {
                "total": len(pipeline.services),
                "healthy": len(pipeline.healthy_services),
                "active": len(pipeline.active_services),
            },
        }

        for state in pipeline.services:
            svc = state.config
            entry = {
                "status": state.status.value,
                "port": svc.port,
                "enabled": svc.enabled,
                "pid": state.pid,
                "upstream": state.upstream_url,
            }

            if (
                svc.stats
                and svc.enabled
                and state.status == ServiceStatus.HEALTHY
            ):
                entry["stats"] = await fetch_service_stats(
                    svc.name, svc.port, svc.stats, client
                )

            result["services"][svc.name] = entry

        return result
    finally:
        if own_client:
            await client.aclose()
