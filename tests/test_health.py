"""Tests for manifold.health."""

import logging
from unittest.mock import AsyncMock, patch

import pytest

from manifold.health import StartupHealthTimeoutError, wait_for_services_ready
from manifold.models import (
    GatewayConfig,
    PipelineState,
    ServiceConfig,
    ServiceState,
    UpstreamVia,
)


def _pipeline_one(enabled: bool = True) -> PipelineState:
    cfg = ServiceConfig(
        name="alpha",
        directory="/tmp",
        command="echo",
        port=17001,
        health="/healthz",
        upstream_via=UpstreamVia.CLI_ARG,
        enabled=enabled,
    )
    return PipelineState(services=[ServiceState(config=cfg)])


@pytest.mark.asyncio
async def test_wait_for_services_ready_all_healthy_immediately():
    gw = GatewayConfig(startup_health_timeout=30.0, startup_health_poll_interval=0.01)
    pipe = _pipeline_one()
    with patch(
        "manifold.health.check_service_health",
        new_callable=AsyncMock,
        return_value=True,
    ):
        await wait_for_services_ready(pipe, gw)


@pytest.mark.asyncio
async def test_wait_for_services_ready_no_enabled_services():
    gw = GatewayConfig()
    pipe = _pipeline_one(enabled=False)
    with patch("manifold.health.check_service_health", new_callable=AsyncMock) as m:
        await wait_for_services_ready(pipe, gw)
    m.assert_not_called()


@pytest.mark.asyncio
async def test_wait_for_services_ready_timeout_logs(caplog):
    caplog.set_level(logging.WARNING)
    gw = GatewayConfig(
        host="127.0.0.1",
        port=9000,
        startup_health_timeout=0.15,
        startup_health_poll_interval=0.05,
        startup_health_strict=False,
    )
    pipe = _pipeline_one()
    with patch(
        "manifold.health.check_service_health",
        new_callable=AsyncMock,
        return_value=False,
    ):
        await wait_for_services_ready(pipe, gw)
    assert any("Timed out" in r.message for r in caplog.records)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_wait_for_services_ready_strict_raises():
    gw = GatewayConfig(
        startup_health_timeout=0.12,
        startup_health_poll_interval=0.05,
        startup_health_strict=True,
    )
    pipe = _pipeline_one()
    with patch(
        "manifold.health.check_service_health",
        new_callable=AsyncMock,
        return_value=False,
    ):
        with pytest.raises(StartupHealthTimeoutError, match="Timed out"):
            await wait_for_services_ready(pipe, gw)
