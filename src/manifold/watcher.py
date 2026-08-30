"""Hot-reload watcher for manifold.yaml changes."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from manifold import process, registry, service_ops
from manifold.chain import (
    compute_upstreams,
    patch_service_config,
    resolve_command,
    rewire_around,
    wire_pipeline,
)
from manifold.config import ConfigError, load_config
from manifold.models import (
    GatewayConfig,
    ManifoldConfig,
    PipelineState,
    ServiceState,
    UpstreamVia,
)

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 2.0


async def watch_config(
    config_path: str | Path,
    pipeline: PipelineState,
    gateway: GatewayConfig,
    interval: float = DEFAULT_POLL_INTERVAL,
    stop_event: asyncio.Event | None = None,
    gw_port: int | None = None,
    gw_pid: int | None = None,
    isolated: bool = False,
) -> None:
    """Poll manifold.yaml for changes and apply them to the running pipeline.

    Detects:
    - Services enabled/disabled
    - Pipeline order changes
    - Port/command changes (triggers restart)

    With *gw_port* set (shared mode) the watcher also maintains the registry:
    adopted services are dropped from tracking only, owned services keep
    fresh entries, and the gateway lease is rewritten after every change.
    Without it the watcher behaves exactly as before (no registry).
    """
    config_path = Path(config_path)
    last_mtime: float = config_path.stat().st_mtime if config_path.exists() else 0

    while True:
        if stop_event and stop_event.is_set():
            break

        try:
            if stop_event:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break
            else:
                await asyncio.sleep(interval)
        except asyncio.TimeoutError:
            pass

        if not config_path.exists():
            continue

        current_mtime = config_path.stat().st_mtime
        if current_mtime <= last_mtime:
            continue

        last_mtime = current_mtime
        log.info("Config file changed, reloading...")

        try:
            new_cfg = load_config(config_path)
        except ConfigError as exc:
            log.error("Invalid config after change, ignoring: %s", exc)
            continue

        await _apply_config_changes(
            new_cfg, pipeline, gateway, gw_port, gw_pid, isolated
        )


async def _apply_config_changes(
    new_cfg: ManifoldConfig,
    pipeline: PipelineState,
    gateway: GatewayConfig,
    gw_port: int | None = None,
    gw_pid: int | None = None,
    isolated: bool = False,
) -> None:
    """Apply differences between running pipeline and new config."""
    new_by_name = {s.name: s for s in new_cfg.pipeline}
    old_by_name = {s.config.name: s for s in pipeline.services}
    shared = gw_port is not None

    # Update gateway config in place
    gateway.host = new_cfg.gateway.host
    gateway.port = new_cfg.gateway.port
    gateway.fallback_upstream = new_cfg.gateway.fallback_upstream

    # Stop removed services
    removed = False
    for name, state in old_by_name.items():
        if name not in new_by_name:
            log.info("Service '%s' removed from config, stopping", name)
            if shared and state.adopted:
                # Another gateway owns the process — drop tracking only (I1).
                log.info("Service '%s' was adopted — not killing it", name)
            else:
                await process.stop_service(state)
                if shared and state.identity:
                    registry.remove_service_entry(state.identity)
            pipeline.services.remove(state)
            removed = True

    # Compute upstreams; in shared mode patch only owned config files —
    # adopters must never patch another gateway's configs (I1), and every
    # spawn patches its own CONFIG_FILE service inside _spawn_owned.
    upstreams = compute_upstreams(new_cfg.pipeline, gateway.fallback_upstream)

    if shared:
        for new_svc in new_cfg.pipeline:
            if not new_svc.enabled:
                continue
            state = old_by_name.get(new_svc.name)
            if (
                state is not None
                and not state.adopted
                and new_svc.upstream_via == UpstreamVia.CONFIG_FILE
            ):
                patch_service_config(
                    new_svc, upstreams.get(new_svc.name, gateway.fallback_upstream)
                )
    else:
        # Wire pipeline FIRST — patches config files with correct upstreams
        # BEFORE any service is (re)started, so it reads the right endpoint.
        wire_pipeline(new_cfg.pipeline, gateway)

    # Update or add services
    changed = False
    for new_svc in new_cfg.pipeline:
        upstream_url = upstreams.get(new_svc.name, gateway.fallback_upstream)

        if new_svc.name in old_by_name:
            state = old_by_name[new_svc.name]
            old_svc = state.config

            # Check if enabled state changed
            if new_svc.enabled != old_svc.enabled:
                if not new_svc.enabled:
                    log.info("Service '%s' disabled, stopping", new_svc.name)
                    if shared and not state.adopted and state.identity:
                        registry.remove_service_entry(state.identity)
                    await process.stop_service(state)
                else:
                    log.info("Service '%s' enabled, starting", new_svc.name)
                    if shared:
                        await service_ops._plan_and_start(
                            state, upstream_url, gw_port, gw_pid
                        )
                    else:
                        await process.start_service(state, upstream_url)
                state.config = new_svc
                changed = True
                continue

            # Check if config changed in a way that requires restart
            needs_restart = (
                new_svc.command != old_svc.command
                or new_svc.port != old_svc.port
                or new_svc.directory != old_svc.directory
            )
            # In shared mode an upstream change also requires a restart of an
            # owned service: the running process keeps its old wiring until
            # restarted (neither CLI_ARG nor CONFIG_FILE services hot-reload
            # upstreams), and the registry entry must stay truthful (I6).
            if shared and not state.adopted and state.upstream_url != upstream_url:
                needs_restart = True

            wiring_changed = needs_restart or (
                shared and state.upstream_url != upstream_url
            )
            if shared and wiring_changed and new_svc.enabled and state.adopted:
                # The running process belongs to another gateway: drop our
                # tracking and let the plan decide for the new wiring (I1).
                # If the owner has already restarted under the new identity
                # we re-adopt; otherwise the conflict is logged, not raised.
                log.info(
                    "Service '%s' wiring changed; releasing adopted process",
                    new_svc.name,
                )
                await process.release_service(state)
                state.config = new_svc
                await service_ops._plan_and_start(state, upstream_url, gw_port, gw_pid)
                changed = True
                continue

            state.config = new_svc

            if needs_restart and new_svc.enabled:
                log.info("Service '%s' config changed, restarting", new_svc.name)
                old_identity = state.identity
                if shared:
                    # New wiring → new identity: the entry must move.
                    state.identity = registry.compute_service_identity(
                        new_svc, upstream_url
                    )
                await process.restart_service(state, upstream_url)
                if shared and state.identity:
                    if old_identity is not None and old_identity != state.identity:
                        registry.remove_service_entry(old_identity)
                    try:
                        entry = registry.read_service_entry(state.identity)
                        if entry is None:
                            # Identity changed → no entry exists yet: build
                            # a fresh one (spawn normally does this).
                            entry = {
                                "schema_version": registry.SCHEMA_VERSION,
                                "identity": state.identity,
                                "name": new_svc.name,
                                "directory": new_svc.directory,
                                "command": resolve_command(new_svc, upstream_url),
                                "port": new_svc.port,
                                "upstream": upstream_url,
                            }
                        entry.update(
                            {
                                "pid": state.pid,
                                "pgid": state.pgid,
                                "owner_port": gw_port,
                                "owner_pid": gw_pid,
                                "started_at": time.time(),
                            }
                        )
                        registry.write_service_entry(entry)
                    except Exception:
                        log.exception(
                            "Failed to refresh registry entry for '%s'",
                            new_svc.name,
                        )
                changed = True
            elif state.upstream_url != upstream_url and new_svc.enabled:
                log.info("Upstream for '%s' changed to %s", new_svc.name, upstream_url)
                state.upstream_url = upstream_url
        else:
            # New service
            log.info("New service '%s' found in config", new_svc.name)
            state = ServiceState(config=new_svc)
            pipeline.services.append(state)
            if new_svc.enabled:
                if shared:
                    await service_ops._plan_and_start(
                        state, upstream_url, gw_port, gw_pid
                    )
                else:
                    await process.start_service(state, upstream_url)
            changed = True

    if changed or removed:
        rewire_around(pipeline, gateway)
        if shared:
            identities = [s.identity for s in pipeline.services if s.identity]
            registry.write_lease(gw_port, gw_pid, identities, isolated)

    log.info("Config reload complete")
