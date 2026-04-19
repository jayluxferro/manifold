"""Subprocess management for pipeline services."""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import signal
import sys
import time
from collections.abc import Callable

from manifold.chain import resolve_command
from manifold.logs import setup_service_log
from manifold.models import ServiceState, ServiceStatus

log = logging.getLogger(__name__)

# Tracks running subprocesses keyed by service name
_processes: dict[str, asyncio.subprocess.Process] = {}

# Log-forwarder tasks per service (cancelled when the service stops)
_log_tasks: dict[str, tuple[asyncio.Task[None], asyncio.Task[None]]] = {}

# Optional callback invoked when a service crashes (set by orchestrator)
_on_crash: Callable[[ServiceState], None] | None = None

# Whether the atexit handler has been registered
_atexit_registered: bool = False


def _use_killpg() -> bool:
    """Unix: whole process group via killpg. Windows: terminate/kill top process only."""
    return sys.platform != "win32" and hasattr(os, "killpg")


def set_on_crash(callback: Callable[[ServiceState], None] | None) -> None:
    """Register a callback to be invoked when a service crashes unexpectedly."""
    global _on_crash
    _on_crash = callback


async def _cancel_log_tasks(name: str) -> None:
    """Cancel stdout/stderr forwarders for a service and wait for them to finish."""
    pair = _log_tasks.pop(name, None)
    if not pair:
        return
    for t in pair:
        if not t.done():
            t.cancel()
    await asyncio.gather(*pair, return_exceptions=True)


async def start_service(
    state: ServiceState,
    upstream_url: str,
) -> None:
    """Start a service subprocess."""
    global _atexit_registered
    if not _atexit_registered:
        atexit.register(sync_kill_tracked_subprocesses)
        _atexit_registered = True

    svc = state.config
    cmd = resolve_command(svc, upstream_url)
    log.info("Starting %s: %s (cwd=%s)", svc.name, cmd, svc.directory)

    state.status = ServiceStatus.STARTING
    state.upstream_url = upstream_url

    sub_kw: dict = {
        "cwd": svc.directory,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if _use_killpg():
        sub_kw["start_new_session"] = True

    proc = await asyncio.create_subprocess_shell(cmd, **sub_kw)
    _processes[svc.name] = proc
    state.pid = proc.pid

    # Set up per-service file logger
    svc_logger = setup_service_log(svc.name)

    # Launch log forwarders (to both console and file)
    tout = asyncio.create_task(
        _stream_output(svc.name, proc.stdout, "stdout", svc_logger)
    )
    terr = asyncio.create_task(
        _stream_output(svc.name, proc.stderr, "stderr", svc_logger)
    )
    _log_tasks[svc.name] = (tout, terr)

    # Monitor for unexpected exit
    asyncio.create_task(_watch_exit(state, proc))


async def _stream_output(
    name: str,
    stream: asyncio.StreamReader,
    label: str,
    svc_logger: logging.Logger | None = None,
) -> None:
    """Forward subprocess output to the manifold logger and per-service log file."""
    while True:
        line = await stream.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").rstrip()
        log.info("[%s/%s] %s", name, label, text)
        if svc_logger:
            svc_logger.info("[%s] %s", label, text)


async def _watch_exit(state: ServiceState, proc: asyncio.subprocess.Process) -> None:
    """Watch for a subprocess to exit unexpectedly."""
    name = state.config.name
    try:
        code = await proc.wait()
        if state.status != ServiceStatus.STOPPED:
            log.warning("%s exited with code %s", name, code)
            state.status = ServiceStatus.UNHEALTHY
            state.pid = None
            if _on_crash is not None:
                try:
                    _on_crash(state)
                except Exception:
                    log.exception("on_crash callback failed for %s", name)
    finally:
        _processes.pop(name, None)
        await _cancel_log_tasks(name)


async def stop_service(state: ServiceState) -> None:
    """Gracefully stop a service subprocess and its entire process group."""
    name = state.config.name
    proc = _processes.get(name)
    if proc is None:
        state.status = ServiceStatus.STOPPED
        state.pid = None
        await _cancel_log_tasks(name)
        return

    log.info("Stopping %s (pid=%s)", name, proc.pid)
    state.status = ServiceStatus.STOPPED

    try:
        if _use_killpg() and proc.pid is not None:
            # Kill the entire process group (shell + children) rather than
            # just the shell process, which would leave children as zombies.
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("%s did not stop gracefully, killing process group", name)
                os.killpg(pgid, signal.SIGKILL)
                await proc.wait()
        elif proc.pid is not None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("%s did not stop gracefully, killing process", name)
                proc.kill()
                await proc.wait()
    except ProcessLookupError:
        pass
    except OSError as exc:
        # Fallback: process group may already be gone
        log.debug("OS error stopping %s: %s", name, exc)

    state.pid = None
    _processes.pop(name, None)
    await _cancel_log_tasks(name)
    log.info("%s stopped", name)


async def stop_all(services: list[ServiceState]) -> None:
    """Stop all running services in reverse order."""
    for state in reversed(services):
        await stop_service(state)


def sync_kill_tracked_subprocesses() -> None:
    """SIGTERM then SIGKILL tracked pipeline children (sync).

    Unix uses process groups. Windows uses terminate/kill on the shell process only
    (child processes may survive if they detached — prefer Unix for full pipelines).
    """
    if not _processes:
        return
    if _use_killpg():
        for _, proc in list(_processes.items()):
            pid = proc.pid
            if pid is None:
                continue
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        grace_deadline = time.monotonic() + 5.0
        while time.monotonic() < grace_deadline:
            if all(
                p.returncode is not None
                for p in _processes.values()
                if p.pid is not None
            ):
                break
            time.sleep(0.05)
        for _, proc in list(_processes.items()):
            pid = proc.pid
            if pid is None:
                continue
            try:
                if proc.returncode is None:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        return

    for _, proc in list(_processes.items()):
        if proc.pid is None:
            continue
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            pass
    grace_deadline = time.monotonic() + 5.0
    while time.monotonic() < grace_deadline:
        if all(
            p.returncode is not None for p in _processes.values() if p.pid is not None
        ):
            break
        time.sleep(0.05)
    for _, proc in list(_processes.items()):
        if proc.pid is None:
            continue
        try:
            if proc.returncode is None:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass


async def restart_service(state: ServiceState, upstream_url: str) -> None:
    """Restart a service with a potentially new upstream."""
    await stop_service(state)
    await asyncio.sleep(0.5)
    await start_service(state, upstream_url)


def is_running(state: ServiceState) -> bool:
    """Check if a service subprocess is still alive."""
    proc = _processes.get(state.config.name)
    if proc is None:
        return False
    return proc.returncode is None
