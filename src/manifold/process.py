"""Subprocess management for pipeline services."""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable

from manifold import shim
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

# Per-service lifecycle locks.  Nothing used to serialize two start_service
# calls on one service: a crash auto-restart racing a hot-reload restart
# spawned two children, _processes[name] kept only the second, and the first
# child was orphaned from tracking — never killed at shutdown.  Tests can
# patch _service_locks (fresh dict per test) or _service_lock itself.
_service_locks: dict[str, asyncio.Lock] = {}

# Which task holds which lifecycle lock.  asyncio.Lock is not reentrant, but
# restart_service must call the PUBLIC stop/start (so test patches and future
# overrides apply) while already holding the lock for atomicity — same-task
# reentry skips the acquisition; a genuinely concurrent task still waits.
_held_locks: dict[int, set[str]] = {}


def _use_killpg() -> bool:
    """Unix: whole process group via killpg. Windows: terminate/kill top process only."""
    return sys.platform != "win32" and hasattr(os, "killpg")


def _service_lock(name: str) -> asyncio.Lock:
    """The lifecycle lock for service *name* (created on first use)."""
    lock = _service_locks.get(name)
    if lock is None:
        lock = asyncio.Lock()
        _service_locks[name] = lock
    return lock


async def _run_locked(name: str, fn: Callable[[], Awaitable[None]]) -> None:
    """Run *fn* under *name*'s lifecycle lock (reentrant per asyncio task)."""
    task = asyncio.current_task()
    held = _held_locks.setdefault(id(task), set()) if task is not None else set()
    if name in held:
        await fn()
        return
    async with _service_lock(name):
        held.add(name)
        try:
            await fn()
        finally:
            held.discard(name)
            if not held and task is not None:
                _held_locks.pop(id(task), None)


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
    """Start a service subprocess (serialized per service name)."""
    await _run_locked(state.config.name, lambda: _start_service(state, upstream_url))


async def _start_service(
    state: ServiceState,
    upstream_url: str,
) -> None:
    """Spawn the child and start tracking it.  Caller holds the service lock."""
    global _atexit_registered
    if not _atexit_registered:
        atexit.register(sync_kill_tracked_subprocesses)
        _atexit_registered = True

    # Under the lock: another spawn path (crash auto-restart vs hot-reload)
    # may have spawned a live child for this name while we waited.  Spawning
    # again would orphan the first child from tracking (never killed at
    # shutdown), so the loser adopts the running child instead.  A live
    # tracked child also means the port is bound by it — no shim can exist.
    existing = _processes.get(state.config.name)
    if existing is not None and existing.returncode is None:
        log.info(
            "%s already has a live child (pid %s) from a concurrent start — "
            "not spawning again",
            state.config.name,
            existing.pid,
        )
        state.adopted = False  # a tracked child is always ours
        state.status = ServiceStatus.STARTING
        state.pid = existing.pid
        return

    # A crash shim may still hold this port (it forwards traffic to the next
    # live service while the real one is down).  The shim must lose the port
    # before the real service rebinds — doing it here covers every spawn path
    # (crash restart, hot-reload restart/enable, adoption promotion), so no
    # caller can race the shim's listener.
    await shim.stop_shim_for_port(state.config.port)

    # A fresh spawn is always owned by this gateway — never adopted (I1)
    state.adopted = False
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

    if _use_killpg() and proc.pid is not None:
        try:
            state.pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            state.pgid = None

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
        # service_name lets the console formatter color the line per service;
        # the file logger below never sees ANSI codes.
        log.info("[%s/%s] %s", name, label, text, extra={"service_name": name})
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
    # never kill a process another gateway owns (I1); nothing shared is
    # mutated, so this early return needs no lock
    if state.adopted:
        state.status = ServiceStatus.STOPPED
        state.pid = None
        return

    await _run_locked(state.config.name, lambda: _stop_service(state))


async def _stop_service(state: ServiceState) -> None:
    """Kill the child and drop tracking.  Caller holds the service lock."""
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
    state.pgid = None
    _processes.pop(name, None)
    await _cancel_log_tasks(name)
    log.info("%s stopped", name)


async def release_service(state: ServiceState) -> None:
    """Hand off ownership of a running service without killing it.

    Another gateway still owns this process (I1): we only drop our tracking.
    The log-forwarder tasks keep draining the pipes until the child exits —
    cancelling them would fill the child's pipe buffer and stall it on its
    next write.
    """
    name = state.config.name
    _processes.pop(name, None)
    # Keep pipes draining until the child exits; cancelling would fill the
    # child's pipe buffer and block it on its next write.
    _log_tasks.pop(name, None)
    state.status = ServiceStatus.STOPPED
    state.pid = None
    state.pgid = None


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
    """Restart a service with a potentially new upstream.

    ONE lock acquisition spans the stop and the start: two separate
    acquisitions would let restart∥restart interleave as stop,stop,start,
    start — two spawns for one service again.  The public stop/start are
    called (not the internals) so patches/overrides apply; the lock is
    reentrant per task, so the nested acquisitions are no-ops here.
    """
    await _run_locked(
        state.config.name, lambda: _restart_unrolled(state, upstream_url)
    )


async def _restart_unrolled(state: ServiceState, upstream_url: str) -> None:
    await stop_service(state)
    await asyncio.sleep(0.5)
    await start_service(state, upstream_url)


def is_running(state: ServiceState) -> bool:
    """Check if a service subprocess is still alive."""
    proc = _processes.get(state.config.name)
    if proc is None:
        return False
    return proc.returncode is None
