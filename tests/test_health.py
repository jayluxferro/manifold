"""Tests for manifold.health."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from manifold.health import (
    StartupHealthTimeoutError,
    run_health_checks,
    wait_for_services_ready,
)
from manifold.models import (
    GatewayConfig,
    PipelineState,
    ServiceConfig,
    ServiceState,
    ServiceStatus,
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


async def _run_health_round(pipe, healthy: bool, callback) -> None:
    """One run_health_checks round with a fake client and health result."""
    with (
        patch(
            "manifold.health.check_service_health",
            new_callable=AsyncMock,
            return_value=healthy,
        ),
        patch("manifold.health.rewire_around"),
    ):
        await run_health_checks(
            pipe,
            GatewayConfig(),
            MagicMock(),
            on_adopted_unhealthy=callback,
        )


@pytest.mark.asyncio
async def test_adopted_unhealthy_flip_calls_callback_once():
    pipe = _pipeline_one()
    state = pipe.services[0]
    state.status = ServiceStatus.HEALTHY
    state.adopted = True
    callback = AsyncMock()
    for _ in range(3):
        await _run_health_round(pipe, healthy=False, callback=callback)
    callback.assert_awaited_once_with(state)
    assert state.status == ServiceStatus.UNHEALTHY
    # already unhealthy: further failures must not re-fire the callback
    await _run_health_round(pipe, healthy=False, callback=callback)
    callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_adopted_unhealthy_flip_does_not_call_callback():
    pipe = _pipeline_one()
    state = pipe.services[0]
    state.status = ServiceStatus.HEALTHY
    state.adopted = False
    callback = AsyncMock()
    for _ in range(3):
        await _run_health_round(pipe, healthy=False, callback=callback)
    callback.assert_not_called()
    assert state.status == ServiceStatus.UNHEALTHY


@pytest.mark.asyncio
async def test_recovery_healthy_does_not_call_callback():
    pipe = _pipeline_one()
    state = pipe.services[0]
    state.status = ServiceStatus.HEALTHY
    state.adopted = True
    callback = AsyncMock()
    # stays healthy the whole time -> never fired
    for _ in range(3):
        await _run_health_round(pipe, healthy=True, callback=callback)
    callback.assert_not_called()
    # flip unhealthy, then recover healthy -> fired exactly once, not on recovery
    for _ in range(3):
        await _run_health_round(pipe, healthy=False, callback=callback)
    assert callback.await_count == 1
    await _run_health_round(pipe, healthy=True, callback=callback)
    assert state.status == ServiceStatus.HEALTHY
    callback.assert_awaited_once()
