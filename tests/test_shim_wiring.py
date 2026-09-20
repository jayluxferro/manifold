"""Tests for crash-shim wiring: who starts shims, who stops them, who sees them.

The transport itself is covered in test_shim.py; here the shim runs for real
(there is no honest way to fake "a socket is bound on the dead service's
port") while everything around it — subprocess spawns, health HTTP, the
registry — uses the repo's usual patch idioms.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import manifold.cli as cli_module
import manifold.process as mp
import manifold.shim as shim
from manifold import registry
from manifold.cli import (
    _is_entry_service,
    _maybe_start_shim,
    _shim_reconcile,
    _shim_target_for,
)
from manifold.health import check_service_health, health_loop
from manifold.models import GatewayConfig, PipelineState, ServiceState, ServiceStatus
from tests.test_shim import _echo_handler, _free_port, _tcp_server


def _svc(name: str, port: int) -> ServiceState:
    from manifold.models import ServiceConfig, UpstreamVia

    return ServiceState(
        config=ServiceConfig(
            name=name,
            directory="/tmp",
            command=f"echo {name} --port {{port}} --upstream {{upstream}}",
            port=port,
            health="/healthz",
            upstream_via=UpstreamVia.CLI_ARG,
            enabled=True,
        )
    )


@pytest.fixture(autouse=True)
def _clean_shim_table():
    shim._shims.clear()
    yield
    shim._shims.clear()


def test_shim_target_skips_dead_services():
    """a → (b dead) → c → d: a's shim target is c, the next LIVE service."""
    ports = {n: 7000 + i for i, n in enumerate(("a", "b", "c", "d"))}
    pipe = PipelineState(services=[_svc(n, p) for n, p in ports.items()])
    pipe.get_service("b").status = ServiceStatus.UNHEALTHY
    pipe.get_service("a").status = ServiceStatus.HEALTHY
    pipe.get_service("c").status = ServiceStatus.HEALTHY
    pipe.get_service("d").status = ServiceStatus.HEALTHY
    assert _shim_target_for(pipe, pipe.get_service("a")) is pipe.get_service("c")


def test_shim_target_none_when_nothing_live_downstream():
    pipe = PipelineState(services=[_svc("a", 7010), _svc("b", 7011)])
    pipe.get_service("a").status = ServiceStatus.HEALTHY
    pipe.get_service("b").status = ServiceStatus.UNHEALTHY
    assert _shim_target_for(pipe, pipe.get_service("a")) is None


def test_is_entry_service():
    pipe = PipelineState(services=[_svc("a", 7012), _svc("b", 7013)])
    assert _is_entry_service(pipe, pipe.get_service("a"))
    assert not _is_entry_service(pipe, pipe.get_service("b"))


@pytest.mark.asyncio
async def test_maybe_start_shim_skips_entry_hop():
    """The gateway re-resolves get_entry_url per request — index 0 is covered."""
    pipe = PipelineState(services=[_svc("a", 7014), _svc("b", 7015)])
    pipe.get_service("a").status = ServiceStatus.UNHEALTHY
    pipe.get_service("b").status = ServiceStatus.HEALTHY
    assert await _maybe_start_shim(pipe, pipe.get_service("a")) is None
    assert shim.all_shims() == {}


@pytest.mark.asyncio
async def test_maybe_start_shim_skips_when_no_live_downstream():
    pipe = PipelineState(services=[_svc("a", 7016), _svc("b", 7017)])
    pipe.get_service("a").status = ServiceStatus.HEALTHY
    pipe.get_service("b").status = ServiceStatus.UNHEALTHY
    assert await _maybe_start_shim(pipe, pipe.get_service("b")) is None
    assert shim.all_shims() == {}


@pytest.mark.asyncio
async def test_maybe_start_shim_port_still_bound_degrades_to_no_shim():
    """A hung process keeps its listener — the shim can't intercept, so no shim."""
    listener = await _tcp_server(await _free_port(), _echo_handler)
    port = listener.sockets[0].getsockname()[1]
    try:
        pipe = PipelineState(services=[_svc("a", port), _svc("b", await _free_port())])
        pipe.get_service("a").status = ServiceStatus.HEALTHY
        pipe.get_service("b").status = ServiceStatus.HEALTHY
        assert await _maybe_start_shim(pipe, pipe.get_service("a")) is None
        assert shim.all_shims() == {}
    finally:
        listener.close()


