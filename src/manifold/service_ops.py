"""Shared-service operations: plan / adopt / spawn / promote.

Both ``cli.py`` (``manifold up``) and ``watcher.py`` (hot-reload) need the
same decisions around registry entries: adopt a running service with the
exact same wiring, promote one whose owner gateway died, or spawn a fresh
copy.  This module holds that logic so neither caller imports the other
(which would create a cycle).

Registry invariants (see SPEC-shared-pipeline.md):
- I1 single writer: only the entry's owner gateway may spawn/kill/restart/patch.
- I6 sharing boundary: adoption only on exact identity match.
- I7 stop on last leave: services stop when their last leasing gateway leaves.
"""

from __future__ import annotations

import asyncio
import logging
import time

import typer

from manifold import paths, process, registry
from manifold.chain import patch_service_config, resolve_command
from manifold.models import ServiceState, ServiceStatus, UpstreamVia

log = logging.getLogger(__name__)

SPAWN_LOCK_RETRIES = 50
SPAWN_LOCK_WAIT = 0.2


def _plan_service(svc, upstream_url: str) -> tuple[str, dict | str | None]:
    """Decide how to handle a service: adopt / promote / spawn / error.

    Returns ``("adopt", entry)`` when a live entry with the identical wiring
    is owned by a live gateway, ``("promote", entry)`` when its owner is dead,
    ``("spawn", None)`` when nothing runs here, or ``("error", msg)`` when
    the port is occupied by something else.  Stale entries are removed as a
    side effect (I2).
    """
    identity = registry.compute_service_identity(svc, upstream_url)
    entry = registry.read_service_entry(identity)
    # "Live" here means the service process itself runs — the owner may be
    # dead (that is exactly the promote case).  entry_is_live() requires the
    # owner too, so it must not gate adopt/promote.
    if entry is not None and registry.pid_alive(entry.get("pid")):
        if registry.pid_alive(entry.get("owner_pid")):
            return ("adopt", entry)
        return ("promote", entry)  # owner died but the process still runs
    if entry is not None:
        log.info("Removing stale entry for '%s'", svc.name)
        registry.remove_service_entry(identity)
    if paths.is_port_in_use(svc.port):
        conflict = registry.find_live_entry_by_port(svc.port)
        if conflict is not None:
            msg = (
                f"Port {svc.port} for service '{svc.name}' is in use by a "
                f"different wiring (identity {conflict['identity']}, "
                f"owner gateway :{conflict.get('owner_port')})"
            )
        else:
            msg = (
                f"Port {svc.port} for service '{svc.name}' is already in use "
                f"by a non-manifold process"
            )
        return ("error", msg)
    return ("spawn", None)


def _adopt_from_entry(state: ServiceState, entry: dict, upstream_url: str) -> None:
    """Mark a running service as adopted.

    Adopted services are read-only from this gateway's point of view (I1):
    we never spawn, kill, restart, or patch them — we only proxy to them and
    report their status.
    """
    state.adopted = True
    state.pid = entry.get("pid")
    state.pgid = entry.get("pgid")
    state.owner_port = entry.get("owner_port")
    state.identity = entry["identity"]
    state.upstream_url = upstream_url
    state.status = ServiceStatus.STARTING
    log.info(
        "Adopting running service '%s' (pid %s, owned by gateway :%s)",
        state.config.name,
        state.pid,
        state.owner_port,
    )


