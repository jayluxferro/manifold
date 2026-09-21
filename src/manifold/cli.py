"""Typer CLI — manifold up / down / status / stats / add."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

import httpx
import typer
import uvicorn
import yaml

from manifold import paths, process, registry, shim
from manifold import service_ops
from manifold.chain import (
    compute_upstreams,
    get_entry_route,
    get_entry_url,
    patch_service_config,
    resolve_command,
    rewire_around,
)
from manifold.config import ConfigError, find_config, load_config
from manifold.gateway import create_app
from manifold.logs import ServiceColorFormatter, console_supports_color
from manifold.health import (
    health_loop,
    wait_for_services_ready,
    StartupHealthTimeoutError,
)
from manifold.shim import ShimHandle
from manifold.watcher import watch_config
from manifold.models import PipelineState, ServiceState, ServiceStatus, UpstreamVia
from manifold.stats import aggregate_stats

app = typer.Typer(
    name="manifold",
    help="Proxy mesh gateway — chain LLM proxy services into a single pipeline.",
    add_completion=False,
)

log = logging.getLogger("manifold")

# Auto-restart backoff for crashed services (exponential, capped).  Module
# level so tests can shrink the base delay instead of waiting it out.
_BASE_RESTART_DELAY = 2.0
_MAX_RESTART_DELAY = 60.0


def _is_entry_service(pipeline: PipelineState, state: ServiceState) -> bool:
    """True when *state* is the first enabled service (the gateway's entry hop)."""
    for s in pipeline.services:
        if not s.config.enabled:
            continue
        return s is state
    return False


def _shim_target_for(
    pipeline: PipelineState, state: ServiceState
) -> ServiceState | None:
    """The next live enabled service strictly after *state*.

    Same next-live rule as the crash-restart upstream computation (the c1
    fix): services that are STOPPED or UNHEALTHY don't receive traffic, so
    they are skipped.  Returns None when nothing live exists downstream — a
    shim has nothing to forward into (the https fallback is unreachable from
    a raw TCP forward: that side speaks TLS).
    """
    seen = False
    for s in pipeline.services:
        if not s.config.enabled:
            continue
        if s is state:
            seen = True
            continue
        if seen and s.status not in (ServiceStatus.STOPPED, ServiceStatus.UNHEALTHY):
            return s
    return None


async def _maybe_start_shim(
    pipeline: PipelineState, state: ServiceState
) -> ShimHandle | None:
    """Shim a down service's port to the next live service (mid-chain bypass).

    Skips the entry hop: get_entry_url re-resolves on every proxy request, so
    the gateway already bypasses a dead first service per request — a shim
    would be redundant.  Returns None (today's behavior) when there is no
    shimmable situation: entry hop, no live downstream, port still bound (a
    hung process keeps its listener — the shim cannot intercept it), or a
    shim already owns the port.
    """
    if not state.config.enabled:
        return None
    if _is_entry_service(pipeline, state):
        return None
    target = _shim_target_for(pipeline, state)
    if target is None:
        return None
    try:
        return await shim.start_shim(
            state.config.port,
            "127.0.0.1",
            target.config.port,
            pid_at_start=state.pid,
        )
    except RuntimeError:
        return None  # a shim already owns this port — nothing to do
    except OSError as exc:
        log.info(
            "No shim for '%s' on port %d: %s", state.config.name, state.config.port, exc
        )
        return None


def _live_shims(cfg) -> dict[str, str]:
    """Ask the running gateway which services are currently shimmed.

    Shims are ephemeral gateway state (not config or registry facts), so the
    only honest source is the live /_manifold/config.  Unreachable gateway →
    no shim info.
    """
    addr = f"{cfg.gateway.host}:{cfg.gateway.port}"
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"http://{addr}/_manifold/config")
        if resp.status_code >= 400:
            return {}
        return {
            s["name"]: s.get("shim_target") or ""
            for s in resp.json().get("pipeline", [])
            if s.get("shim")
        }
    except (httpx.HTTPError, ValueError):
        return {}


# Reconcile's TTL backstop: no shim may outlive this, whatever the registry
# says.  Shims are silent function loss (a shimmed redactor scrubs nothing),
# so a shim that outlived every release signal must not hold the port
# forever.  Dead services restart or get swept well inside this window; if
# none of that happened, traffic degrades LOUDLY (connection refused) instead
# of silently bypassing the dead layer indefinitely.
_SHIM_MAX_TTL_SECONDS = 30 * 60.0


