"""Subprocess management for pipeline services."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Callable

from manifold.chain import resolve_command
from manifold.logs import setup_service_log
from manifold.models import ServiceConfig, ServiceState, ServiceStatus

log = logging.getLogger(__name__)

# Tracks running subprocesses keyed by service name
_processes: dict[str, asyncio.subprocess.Process] = {}

# Optional callback invoked when a service crashes (set by orchestrator)
_on_crash: Callable[[ServiceState], None] | None = None


def set_on_crash(callback: Callable[[ServiceState], None] | None) -> None:
    """Register a callback to be invoked when a service crashes unexpectedly."""
    global _on_crash
    _on_crash = callback


async def start_service(
    state: ServiceState,
    upstream_url: str,
) -> None:
    """Start a service subprocess."""
    svc = state.config
    cmd = resolve_command(svc, upstream_url)
    log.info("Starting %s: %s (cwd=%s)", svc.name, cmd, svc.directory)

    state.status = ServiceStatus.STARTING
    state.upstream_url = upstream_url

    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=svc.directory,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # create process group so we can kill children
    )
    _processes[svc.name] = proc
    state.pid = proc.pid

    # Set up per-service file logger
    svc_logger = setup_service_log(svc.name)

    # Launch log forwarders (to both console and file)
    asyncio.create_task(_stream_output(svc.name, proc.stdout, "stdout", svc_logger))
    asyncio.create_task(_stream_output(svc.name, proc.stderr, "stderr", svc_logger))

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
    code = await proc.wait()
    name = state.config.name
    if state.status != ServiceStatus.STOPPED:
        log.warning("%s exited with code %s", name, code)
        state.status = ServiceStatus.UNHEALTHY
        state.pid = None
        if _on_crash is not None:
            try:
                _on_crash(state)
            except Exception:
                log.exception("on_crash callback failed for %s", name)
    _processes.pop(name, None)


async def stop_service(state: ServiceState) -> None:
    """Gracefully stop a service subprocess and its entire process group."""
    name = state.config.name
    proc = _processes.get(name)
    if proc is None:
        state.status = ServiceStatus.STOPPED
        state.pid = None
        return

    log.info("Stopping %s (pid=%s)", name, proc.pid)
    state.status = ServiceStatus.STOPPED

    try:
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
    except ProcessLookupError:
        pass
    except OSError as exc:
        # Fallback: process group may already be gone
        log.debug("OS error stopping %s: %s", name, exc)

    state.pid = None
    _processes.pop(name, None)
    log.info("%s stopped", name)


async def stop_all(services: list[ServiceState]) -> None:
    """Stop all running services in reverse order."""
    for state in reversed(services):
        await stop_service(state)


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