async def _spawn_owned(
    state: ServiceState,
    upstream_url: str,
    identity: str,
    gw_port: int,
    gw_pid: int,
    reclaim_entry: dict | None = None,
) -> bool:
    """Spawn a service and register it as owned by gateway *gw_port*.

    Serializes on the identity's spawn lock (I1).  Returns False when the
    service ended up adopted instead (another gateway spawned it while we
    waited); True when this gateway spawned it.

    *reclaim_entry* is the entry of a promote target — the dead owner's
    process is killed before we take over.
    """
    svc = state.config
    for _ in range(SPAWN_LOCK_RETRIES):
        if registry.acquire_spawn_lock(identity):
            try:
                entry = registry.read_service_entry(identity)
                if entry is not None and registry.entry_is_live(entry):
                    # Another gateway won the race — adopt instead (I1).
                    _adopt_from_entry(state, entry, upstream_url)
                    return False
                if reclaim_entry is not None:
                    # The reclaim target IS the port occupant — the port-busy
                    # check below must not fire for the promote path.
                    log.info(
                        "Reclaiming '%s' from dead owner gateway :%s",
                        svc.name,
                        reclaim_entry.get("owner_port"),
                    )
                    registry.kill_entry_processes(reclaim_entry)
                    registry.remove_service_entry(reclaim_entry["identity"])
                    if paths.is_port_in_use(svc.port):
                        # I4: the kill was skipped (pid/pgid reuse) or the
                        # process survived SIGKILL — never spawn on a port we
                        # could not free.
                        raise typer.Exit(
                            f"Port {svc.port} still occupied after reclaiming "
                            f"'{svc.name}' — not spawning"
                        )
                elif paths.is_port_in_use(svc.port):
                    raise typer.Exit(
                        f"Port {svc.port} taken while spawning '{svc.name}'"
                    )

                state.adopted = False
                state.identity = identity
                # Patching the config is an owner duty (I1): only we may point
                # our own service at its upstream.
                if svc.upstream_via == UpstreamVia.CONFIG_FILE:
                    patch_service_config(svc, upstream_url)
                await process.start_service(state, upstream_url)
                state.owner_port = gw_port
                registry.write_service_entry(
                    {
                        "schema_version": registry.SCHEMA_VERSION,
                        "identity": identity,
                        "name": svc.name,
                        "directory": svc.directory,
                        "command": resolve_command(svc, upstream_url),
                        "port": svc.port,
                        "upstream": upstream_url,
                        "pid": state.pid,
                        "pgid": state.pgid,
                        "owner_port": gw_port,
                        "owner_pid": gw_pid,
                        "started_at": time.time(),
                    }
                )
                return True
            finally:
                registry.release_spawn_lock(identity)
        # Lock held by another gateway — wait for its spawn/adopt to land.
        entry = registry.read_service_entry(identity)
        if entry is not None and registry.entry_is_live(entry):
            _adopt_from_entry(state, entry, upstream_url)
            return False
        await asyncio.sleep(SPAWN_LOCK_WAIT)
    log.warning("Giving up waiting for spawn lock on '%s'", svc.name)
    return False


async def _promote_adopted(state: ServiceState, gw_port: int, gw_pid: int) -> bool:
    """Reclaim an adopted service whose owner gateway died.

    Called via the health-loop hook when an adopted service turns unhealthy.
    Returns True when this gateway spawned a replacement (caller should
    refresh its lease); False when there was nothing to do.
    """
    if state.identity is None:
        return False
    entry = registry.read_service_entry(state.identity)
    if entry is None:
        return False  # nothing to do — service already gone
    if not registry.pid_alive(entry.get("pid")):
        log.info("Removing dead entry for adopted service '%s'", state.config.name)
        registry.remove_service_entry(state.identity)
        return False
    if registry.pid_alive(entry.get("owner_pid")):
        log.warning(
            "Service '%s' is still owned by live gateway :%s — "
            "leaving recovery to the owner",
            state.config.name,
            entry.get("owner_port"),
        )
        return False
    log.info(
        "Promoting '%s': reclaiming from dead owner gateway :%s",
        state.config.name,
        entry.get("owner_port"),
    )
    state.adopted = False
    return await _spawn_owned(
        state,
        state.upstream_url or "",
        state.identity,
        gw_port,
        gw_pid,
        reclaim_entry=entry,
    )


async def _plan_and_start(
    state: ServiceState,
    upstream_url: str,
    gw_port: int,
    gw_pid: int,
) -> None:
    """Plan then start/adopt/promote a service (hot-reload path).

    Errors (port conflicts) are logged, not raised — config reloads keep
    going even when one service cannot be started.
    """
    identity = registry.compute_service_identity(state.config, upstream_url)
    decision, payload = _plan_service(state.config, upstream_url)
    if decision == "error":
        log.error("%s", payload)
        return
    if decision == "adopt":
        _adopt_from_entry(state, payload, upstream_url)
        return
    try:
        await _spawn_owned(
            state,
            upstream_url,
            identity,
            gw_port,
            gw_pid,
            reclaim_entry=payload if decision == "promote" else None,
        )
    except typer.Exit as exc:
        # Hot-reload must never die on a spawn conflict — degrade loudly.
        log.error("Hot-reload spawn failed: %s", exc)
