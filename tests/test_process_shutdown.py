"""Tests for subprocess shutdown helpers."""

import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import manifold.process as mp
from manifold.models import ServiceConfig, ServiceState, ServiceStatus


def _service_state(name: str = "svc") -> ServiceState:
    return ServiceState(
        config=ServiceConfig(
            name=name,
            directory="/tmp",
            command="echo",
            port=17002,
            health="/healthz",
        )
    )


def test_sync_kill_tracked_subprocesses_noop_when_empty():
    with patch.dict(mp._processes, {}, clear=True):
        mp.sync_kill_tracked_subprocesses()  # should not raise


def test_sync_kill_tracked_subprocesses_sends_sigterm_unix_path():
    mock_proc = MagicMock()
    mock_proc.pid = 4242
    mock_proc.returncode = 0
    with (
        patch.object(mp.sys, "platform", "linux"),
        patch.dict(mp._processes, {"svc": mock_proc}, clear=True),
    ):
        with (
            patch("manifold.process.os.getpgid", return_value=99) as gp,
            patch("manifold.process.os.killpg") as kp,
        ):
            mp.sync_kill_tracked_subprocesses()
    gp.assert_called_with(4242)
    assert kp.call_count >= 1
    assert kp.call_args_list[0][0][1] == signal.SIGTERM


def test_sync_kill_tracked_subprocesses_win32_uses_terminate_kill():
    mock_proc = MagicMock()
    mock_proc.pid = 111
    mock_proc.returncode = 0
    with (
        patch.object(mp.sys, "platform", "win32"),
        patch.dict(mp._processes, {"svc": mock_proc}, clear=True),
    ):
        mp.sync_kill_tracked_subprocesses()
    mock_proc.terminate.assert_called_once()
    # returncode already set → kill should not be required; may still be checked
    assert mock_proc.returncode == 0


@pytest.mark.asyncio
async def test_start_service_sets_pgid_and_resets_adopted():
    fake_proc = MagicMock()
    fake_proc.pid = 4242
    state = _service_state()
    state.adopted = True  # start_service must reset this: a fresh spawn is owned
    with (
        patch.object(mp.sys, "platform", "linux"),
        patch(
            "manifold.process.asyncio.create_subprocess_shell",
            new_callable=AsyncMock,
            return_value=fake_proc,
        ) as spawn,
        patch("manifold.process.os.getpgid", return_value=4242) as gp,
        patch("manifold.process.setup_service_log", return_value=None),
        patch(
            "manifold.process.asyncio.create_task",
            side_effect=lambda coro: coro.close(),
        ),
        patch.dict(mp._processes, {}, clear=True),
    ):
        await mp.start_service(state, "http://upstream:8000")
        spawn.assert_awaited_once()
        gp.assert_called_with(4242)
        assert state.pgid == 4242
        assert state.adopted is False
        assert mp._processes["svc"] is fake_proc


@pytest.mark.asyncio
async def test_start_service_pgid_lookup_failure_clears_pgid():
    fake_proc = MagicMock()
    fake_proc.pid = 4242
    state = _service_state()
    with (
        patch.object(mp.sys, "platform", "linux"),
        patch(
            "manifold.process.asyncio.create_subprocess_shell",
            new_callable=AsyncMock,
            return_value=fake_proc,
        ),
        patch(
            "manifold.process.os.getpgid",
            side_effect=ProcessLookupError,
        ),
        patch("manifold.process.setup_service_log", return_value=None),
        patch(
            "manifold.process.asyncio.create_task",
            side_effect=lambda coro: coro.close(),
        ),
        patch.dict(mp._processes, {}, clear=True),
    ):
        await mp.start_service(state, "http://upstream:8000")
    assert state.pgid is None


@pytest.mark.asyncio
async def test_stop_service_adopted_guard_returns_without_kill():
    state = _service_state()
    state.adopted = True
    state.pid = 4242
    with (
        patch.dict(mp._processes, {"svc": MagicMock()}, clear=True),
        patch(
            "manifold.process.os.killpg",
            side_effect=AssertionError("killpg must not be called"),
        ) as kp,
        patch("manifold.process.os.getpgid"),
    ):
        await mp.stop_service(state)
    kp.assert_not_called()
    assert state.status == ServiceStatus.STOPPED
    assert state.pid is None


@pytest.mark.asyncio
async def test_stop_service_clears_pgid_after_kill():
    fake_proc = MagicMock()
    fake_proc.pid = 4242
    fake_proc.wait = AsyncMock(return_value=0)
    state = _service_state()
    state.pid = 4242
    state.pgid = 4242
    with (
        patch.object(mp.sys, "platform", "linux"),
        patch.dict(mp._processes, {"svc": fake_proc}, clear=True),
        patch("manifold.process.os.getpgid", return_value=4242),
        patch("manifold.process.os.killpg"),
        patch.dict(mp._log_tasks, {}, clear=True),
    ):
        await mp.stop_service(state)
    assert state.pid is None
    assert state.pgid is None


@pytest.mark.asyncio
async def test_release_service_pops_without_kill_and_keeps_forwarders():
    state = _service_state()
    state.adopted = True
    state.pid = 4242
    state.pgid = 4242
    fake_proc = MagicMock()
    t1, t2 = MagicMock(), MagicMock()
    with (
        patch.dict(mp._processes, {"svc": fake_proc}, clear=True),
        patch.dict(mp._log_tasks, {"svc": (t1, t2)}, clear=True),
        patch(
            "manifold.process.os.killpg",
            side_effect=AssertionError("killpg must not be called"),
        ) as kp,
        patch("manifold.process.os.getpgid"),
    ):
        await mp.release_service(state)
        kp.assert_not_called()
        assert mp._processes == {}
        assert mp._log_tasks == {}
        # forwarder tasks must NOT be cancelled — pipes keep draining until
        # the child exits; cancellation would fill its pipe buffer.
        t1.cancel.assert_not_called()
        t2.cancel.assert_not_called()
        assert state.status == ServiceStatus.STOPPED
        assert state.pid is None
        assert state.pgid is None
