"""Tests for subprocess shutdown helpers."""

import signal
from unittest.mock import MagicMock, patch

import manifold.process as mp


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