async def _shim_reconcile(pipeline: PipelineState) -> None:
    """Release crash shims whose release signal has fired.

    Shims on ADOPTED services are owned by registry signals (owned services
    manage their own shims via the spawn path's choke point):

    - TTL backstop: any shim — adopted or not — older than
      ``_SHIM_MAX_TTL_SECONDS`` is released loudly.  This is the last line of
      defense against a permanent port hijack: a released shim can only ever
      cost availability, while a stuck shim silently loses the layer's
      function AND blocks the owner's respawn.
    - entry gone: sweeps remove the entry once the service pid is dead, and
      removals mean nobody claims the service anymore.  Holding the shim past
      this point hijacks the port — the owner's next ``up`` would abort on
      "port in use by a non-manifold process".  The entry vanishing IS a
      release signal.
    - entry pid changed: the owner gateway's respawn attempts refresh the
      entry with each try; a new pid means the next attempt needs the port.
    - recorded pid dead again: an entry pid equal to the shim's recorded pid
      is weak evidence (pids get recycled), so it is checked for liveness —
      a dead pid means the entry names a process that does not exist.
    """
    for port, handle in shim.all_shims().items():
        if handle.age_seconds >= _SHIM_MAX_TTL_SECONDS:
            log.warning(
                "Shim on port %d (→ %s) exceeded its %ds TTL — releasing it "
                "regardless of state. If the real service does not rebind, "
                "traffic to this port now fails visibly instead of bypassing "
                "the dead layer silently.",
                port,
                f"{handle.target_host}:{handle.target_port}",
                int(_SHIM_MAX_TTL_SECONDS),
            )
            await shim.stop_shim(handle)

    for state in pipeline.services:
        if not state.adopted or state.identity is None:
            continue
        handle = shim.get_shim(state.config.port)
        if handle is None or handle.pid_at_start is None:
            continue
        try:
            entry = registry.read_service_entry(state.identity)
        except Exception:
            log.exception(
                "Shim reconcile: failed to read entry for '%s'", state.config.name
            )
            continue
        if entry is None:
            log.info(
                "Registry entry for adopted service '%s' is gone (swept or "
                "removed) — releasing shim on port %d so the owner's next "
                "respawn can rebind",
                state.config.name,
                state.config.port,
            )
            await shim.stop_shim(handle)
            continue
        entry_pid = entry.get("pid")
        if entry_pid is not None and entry_pid != handle.pid_at_start:
            log.info(
                "Owner gateway restarted '%s' (new pid %s) — releasing shim on "
                "port %d so the real service can rebind",
                state.config.name,
                entry_pid,
                state.config.port,
            )
            await shim.stop_shim(handle)
        elif not registry.pid_alive(handle.pid_at_start):
            # Pid-reuse guard: an entry pid equal to the shim's recorded pid
            # proves nothing — the owner's respawned child may have been
            # recycled into the same pid number as the corpse.  A recorded
            # pid that is not alive means the process the entry names does
            # not exist, so no live service is being protected by holding
            # the port; release it for the owner's next attempt (the TTL
            # backstop below remains the final safety net).
            log.info(
                "Entry pid for adopted service '%s' matches the shim's "
                "recorded pid %s but that process is not alive (pid reuse?) "
                "— releasing shim on port %d",
                state.config.name,
                handle.pid_at_start,
                state.config.port,
            )
            await shim.stop_shim(handle)


def _maybe_prompt_gateway_startup_health(raw: dict) -> None:
    """Optionally merge gateway.startup_health_* keys into *raw* (mutates in place)."""
    if not typer.confirm("Configure gateway startup health options?", default=False):
        return

    gwy = raw.get("gateway")
    if not isinstance(gwy, dict):
        gwy = {}
        raw["gateway"] = gwy

    while True:
        timeout = typer.prompt(
            "startup_health_timeout (seconds)",
            default=int(gwy.get("startup_health_timeout", 120)),
            type=int,
        )
        poll = typer.prompt(
            "startup_health_poll_interval (seconds)",
            default=float(gwy.get("startup_health_poll_interval", 0.25)),
            type=float,
        )
        if timeout <= 0 or poll <= 0:
            typer.echo(
                "Values must be positive — not saving startup health options.", err=True
            )
            return
        if poll > timeout:
            typer.echo(
                "startup_health_poll_interval must be <= startup_health_timeout — try again."
            )
            continue
        strict = typer.confirm(
            "startup_health_strict (fail `manifold up` if services never become healthy)?",
            default=bool(gwy.get("startup_health_strict", False)),
        )
        gwy["startup_health_timeout"] = timeout
        gwy["startup_health_poll_interval"] = poll
        gwy["startup_health_strict"] = strict
        break


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(
        ServiceColorFormatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
            use_color=console_supports_color(),
        )
    )
    logging.basicConfig(level=level, handlers=[handler])


def _apply_port_override(
    cfg, port_override: int | None, isolated: bool = False
) -> None:
    """Shift ports when ``--port`` is given.

    Shared mode: only the gateway port moves — services are shared by
    identity with other instances, so their ports must stay put.  Isolated
    mode restores the legacy delta-offset: the gateway AND every service
    port shift by the same delta so this instance owns a private copy of
    the whole chain.
    """
    if port_override is None or port_override == cfg.gateway.port:
        return
    delta = port_override - cfg.gateway.port
    log.info(
        "Port override: gateway %d → %d (delta %+d)%s",
        cfg.gateway.port,
        port_override,
        delta,
        " — isolated: service ports offset too"
        if isolated
        else " — shared: gateway only",
    )
    cfg.gateway.port = port_override
    if isolated:
        for svc in cfg.pipeline:
            svc.port = svc.port + delta


