"""Hot-reload watcher for manifold.yaml changes."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from manifold.chain import wire_pipeline, compute_upstreams
from manifold.config import ConfigError, load_config
from manifold.models import GatewayConfig, ManifoldConfig, PipelineState, ServiceState
from manifold.process import restart_service, start_service, stop_service

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 2.0


async def watch_config(
    config_path: str | Path,
    pipeline: PipelineState,
    gateway: GatewayConfig,
    interval: float = DEFAULT_POLL_INTERVAL,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Poll manifold.yaml for changes and apply them to the running pipeline.

    Detects:
    - Services enabled/disabled
    - Pipeline order changes
    - Port/command changes (triggers restart)
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

        await _apply_config_changes(new_cfg, pipeline, gateway)


async def _apply_config_changes(
    new_cfg: ManifoldConfig,
    pipeline: PipelineState,
    gateway: GatewayConfig,
) -> None:
    """Apply differences between running pipeline and new config."""
    new_by_name = {s.name: s for s in new_cfg.pipeline}
    old_by_name = {s.config.name: s for s in pipeline.services}

    # Update gateway config in place
    gateway.host = new_cfg.gateway.host
    gateway.port = new_cfg.gateway.port
    gateway.fallback_upstream = new_cfg.gateway.fallback_upstream

    # Stop removed services
    for name, state in old_by_name.items():
        if name not in new_by_name:
            log.info("Service '%s' removed from config, stopping", name)
            await stop_service(state)
            pipeline.services.remove(state)

    # Compute new upstreams
    upstreams = compute_upstreams(new_cfg.pipeline, gateway.fallback_upstream)

    # Update or add services
    for new_svc in new_cfg.pipeline:
        upstream_url = upstreams.get(new_svc.name, gateway.fallback_upstream)

        if new_svc.name in old_by_name:
            state = old_by_name[new_svc.name]
            old_svc = state.config

            # Check if enabled state changed
            if new_svc.enabled != old_svc.enabled:
                if not new_svc.enabled:
                    log.info("Service '%s' disabled, stopping", new_svc.name)
                    await stop_service(state)
                else:
                    log.info("Service '%s' enabled, starting", new_svc.name)
                    state.config = new_svc
                    await start_service(state, upstream_url)
                state.config = new_svc
                continue

            # Check if config changed in a way that requires restart
            needs_restart = (
                new_svc.command != old_svc.command
                or new_svc.port != old_svc.port
                or new_svc.directory != old_svc.directory
            )

            state.config = new_svc

            if needs_restart and new_svc.enabled:
                log.info("Service '%s' config changed, restarting", new_svc.name)
                await restart_service(state, upstream_url)
            elif state.upstream_url != upstream_url and new_svc.enabled:
                log.info("Upstream for '%s' changed to %s", new_svc.name, upstream_url)
                state.upstream_url = upstream_url
        else:
            # New service
            log.info("New service '%s' found in config", new_svc.name)
            state = ServiceState(config=new_svc)
            pipeline.services.append(state)
            if new_svc.enabled:
                await start_service(state, upstream_url)

    # Re-wire config files for all services
    wire_pipeline(
        [s.config for s in pipeline.services],
        gateway,
    )

    log.info("Config reload complete")