@pytest.mark.asyncio
async def test_shim_on_dead_mid_chain_service_carries_traffic_to_next_live():
    """The core promise: traffic aimed at the corpse's port reaches the next live
    service.  Service b is dead (its restart will fail — permanent crash); a's
    baked upstream still points at b's port; the shim forwards those bytes to c.
    """
    port_b = await _free_port()
    port_c = await _free_port()
    c_server = await _tcp_server(port_c, _echo_handler)
    pipe = PipelineState(
        services=[_svc("a", await _free_port()), _svc("b", port_b), _svc("c", port_c)]
    )
    pipe.get_service("a").status = ServiceStatus.HEALTHY
    pipe.get_service("b").status = ServiceStatus.UNHEALTHY  # b's process is gone
    pipe.get_service("c").status = ServiceStatus.HEALTHY

    handle = await _maybe_start_shim(pipe, pipe.get_service("b"))
    assert handle is not None
    try:
        # "a" is any client of b's port: its bytes must reach c and come back.
        reader, writer = await asyncio.open_connection("127.0.0.1", port_b)
        writer.write(b"request that must not die with b")
        writer.write_eof()
        echoed = await asyncio.wait_for(reader.read(4096), timeout=5.0)
        assert echoed == b"request that must not die with b"
        writer.close()

        # A permanently failing restart must not take the shim down with it.
        with patch(
            "manifold.process.start_service",
            new_callable=AsyncMock,
            side_effect=OSError("crash loop: never comes back"),
        ):
            with pytest.raises(OSError):
                await mp.start_service(pipe.get_service("b"), "http://127.0.0.1:1")
        assert shim.get_shim(port_b) is handle  # still protecting the chain
    finally:
        await shim.stop_shim(handle)
        c_server.close()


@pytest.mark.asyncio
async def test_recovery_shim_stops_and_real_service_retakes_port():
    """Recovery: the respawn (process.start_service) stops the shim first, then
    the real service rebinds the port it owns."""
    port_b = await _free_port()
    port_c = await _free_port()
    c_server = await _tcp_server(port_c, _echo_handler)
    pipe = PipelineState(
        services=[_svc("a", await _free_port()), _svc("b", port_b), _svc("c", port_c)]
    )
    state_b = pipe.get_service("b")
    pipe.get_service("a").status = ServiceStatus.HEALTHY
    state_b.status = ServiceStatus.UNHEALTHY
    pipe.get_service("c").status = ServiceStatus.HEALTHY
    handle = await _maybe_start_shim(pipe, state_b)
    assert handle is not None

    fake_proc = MagicMock()
    fake_proc.pid = 4242
    with (
        patch.object(mp.sys, "platform", "linux"),
        patch(
            "manifold.process.asyncio.create_subprocess_shell",
            new_callable=AsyncMock,
            return_value=fake_proc,
        ),
        patch("manifold.process.os.getpgid", return_value=4242),
        patch("manifold.process.setup_service_log", return_value=None),
        patch(
            "manifold.process.asyncio.create_task",
            side_effect=lambda coro: coro.close(),
        ),
        patch.dict(mp._processes, {}, clear=True),
        patch.dict(mp._log_tasks, {}, clear=True),
    ):
        await mp.start_service(state_b, "http://127.0.0.1:1")

    # The choke point released the port; a real listener can take it back.
    assert shim.get_shim(port_b) is None
    reborn = await _tcp_server(port_b, _echo_handler)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port_b)
        writer.write(b"the real b is back")
        writer.write_eof()
        assert await asyncio.wait_for(reader.read(4096), timeout=5.0) == (
            b"the real b is back"
        )
        writer.close()
    finally:
        reborn.close()
        c_server.close()


@pytest.mark.asyncio
async def test_start_service_stops_shim_before_spawn():
    """The choke point: every spawn path clears a shim on its target port."""
    port = await _free_port()
    target = await _tcp_server(await _free_port(), _echo_handler)
    handle = await shim.start_shim(port, "127.0.0.1", 1)
    state = _svc("b", port)

    fake_proc = MagicMock()
    fake_proc.pid = 4242
    spawned = False

    def _fake_create_task(coro):
        nonlocal spawned
        spawned = True
        coro.close()
        return MagicMock()

    with (
        patch.object(mp.sys, "platform", "linux"),
        patch(
            "manifold.process.asyncio.create_subprocess_shell",
            new_callable=AsyncMock,
            return_value=fake_proc,
        ),
        patch("manifold.process.os.getpgid", return_value=4242),
        patch("manifold.process.setup_service_log", return_value=None),
        patch("manifold.process.asyncio.create_task", side_effect=_fake_create_task),
        patch.dict(mp._processes, {}, clear=True),
        patch.dict(mp._log_tasks, {}, clear=True),
    ):
        await mp.start_service(state, "http://127.0.0.1:1")
    assert spawned
    assert shim.get_shim(port) is None
    assert handle.server is not None and handle.server.is_serving() is False
    target.close()