def _preflight_check(cfg, shared: bool = False) -> list[str]:
    """Validate the loaded config and environment before starting anything.

    Returns a list of non-fatal warnings.  Raises :exc:`typer.Exit` on
    hard failures so the pipeline never starts in a broken state.

    In *shared* mode a service port that is already in use is only an error
    when no live registry entry with the matching identity exists — the same
    wiring may be adopted (I6); a different wiring is a hard collision.  The
    gateway port is a hard error in both modes.
    """
    warnings: list[str] = []

    # --- directories -------------------------------------------------------
    for svc in cfg.pipeline:
        if not svc.enabled:
            continue
        d = Path(svc.directory)
        if not d.is_dir():
            warnings.append(f"Service '{svc.name}': directory does not exist: {d}")
        elif svc.upstream_via == UpstreamVia.CONFIG_FILE and svc.config_file:
            cf = d / svc.config_file
            if not cf.is_file():
                warnings.append(f"Service '{svc.name}': config file not found: {cf}")

    # --- port collisions ---------------------------------------------------
    if paths.is_port_in_use(cfg.gateway.port, cfg.gateway.host):
        log.error("Gateway port %d is already in use", cfg.gateway.port)
        raise typer.Exit(1)

    if shared:
        upstreams = compute_upstreams(cfg.pipeline, cfg.gateway.fallback_upstream)
        for svc in cfg.pipeline:
            if not svc.enabled:
                continue
            if not paths.is_port_in_use(svc.port):
                continue
            identity = registry.compute_service_identity(
                svc, upstreams.get(svc.name, cfg.gateway.fallback_upstream)
            )
            entry = registry.read_service_entry(identity)
            # Same rule as _plan_service: a running service process with the
            # identical wiring is adoptable (or promotable if its owner died).
            if entry is not None and registry.pid_alive(entry.get("pid")):
                continue  # identical wiring already running — adoption OK (I6)
            conflict = registry.find_live_entry_by_port(svc.port)
            if conflict is not None:
                log.error(
                    "Port %d for service '%s' is already in use by a different "
                    "wiring (identity %s…, owner gateway :%s)",
                    svc.port,
                    svc.name,
                    identity[:12],
                    conflict.get("owner_port"),
                )
            else:
                # A foreign gateway's shim cannot be distinguished from a
                # normal process bind here — but the live gateways will tell
                # us if one of them holds a shim on this port, and that case
                # resolves itself (reconcile releases it) instead of being a
                # permanent conflict.  Say so instead of the misleading
                # "non-manifold process".
                shim_gw = service_ops.find_live_shim_owner(svc.port)
                if shim_gw is not None:
                    log.error(
                        "Port %d for service '%s' is held by a crash shim from "
                        "gateway :%d — it releases when that gateway reconciles "
                        "(or at its shim TTL); retry shortly, or stop that gateway",
                        svc.port,
                        svc.name,
                        shim_gw,
                    )
                else:
                    log.error(
                        "Port %d for service '%s' is already in use by a "
                        "non-manifold process (possibly a crash shim from an "
                        "unreachable gateway)",
                        svc.port,
                        svc.name,
                    )
            raise typer.Exit(1)
    else:
        service_ports = {s.name: s.port for s in cfg.pipeline if s.enabled}
        collisions = paths.check_port_collisions(
            cfg.gateway.port, service_ports, cfg.gateway.host
        )
        if collisions:
            for msg in collisions:
                log.error(msg)
            enabled_count = len(service_ports)
            suggested = cfg.gateway.port + enabled_count + 1
            log.error(
                "Port collision detected — try --port %d or higher to avoid conflicts",
                suggested,
            )
            raise typer.Exit(1)

    # --- startup health sanity ---------------------------------------------
    if cfg.gateway.startup_health_poll_interval > cfg.gateway.startup_health_timeout:
        raise typer.Exit(
            f"startup_health_poll_interval ({cfg.gateway.startup_health_poll_interval}s)"
            f" must be <= startup_health_timeout ({cfg.gateway.startup_health_timeout}s)"
        )

    return warnings


async def _run_pipeline(
    config_path: str | None,
    verbose: bool,
    port_override: int | None = None,
    isolated: bool = False,
) -> None:
    """Core async logic for 'manifold up'."""
    _setup_logging(verbose)

    try:
        resolved_config_path = find_config(config_path)
        cfg = load_config(config_path)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        raise typer.Exit(1)

    # Port override: shared mode moves only the gateway port; --isolated
    # offsets every service port too (legacy full-duplicate behavior).
    _apply_port_override(cfg, port_override, isolated)

    # Clear orphaned registry state before planning: dead entries, leases of
    # dead gateways, and orphans whose owner died (I2/I7).
    registry.sweep_stale()

    # Pre-flight validation — catch misconfiguration before starting any
    # subprocess so the user sees a clear error instead of a cryptic 502.
    log.info("Validating configuration...")
    warnings = _preflight_check(cfg, shared=not isolated)
    for w in warnings:
        log.warning(w)
    enabled = [s.name for s in cfg.pipeline if s.enabled]
    log.info(
        "Config valid: %d service(s) enabled (%s)",
        len(enabled),
        ", ".join(enabled),
    )

    pipeline = PipelineState(
        services=[ServiceState(config=svc) for svc in cfg.pipeline]
    )

    gw_port = cfg.gateway.port
    gw_pid = os.getpid()

    # Register crash callback: rewire chain, then schedule auto-restart
    _restart_delays: dict[str, float] = {}
    # Set by the signal handler below — an exit while True is OUR teardown
    # SIGTERMing the children, not a crash: log it quietly and never
    # rewire/restart (the user's 08:01 log showed every service "crashed —
    # rewiring to bypass / will auto-restart" during a plain shutdown).
    _shutting_down = False

    def _handle_crash(state: ServiceState) -> None:
        if _shutting_down:
            log.info("Service '%s' exited during shutdown", state.config.name)
            return
        if state.adopted:
            # I1: kill/restart of an adopted service belongs to its owner
            # gateway.  But our chain must still route around the corpse, so
            # do the bookkeeping rewire (in-memory upstreams + statuses) and
            # leave recovery to the owner.  No registry writes: the entry
            # describes the owner's process, and the owner's own crash path
            # or a sweep removes it (I2).
            log.warning(
                "Adopted service '%s' (owner gateway :%s) crashed — rewiring "
                "around it; recovery belongs to its owner",
                state.config.name,
                state.owner_port,
            )
            rewire_around(pipeline, cfg.gateway)
            return
        name = state.config.name
        log.warning("Service '%s' crashed — rewiring chain to bypass it", name)
        rewire_around(pipeline, cfg.gateway)

        # Schedule auto-restart with exponential backoff
        if not state.config.enabled:
            return
        delay = _restart_delays.get(name, _BASE_RESTART_DELAY)
        _restart_delays[name] = min(delay * 2, _MAX_RESTART_DELAY)
        log.info("Will auto-restart '%s' in %.1fs", name, delay)

        async def _do_restart():
            # Protect traffic for the whole backoff window (and beyond, if
            # restarts keep failing): shim the dead port to the next live
            # service.  Started BEFORE the sleep so the shim start and the
            # restart are ordered within this one task — no bind race between
            # them; process.start_service stops the shim before respawning.
            # If restarts keep failing the shim bridges until the TTL
            # backstop (30 min) degrades loudly — an ADOPTED corpse's shim
            # is released within one health tick by the entry-gone /
            # dead-pid rules; nothing re-shims it (owner's job).
            await _maybe_start_shim(pipeline, state)
            await asyncio.sleep(delay)
            if state.status == ServiceStatus.STOPPED:
                return  # user explicitly stopped it
            # Reconstruct the chain the way the health loop's bypass sees it
            # (live services only), but keep the crashed service in its own
            # slot: its upstream must be the next LIVE service after it.  The
            # old code computed from the full enabled list and happily
            # restarted into a dependency that was itself still dead.
            live = [
                s.config
                for s in pipeline.services
                if s.config.enabled
                and (
                    s is state
                    or s.status not in (ServiceStatus.STOPPED, ServiceStatus.UNHEALTHY)
                )
            ]
            upstreams = compute_upstreams(live, cfg.gateway.fallback_upstream)
            upstream_url = upstreams.get(name, cfg.gateway.fallback_upstream)
            svc = state.config
            if svc.upstream_via == UpstreamVia.CONFIG_FILE:
                patch_service_config(svc, upstream_url)
            log.info("Auto-restarting '%s' with upstream %s", name, upstream_url)
            await process.start_service(state, upstream_url)
            # The entry must reflect the fresh pid/pgid so reapers and other
            # gateways keep seeing a live owner (I2).
            if state.identity:
                try:
                    entry = registry.read_service_entry(state.identity)
                    if entry is not None:
                        entry.update(
                            {
                                "pid": state.pid,
                                "pgid": state.pgid,
                                "command": resolve_command(svc, upstream_url),
                                "upstream": upstream_url,
                                "started_at": time.time(),
                            }
                        )
                        registry.write_service_entry(entry)
                except Exception:
                    log.exception("Failed to refresh registry entry for '%s'", name)
            _restart_delays.pop(name, None)

        asyncio.create_task(_do_restart())

    process.set_on_crash(_handle_crash)

    # Compute upstreams.  There is no blanket wire_pipeline here: adopters
    # must never patch another gateway's configs (I1), and every spawn
    # patches its own CONFIG_FILE service inside _spawn_owned.
    upstreams = compute_upstreams(cfg.pipeline, cfg.gateway.fallback_upstream)

    # Plan every enabled service BEFORE spawning anything, so a single
    # conflict aborts the start with nothing left half-started.
    plans: list[tuple[ServiceState, str, str, dict | str | None, str]] = []
    identities: list[str] = []
    for state in pipeline.services:
        if not state.config.enabled:
            log.info("Skipping disabled service: %s", state.config.name)
            continue
        upstream_url = upstreams[state.config.name]
        decision, payload = service_ops._plan_service(state.config, upstream_url)
        if decision == "error":
            log.error("%s", payload)
            raise typer.Exit(1)
        identity = registry.compute_service_identity(state.config, upstream_url)
        plans.append((state, decision, upstream_url, payload, identity))
        identities.append(identity)

    # Write the lease BEFORE the first spawn so a racing `down` knows what
    # this gateway is about to own (narrows the down-races-up window).
    registry.write_lease(gw_port, gw_pid, identities, isolated)

    # Start services in order: adopt running ones, reclaim dead-owner ones,
    # spawn the rest.  This loop sits inside its own try so a mid-loop
    # failure (typer.Exit when a port is taken while spawning, or anything
    # else) runs the same _shutdown_pipeline teardown as the runtime
    # finally-block below.  Without this guard, a failure on service N left
    # services 1..N-1 running with a lease written and nobody cleaning up.
    try:
        for state, decision, upstream_url, payload, identity in plans:
            if decision == "adopt":
                service_ops._adopt_from_entry(state, payload, upstream_url)
            else:
                await service_ops._spawn_owned(
                    state,
                    upstream_url,
                    identity,
                    gw_port,
                    gw_pid,
                    reclaim_entry=payload if decision == "promote" else None,
                )
    except BaseException:
        log.warning("Startup failed mid-spawn — tearing down the half-started pipeline")
        try:
            await _shutdown_pipeline(pipeline, gw_port, gw_pid)
        except Exception:
            log.exception("Error stopping pipeline services after failed startup")
        raise

    # The identity set is final now — refresh the lease.
    registry.write_lease(gw_port, gw_pid, identities, isolated)

    stop_event = asyncio.Event()
    health_task: asyncio.Task | None = None
    watcher_task: asyncio.Task | None = None

    async def _stats_callback():
        async with httpx.AsyncClient() as client:
            return await aggregate_stats(pipeline, client)

    async def _health_callback():
        services = {}
        for s in pipeline.services:
            services[s.config.name] = {
                "status": s.status.value,
                "pid": s.pid,
                "port": s.config.port,
                "enabled": s.config.enabled,
                "adopted": s.adopted,
                "owner_port": s.owner_port,
                "shim": shim.get_shim(s.config.port) is not None,
            }
        return {"services": services, "gateway": "running"}

    async def _on_adopted_unhealthy(state: ServiceState) -> None:
        """Reclaim an adopted service whose owner gateway died (health hook)."""
        # Traffic protection despite I1: the shim never touches the owner's
        # process — it only occupies the dead port and forwards.  Common case:
        # the owner gateway restarts the service within seconds, the port is
        # still bound, and this bind fails harmlessly.  If the owner is slow
        # or dead the shim keeps the chain connected; when the owner's respawn
        # eventually tries to rebind, _shim_reconcile releases the shim for it.
        # No restart here (I1) — the process belongs to the owner gateway.
        await _maybe_start_shim(pipeline, state)
        if await service_ops._promote_adopted(state, gw_port, gw_pid):
            current = [s.identity for s in pipeline.services if s.identity]
            registry.write_lease(gw_port, gw_pid, current, isolated)

    gateway_app = create_app(
        pipeline=pipeline,
        gateway_config=cfg.gateway,
        get_entry_url=lambda: get_entry_url(pipeline, cfg.gateway),
        get_entry_route=lambda: get_entry_route(pipeline, cfg.gateway),
        get_stats=_stats_callback,
        get_health=_health_callback,
    )
    pipeline.gateway_running = True

    # Write per-instance PID + port files for `manifold down` / `manifold stats`
    pid_file = paths.pid_file_for(gw_port)
    port_file = paths.port_file_for(gw_port)
    paths.PID_DIR.mkdir(parents=True, exist_ok=True)
    paths.atomic_write_text(pid_file, str(os.getpid()))
    paths.atomic_write_text(port_file, f"{cfg.gateway.host}:{gw_port}")

    log.info(
        "Manifold gateway listening on %s:%d",
        cfg.gateway.host,
        cfg.gateway.port,
    )
    enabled_names = [s.config.name for s in pipeline.services if s.config.enabled]
    log.info("Pipeline: %s", " → ".join(enabled_names))

    # Run uvicorn — we bypass server.serve() and call startup/main_loop/
    # shutdown directly so we own signal handling.  Uvicorn's
    # capture_signals() re-raises SIGINT after lifespan teardown and skips
    # lifespan entirely on a second Ctrl+C (force_exit), which orphans
    # pipeline child processes.
    uvi_config = uvicorn.Config(
        app=gateway_app,
        host=cfg.gateway.host,
        port=cfg.gateway.port,
        log_level="warning",
    )
    server = uvicorn.Server(uvi_config)

    # Replicate the initialisation that _serve() does before startup()
    if not server.config.loaded:
        server.config.load()
    server.lifespan = server.config.lifespan_class(server.config)

    # Install our own signal handlers so cleanup always runs.
    loop = asyncio.get_running_loop()

    def _handle_shutdown():
        nonlocal _shutting_down
        if not _shutting_down:
            _shutting_down = True
            log.info("Shutting down gracefully (press Ctrl+C again to force)...")
            server.should_exit = True
        else:
            log.warning("Forced shutdown — killing all services")
            process.sync_kill_tracked_subprocesses()
            os._exit(1)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_shutdown)

    # The whole runtime sits in a try/finally so that even a startup-health
    # timeout or a crashed uvicorn tears the registry down (today's leak:
    # StartupHealthTimeoutError used to orphan spawned services).
    try:
        try:
            await wait_for_services_ready(pipeline, cfg.gateway)
        except StartupHealthTimeoutError as exc:
            log.error("%s", exc)
            raise typer.Exit(1)

        # Start background health checks
        health_task = asyncio.create_task(
            health_loop(
                pipeline,
                cfg.gateway,
                stop_event=stop_event,
                on_adopted_unhealthy=_on_adopted_unhealthy,
                on_tick=lambda: _shim_reconcile(pipeline),
            )
        )

        # Start config file watcher for hot-reload
        watcher_task = asyncio.create_task(
            watch_config(
                resolved_config_path,
                pipeline,
                cfg.gateway,
                stop_event=stop_event,
                gw_port=gw_port,
                gw_pid=gw_pid,
                isolated=isolated,
            )
        )

        await server.startup()
        if not server.should_exit:
            await server.main_loop()
        if server.started:
            await server.shutdown()
    finally:
        # Non-signal teardowns (startup-health typer.Exit, lifespan failure)
        # reach this finally with the flag still False — the watcher would
        # fire the full "crashed — rewiring / will auto-restart" fiction for
        # every child this teardown SIGTERMs (the exact noise the signal
        # path fixed).  The flag is THE teardown marker; set it here too.
        _shutting_down = True
        stop_event.set()
        if health_task is not None:
            health_task.cancel()
        if watcher_task is not None:
            watcher_task.cancel()
        if health_task is not None or watcher_task is not None:
            await asyncio.gather(health_task, watcher_task, return_exceptions=True)

        if pipeline.gateway_running:
            log.info("Stopping pipeline services...")
        try:
            await _shutdown_pipeline(pipeline, gw_port, gw_pid)
        except Exception:
            log.exception("Error stopping pipeline services")
        pipeline.gateway_running = False
        pid_file.unlink(missing_ok=True)
        port_file.unlink(missing_ok=True)
        log.info("Manifold stopped.")

        # Remove signal handlers — cleanup is done
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(sig)
            except Exception:
                pass