@pytest.mark.asyncio
async def test_check_service_health_shimmed_port_reports_unhealthy():
    """A 2xx through the shim is the NEXT service answering — never flip a
    shimmed corpse to healthy."""
    port = await _free_port()
    state = _svc("b", port)
    state.status = ServiceStatus.UNHEALTHY
    target = await _tcp_server(await _free_port(), _echo_handler)
    handle = await shim.start_shim(port, "127.0.0.1", 1)
    try:
        assert await check_service_health(state, AsyncMock()) is False
        await shim.stop_shim(handle)
        # Without the shim the check hits the endpoint as usual (mock client).
        client = AsyncMock()
        client.get.return_value = MagicMock(status_code=200)
        assert await check_service_health(state, client) is True
    finally:
        await shim.stop_shim_for_port(port)
        target.close()


@pytest.mark.asyncio
async def test_health_loop_runs_on_tick_each_round():
    on_tick = AsyncMock()
    stop_event = asyncio.Event()
    with (
        patch("manifold.health.run_health_checks", new_callable=AsyncMock),
        patch("manifold.health.check_service_health", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(
            health_loop(
                PipelineState(services=[]),
                GatewayConfig(),
                interval=0.01,
                stop_event=stop_event,
                on_tick=on_tick,
            )
        )
        for _ in range(500):
            if on_tick.await_count >= 2:
                break
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=2.0)
    assert on_tick.await_count >= 2


@pytest.mark.asyncio
async def test_shim_reconcile_releases_shim_when_owner_respawns(tmp_path):
    """The owner gateway's respawn attempts show up as a changed entry pid —
    release the shim so its next attempt can actually bind."""
    with patch("manifold.paths.PID_DIR", tmp_path):
        port_b = await _free_port()
        state = _svc("b", port_b)
        upstream = "http://127.0.0.1:9999"
        state.identity = registry.compute_service_identity(state.config, upstream)
        state.adopted = True
        state.owner_port = 9100
        registry.write_service_entry(
            {
                "schema_version": registry.SCHEMA_VERSION,
                "identity": state.identity,
                "name": "b",
                "pid": 5555,  # owner respawned: differs from the shim's pid
                "owner_port": 9100,
                "owner_pid": 9101,
            }
        )
        target = await _tcp_server(await _free_port(), _echo_handler)
        handle = await shim.start_shim(port_b, "127.0.0.1", 1, pid_at_start=1111)
        try:
            await _shim_reconcile(PipelineState(services=[state]))
            assert shim.get_shim(port_b) is None
        finally:
            await shim.stop_shim(handle)
            target.close()


@pytest.mark.asyncio
async def test_shim_reconcile_releases_shim_when_entry_gone(tmp_path):
    """The entry vanishing IS a release signal: any gateway's sweep removes a
    dead service's entry, and a held-past-that shim hijacks the port forever —
    the owner's next `up` would abort on a 'non-manifold process' that is
    actually our own shim."""
    with patch("manifold.paths.PID_DIR", tmp_path):
        port_b = await _free_port()
        state = _svc("b", port_b)
        upstream = "http://127.0.0.1:9999"
        state.identity = registry.compute_service_identity(state.config, upstream)
        state.adopted = True
        state.owner_port = 9100
        registry.write_service_entry(
            {
                "schema_version": registry.SCHEMA_VERSION,
                "identity": state.identity,
                "name": "b",
                "pid": 999999999,  # dead corpse pid
                "owner_port": 9100,
                "owner_pid": 999999998,  # dead owner: nothing will respawn
            }
        )
        target = await _tcp_server(await _free_port(), _echo_handler)
        await shim.start_shim(port_b, "127.0.0.1", 1, pid_at_start=999999999)
        try:
            # Any gateway's `up` sweeps the dead entry out from under the shim.
            registry.sweep_stale()
            assert registry.read_service_entry(state.identity) is None

            await _shim_reconcile(PipelineState(services=[state]))
            assert shim.get_shim(port_b) is None
            # The port is free again: a rebinding service (or a fresh `up`)
            # can take it.
            reborn = await _tcp_server(port_b, _echo_handler)
            reborn.close()
        finally:
            await shim.stop_shim_for_port(port_b)
            target.close()


@pytest.mark.asyncio
async def test_shim_reconcile_ttl_backstop_releases_old_shim(tmp_path):
    """No shim outruns the TTL backstop, whatever the registry says — a shim
    that outlived every release signal must not hold the port forever."""
    with patch("manifold.paths.PID_DIR", tmp_path):
        port_old, port_new = await _free_port(), await _free_port()
        state = _svc("b", port_old)
        upstream = "http://127.0.0.1:9999"
        state.identity = registry.compute_service_identity(state.config, upstream)
        state.adopted = True
        live_pid = os.getpid()  # alive: no registry signal ever fires below
        registry.write_service_entry(
            {
                "schema_version": registry.SCHEMA_VERSION,
                "identity": state.identity,
                "name": "b",
                "pid": live_pid,
                "owner_port": 9100,
                "owner_pid": os.getpid(),
            }
        )
        target = await _tcp_server(await _free_port(), _echo_handler)
        old = await shim.start_shim(port_old, "127.0.0.1", 1, pid_at_start=live_pid)
        new = await shim.start_shim(port_new, "127.0.0.1", 1, pid_at_start=live_pid)
        try:
            old.started_at -= 31 * 60  # backdate past the 30-minute TTL
            await _shim_reconcile(PipelineState(services=[state]))
            assert shim.get_shim(port_old) is None  # TTL release
            assert shim.get_shim(port_new) is new  # fresh shim untouched
        finally:
            await shim.stop_shim_for_port(port_old)
            await shim.stop_shim_for_port(port_new)
            target.close()


@pytest.mark.asyncio
async def test_shim_reconcile_keeps_shim_while_owner_has_not_respawned(tmp_path):
    """No release signal fired: the entry still names the very pid the shim
    recorded and that process is alive (e.g. an unhealthy-but-running service
    or a recycled pid).  The shim keeps protecting the hop."""
    with patch("manifold.paths.PID_DIR", tmp_path):
        port_b = await _free_port()
        state = _svc("b", port_b)
        upstream = "http://127.0.0.1:9999"
        state.identity = registry.compute_service_identity(state.config, upstream)
        state.adopted = True
        live_pid = os.getpid()
        registry.write_service_entry(
            {
                "schema_version": registry.SCHEMA_VERSION,
                "identity": state.identity,
                "name": "b",
                "pid": live_pid,  # same pid the shim recorded, and alive
                "owner_port": 9100,
            }
        )
        target = await _tcp_server(await _free_port(), _echo_handler)
        handle = await shim.start_shim(port_b, "127.0.0.1", 1, pid_at_start=live_pid)
        try:
            await _shim_reconcile(PipelineState(services=[state]))
            assert shim.get_shim(port_b) is handle
        finally:
            await shim.stop_shim(handle)
            target.close()


@pytest.mark.asyncio
async def test_shim_reconcile_releases_shim_on_pid_reuse(tmp_path):
    """A recycled pid makes 'entry pid == recorded pid' meaningless: the
    owner respawned into the same pid number, so the pid-changed signal never
    fires.  The recorded pid not being alive is itself the release signal —
    without it the shim holds the port through every respawn attempt."""
    with patch("manifold.paths.PID_DIR", tmp_path):
        port_b = await _free_port()
        state = _svc("b", port_b)
        upstream = "http://127.0.0.1:9999"
        state.identity = registry.compute_service_identity(state.config, upstream)
        state.adopted = True
        state.owner_port = 9100
        recycled = 999999999  # dead: the respawned child died on the held port
        registry.write_service_entry(
            {
                "schema_version": registry.SCHEMA_VERSION,
                "identity": state.identity,
                "name": "b",
                "pid": recycled,  # same number the shim recorded — coincidence
                "owner_port": 9100,
                "owner_pid": 9101,
            }
        )
        target = await _tcp_server(await _free_port(), _echo_handler)
        await shim.start_shim(port_b, "127.0.0.1", 1, pid_at_start=recycled)
        try:
            await _shim_reconcile(PipelineState(services=[state]))
            assert shim.get_shim(port_b) is None
        finally:
            await shim.stop_shim_for_port(port_b)
            target.close()


@pytest.mark.asyncio
async def test_shutdown_pipeline_stops_all_shims(tmp_path):
    """Teardown kills gateway-owned shims (they are ephemeral local state)."""
    with patch("manifold.paths.PID_DIR", tmp_path):
        port = await _free_port()
        target = await _tcp_server(await _free_port(), _echo_handler)
        pipe = PipelineState(services=[_svc("a", port)])
        handle = await shim.start_shim(port, "127.0.0.1", 1)
        try:
            await cli_module._shutdown_pipeline(pipe, gw_port=9000, gw_pid=os.getpid())
            assert shim.get_shim(port) is None
            assert handle.server is not None and not handle.server.is_serving()
        finally:
            await shim.stop_shim_for_port(port)
            target.close()