async def _shutdown_pipeline(
    pipeline: PipelineState, gw_port: int, gw_pid: int
) -> None:
    """Graceful teardown with registry handoff (I7: stop on last leave).

    For each owned service still needed by a live other lease, transfer entry
    ownership to that lease's gateway and release our tracking (the process
    keeps running — we just stop being its owner).  Owned services nobody
    needs anymore are stopped and their entries removed.  Adopted services
    are never touched (I1).

    Crash shims are stopped first: they are gateway-local ephemeral state (no
    registry trace, no separate process — an asyncio listener dies with this
    process either way), but stopping them explicitly means nothing forwards
    into services that are about to be stopped.
    """
    await shim.stop_all_shims()

    other_live = registry.live_other_leases(gw_port)
    still_needed: set[str] = set()
    holder_of: dict[str, dict] = {}
    for lease in other_live:
        for ident in lease.get("identities", []):
            still_needed.add(ident)
            holder_of.setdefault(ident, lease)

    for state in reversed(pipeline.services):
        if state.identity is None:
            continue
        if state.adopted:
            continue  # another gateway owns it — leave it alone (I1)
        if state.identity in still_needed:
            holder = holder_of[state.identity]
            entry = registry.read_service_entry(state.identity)
            if entry is not None:
                entry["owner_port"] = holder["gateway_port"]
                entry["owner_pid"] = holder["gateway_pid"]
                registry.write_service_entry(entry)
            await process.release_service(state)
            log.info(
                "Handed off '%s' to gateway :%s",
                state.config.name,
                holder["gateway_port"],
            )
        else:
            await process.stop_service(state)
            registry.remove_service_entry(state.identity)

    registry.remove_lease(gw_port)
    process.sync_kill_tracked_subprocesses()


@app.command()
def up(
    config: str = typer.Option(None, "--config", "-c", help="Path to manifold.yaml"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
    port: int = typer.Option(
        None,
        "--port",
        "-p",
        help="Override gateway port (shared mode: gateway only — services are shared by identity)",
    ),
    isolated: bool = typer.Option(
        False,
        "--isolated",
        help=(
            "Run a fully independent instance: --port offsets gateway AND "
            "service ports by the same delta (legacy behavior)"
        ),
    ),
) -> None:
    """Start all services and the gateway."""
    asyncio.run(_run_pipeline(config, verbose, port_override=port, isolated=isolated))


@app.command()
def status(
    config: str = typer.Option(None, "--config", "-c", help="Path to manifold.yaml"),
) -> None:
    """Show pipeline configuration and service status."""
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    upstreams = compute_upstreams(cfg.pipeline, cfg.gateway.fallback_upstream)
    shimmed = _live_shims(cfg)

    typer.echo(f"Gateway: {cfg.gateway.host}:{cfg.gateway.port}")
    typer.echo(f"Fallback upstream: {cfg.gateway.fallback_upstream}")
    typer.echo()

    for svc in cfg.pipeline:
        marker = "✓" if svc.enabled else "✗"
        upstream = upstreams.get(svc.name, "N/A")
        if svc.name in shimmed:
            typer.echo(f"  [{marker}] {svc.name} [SHIMMED]")
        else:
            typer.echo(f"  [{marker}] {svc.name}")
        typer.echo(f"      port: {svc.port}")
        typer.echo(f"      upstream: {upstream}")
        typer.echo(f"      health: http://127.0.0.1:{svc.port}{svc.health}")
        if svc.name in shimmed:
            typer.echo(
                f"      SHIMMED → {shimmed[svc.name]} "
                "(mid-chain traffic bypass while this service is down; "
                "its function is not performed)"
            )
        if svc.enabled and upstream != "N/A":
            entry = registry.read_service_entry(
                registry.compute_service_identity(svc, upstream)
            )
            if entry is not None and registry.entry_is_live(entry):
                typer.echo(
                    f"      registry: running (pid {entry.get('pid')}, "
                    f"owner gateway :{entry.get('owner_port')})"
                )
        typer.echo()

    enabled = [s.name for s in cfg.pipeline if s.enabled]
    typer.echo(f"Chain: {' → '.join(enabled)}")


@app.command()
def validate(
    config: str = typer.Option(None, "--config", "-c", help="Path to manifold.yaml"),
) -> None:
    """Validate the configuration file."""
    try:
        cfg = load_config(config)
        typer.echo(f"Valid: {len(cfg.pipeline)} services configured")
    except ConfigError as exc:
        typer.echo(f"Invalid: {exc}", err=True)
        raise typer.Exit(1)


def _discover_instances() -> list[tuple[int, Path, Path]]:
    """Find all running manifold instances by globbing PID files.

    Returns a list of (gateway_port, pid_file, port_file) tuples.
    """
    instances: list[tuple[int, Path, Path]] = []
    if not paths.PID_DIR.is_dir():
        return instances
    for pf in sorted(paths.PID_DIR.glob("manifold-*.pid")):
        # Extract port from filename: manifold-9000.pid -> 9000
        stem = pf.stem  # manifold-9000
        try:
            gw_port = int(stem.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        port_f = paths.port_file_for(gw_port)
        instances.append((gw_port, pf, port_f))
    return instances


def _resolve_instance(
    port: int | None,
) -> tuple[int, Path, Path]:
    """Resolve which instance to target.

    If *port* is given, use it directly. Otherwise discover instances:
    - exactly one → use it
    - zero → error
    - multiple → error listing them
    """
    if port is not None:
        pid_f = paths.pid_file_for(port)
        port_f = paths.port_file_for(port)
        if not pid_f.exists():
            typer.echo(f"No manifold instance on port {port}.", err=True)
            raise typer.Exit(1)
        return port, pid_f, port_f

    instances = _discover_instances()
    if not instances:
        typer.echo("No running manifold instance found.", err=True)
        raise typer.Exit(1)
    if len(instances) == 1:
        return instances[0]
    # Multiple instances — ask user to specify
    ports_list = ", ".join(str(gw) for gw, _, _ in instances)
    typer.echo(
        f"Multiple manifold instances running (ports: {ports_list}). "
        "Use --port to specify which one.",
        err=True,
    )
    raise typer.Exit(1)


def _read_gateway_address(port: int | None = None) -> str | None:
    """Read the gateway address from a port file."""
    if port is not None:
        pf = paths.port_file_for(port)
        if pf.exists():
            return pf.read_text().strip()
        return None
    # Discover single instance
    instances = _discover_instances()
    if len(instances) == 1:
        _, _, port_f = instances[0]
        if port_f.exists():
            return port_f.read_text().strip()
    return None


def _lsof_ports_for_config(config_path: str | None) -> list[int]:
    """Kill listeners on configured service ports via lsof (legacy fallback).

    Mirrors kill-ports.sh's port scan for instances that predate the
    registry (no lease, nothing reaped).  Returns the pids killed.  Needs
    *config_path* or a discoverable manifold.yaml.
    """
    if not config_path:
        try:
            config_path = find_config(None)
        except ConfigError:
            return []
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        log.warning("Cannot load config for legacy lsof cleanup: %s", exc)
        return []
    pids: list[int] = []
    for svc in cfg.pipeline:
        if not svc.enabled:
            continue
        try:
            result = subprocess.run(
                ["lsof", "-ti", f"TCP:{svc.port}", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=5.0,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
    # SIGTERM everyone, give them a beat, then SIGKILL survivors (mirrors
    # kill-ports.sh's TERM-then-KILL escalation).
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
    if pids:
        time.sleep(0.5)
    for pid in pids:
        if not registry.pid_alive(pid):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return pids


def _gateway_pid_looks_like_manifold(pid: int) -> bool:
    """Best-effort identity check before signaling a gateway pid (M6).

    Pids are recycled: a stale ``manifold-<port>.pid`` can point at a process
    that has nothing to do with manifold, and a blind SIGTERM kills an
    innocent process.  Reads the command line via ``ps`` and accepts when it
    mentions manifold or uvicorn (service kills verify the pgid per I4; a
    gateway pid has no such second check, so this is its weaker analogue).

    Fails OPEN when ``ps`` itself is unusable (missing/broken): the historic
    behavior was an unconditional signal, and a broken ``ps`` should not
    break ``down``.
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode != 0:
        # ps says there is no such process (it likely exited since the
        # pid_alive check) — nothing to signal either way.
        return False
    command = result.stdout.lower()
    return "manifold" in command or "uvicorn" in command


def _down_one(port: int, config_path: str | None) -> None:
    """Stop the gateway on *port* and reap its registry-tracked services.

    Ordered so that even a dead or hung gateway is fully torn down:
    1. snapshot the lease + pid file BEFORE signaling,
    2. signal the gateway (SIGTERM → poll → SIGKILL),
    3. reap by registry: transfer entries a live other lease still needs
       (I7), kill the rest,
    4. owner_port sweep for entries that never made it into a lease,
    5. clean up lease/pid/port files,
    6. legacy lsof fallback only when there was no registry trace at all.
    """
    # 1. Snapshot BEFORE signaling (the gateway's own handler may clean up).
    lease = registry.read_lease(port)
    snapshot_identities = list(lease.get("identities", [])) if lease else []
    pid_file = paths.pid_file_for(port)
    port_file = paths.port_file_for(port)
    gw_pid: int | None = None
    if pid_file.exists():
        try:
            gw_pid = int(pid_file.read_text().strip())
        except ValueError:
            gw_pid = None

    # 2. Gateway signal: SIGTERM, poll up to 5s, SIGKILL if it hangs — but
    #    only after a best-effort identity check: pid files outlive their
    #    gateway and pids get recycled, and a blind SIGTERM can hit an
    #    unrelated process.
    if gw_pid is not None and registry.pid_alive(gw_pid):
        if not _gateway_pid_looks_like_manifold(gw_pid):
            log.warning(
                "Process %d on port %d does not look like a manifold gateway "
                "(command names neither manifold nor uvicorn) — pid reuse? "
                "Skipping the signal; cleaning up stale files only.",
                gw_pid,
                port,
            )
            typer.echo(
                f"Process {gw_pid} on port {port} does not look like a manifold "
                "gateway (pid reuse?) — skipping signal, cleaning stale files"
            )
        else:
            try:
                os.kill(gw_pid, signal.SIGTERM)
                typer.echo(f"Sent SIGTERM to manifold on port {port} (pid={gw_pid})")
            except PermissionError:
                typer.echo(
                    f"Permission denied sending signal to pid={gw_pid}", err=True
                )
                raise typer.Exit(1)
            deadline = time.monotonic() + 5.0
            while registry.pid_alive(gw_pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if registry.pid_alive(gw_pid):
                log.info("Gateway %d still alive after SIGTERM — SIGKILL", port)
                try:
                    os.kill(gw_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                time.sleep(0.5)
    elif gw_pid is not None:
        typer.echo(f"Process {gw_pid} not found — cleaning up stale PID file.")

    # 3. Direct reap by registry: for each snapshotted identity.
    other_live = registry.live_other_leases(port)
    surviving: set[str] = set()
    holder_of: dict[str, dict] = {}
    for other in other_live:
        for ident in other.get("identities", []):
            surviving.add(ident)
            holder_of.setdefault(ident, other)

    reaped = False
    for identity in snapshot_identities:
        entry = registry.read_service_entry(identity)
        if entry is None:
            continue
        if identity in surviving:
            # Another live gateway still needs it — transfer ownership (I7).
            holder = holder_of[identity]
            entry["owner_port"] = holder["gateway_port"]
            entry["owner_pid"] = holder["gateway_pid"]
            registry.write_service_entry(entry)
            log.info(
                "Transferred '%s' ownership to gateway :%s",
                identity,
                holder["gateway_port"],
            )
        elif entry.get("owner_port") == port or not registry.pid_alive(
            entry.get("owner_pid")
        ):
            # We are the last live lease holder (I7): kill when WE are the
            # recorded owner, or when the recorded owner is dead (the
            # transfer branch above covered the live-owner case).
            registry.kill_entry_processes(entry)
            registry.remove_service_entry(identity)
            reaped = True

    # 4. owner_port sweep: entries owned by this gateway that never made it
    #    into the lease (crash-mid-startup).
    for entry in registry.list_service_entries():
        if entry.get("owner_port") != port:
            continue
        if entry["identity"] in surviving:
            continue
        registry.kill_entry_processes(entry)
        registry.remove_service_entry(entry["identity"])
        reaped = True

    # 5. File cleanup.
    registry.remove_lease(port)
    pid_file.unlink(missing_ok=True)
    port_file.unlink(missing_ok=True)

    # 6. Legacy fallback — only when there was no registry trace at all.
    if lease is None and not reaped:
        killed = _lsof_ports_for_config(config_path)
        if killed:
            typer.echo(f"Killed {len(killed)} legacy process(es) via lsof port scan")


@app.command()
def down(
    port: int = typer.Option(
        None,
        "--port",
        "-p",
        help="Gateway port of the instance to stop",
    ),
    all: bool = typer.Option(
        False,
        "--all",
        help="Stop every running manifold instance",
    ),
    config: str = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to manifold.yaml (used by the legacy lsof fallback)",
    ),
) -> None:
    """Stop a running manifold instance."""
    if all:
        discovered: set[int] = set()
        for lease in registry.list_leases():
            discovered.add(lease["gateway_port"])
        for gw_port, _, _ in _discover_instances():
            discovered.add(gw_port)
        for gw_port in sorted(discovered):
            _down_one(gw_port, config)
        registry.sweep_stale()
        return
    try:
        gw_port, _pid_file, _port_file = _resolve_instance(port)
    except typer.Exit:
        # A dead gateway may have lost its pid file (kill-ports.sh removes
        # them) while registry entries remain — the registry is the source
        # of truth for teardown, not the pid file.
        has_registry = registry.read_lease(port) is not None or any(
            entry.get("owner_port") == port for entry in registry.list_service_entries()
        )
        if not has_registry:
            raise
        gw_port = port
    _down_one(gw_port, config)


@app.command()
def stats(
    config: str = typer.Option(None, "--config", "-c", help="Path to manifold.yaml"),
    port: int = typer.Option(
        None,
        "--port",
        "-p",
        help="Gateway port of the instance to query",
    ),
) -> None:
    """Fetch and display stats from a running manifold gateway."""
    addr = _read_gateway_address(port)
    if addr is None:
        # Fall back to config to find the port
        try:
            cfg = load_config(config)
            addr = f"{cfg.gateway.host}:{cfg.gateway.port}"
        except ConfigError:
            typer.echo(
                "No running manifold found and no config to read port from.", err=True
            )
            raise typer.Exit(1)

    url = f"http://{addr}/_manifold/stats"
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url)
        if resp.status_code >= 400:
            typer.echo(f"Gateway returned HTTP {resp.status_code}", err=True)
            raise typer.Exit(1)
        typer.echo(json.dumps(resp.json(), indent=2))
    except httpx.ConnectError:
        typer.echo(f"Cannot connect to manifold at {addr}", err=True)
        raise typer.Exit(1)


@app.command()
def add(
    config: str = typer.Option(None, "--config", "-c", help="Path to manifold.yaml"),
) -> None:
    """Interactively register a new service into the pipeline."""
    try:
        config_path = find_config(config)
    except ConfigError:
        config_path = Path("manifold.yaml")
        if not config_path.exists():
            typer.echo(f"Config file not found. Will create {config_path}")

    name = typer.prompt("Service name")
    directory = typer.prompt("Service directory (absolute path)")
    command = typer.prompt("Start command (use {port} and {upstream} templates)")
    port = typer.prompt("Port", type=int)
    health = typer.prompt("Health endpoint path (e.g. /healthz)")
    stats_ep = typer.prompt("Stats endpoint path (leave empty to skip)", default="")
    import click

    upstream_via = typer.prompt(
        "Upstream via",
        type=click.Choice(["config_file", "cli_arg"]),
        default="cli_arg",
    )

    entry: dict = {
        "name": name,
        "directory": directory,
        "command": command,
        "port": port,
        "health": health,
        "upstream_via": upstream_via,
        "enabled": True,
    }

    if stats_ep:
        entry["stats"] = stats_ep

    if upstream_via == "config_file":
        cfg_file = typer.prompt("Config file (relative to service directory)")
        upstream_key = typer.prompt("Upstream key (dot-path in YAML config)")
        entry["config_file"] = cfg_file
        entry["upstream_key"] = upstream_key

    # Load existing config or create new
    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
    else:
        raw = {"gateway": {"host": "127.0.0.1", "port": 9000}, "pipeline": []}

    if "pipeline" not in raw:
        raw["pipeline"] = []

    raw["pipeline"].append(entry)

    # Only prompt for startup health if not already configured
    gwy = raw.get("gateway") or {}
    if "startup_health_timeout" not in gwy:
        _maybe_prompt_gateway_startup_health(raw)

    with open(config_path, "w") as f:
        yaml.safe_dump(raw, f, default_flow_style=False, sort_keys=False)

    typer.echo(f"Added '{name}' to {config_path}")
    typer.echo(f"Pipeline now has {len(raw['pipeline'])} service(s)")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
