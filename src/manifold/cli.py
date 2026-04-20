"""Typer CLI — manifold up / down / status / stats / add."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path

import httpx
import typer
import uvicorn
import yaml

from manifold.chain import (
    compute_upstreams,
    get_entry_url,
    patch_service_config,
    wire_pipeline,
)
from manifold.config import ConfigError, find_config, load_config
from manifold import paths
from manifold.gateway import create_app
from manifold.health import (
    health_loop,
    wait_for_services_ready,
    StartupHealthTimeoutError,
)
from manifold.watcher import watch_config
from manifold.models import PipelineState, ServiceState, ServiceStatus, UpstreamVia
from manifold.process import (
    set_on_crash,
    start_service,
    stop_all,
    sync_kill_tracked_subprocesses,
)
from manifold.stats import aggregate_stats

app = typer.Typer(
    name="manifold",
    help="Proxy mesh gateway — chain LLM proxy services into a single pipeline.",
    add_completion=False,
)

log = logging.getLogger("manifold")


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
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def _run_pipeline(config_path: str | None, verbose: bool) -> None:
    """Core async logic for 'manifold up'."""
    _setup_logging(verbose)

    try:
        resolved_config_path = find_config(config_path)
        cfg = load_config(config_path)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        raise typer.Exit(1)

    pipeline = PipelineState(
        services=[ServiceState(config=svc) for svc in cfg.pipeline]
    )

    # Register crash callback: rewire chain, then schedule auto-restart
    from manifold.chain import rewire_around

    _restart_delays: dict[str, float] = {}
    _MAX_RESTART_DELAY = 60.0
    _BASE_RESTART_DELAY = 2.0

    def _handle_crash(state: ServiceState) -> None:
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
            await asyncio.sleep(delay)
            if state.status == ServiceStatus.STOPPED:
                return  # user explicitly stopped it
            # Re-compute correct upstream and patch config before restarting
            upstreams = compute_upstreams(cfg.pipeline, cfg.gateway.fallback_upstream)
            upstream_url = upstreams.get(name, cfg.gateway.fallback_upstream)
            svc = state.config
            if svc.upstream_via == UpstreamVia.CONFIG_FILE:
                patch_service_config(svc, upstream_url)
            log.info("Auto-restarting '%s' with upstream %s", name, upstream_url)
            await start_service(state, upstream_url)
            _restart_delays.pop(name, None)

        asyncio.ensure_future(_do_restart())

    set_on_crash(_handle_crash)

    # Wire chain: compute upstreams and patch config files
    upstreams = wire_pipeline(cfg.pipeline, cfg.gateway)

    # Start services in order
    for state in pipeline.services:
        if not state.config.enabled:
            log.info("Skipping disabled service: %s", state.config.name)
            continue
        upstream_url = upstreams[state.config.name]
        await start_service(state, upstream_url)

    try:
        await wait_for_services_ready(pipeline, cfg.gateway)
    except StartupHealthTimeoutError as exc:
        log.error("%s", exc)
        raise typer.Exit(1)

    # Health check stop event
    stop_event = asyncio.Event()

    # Start background health checks
    health_task = asyncio.create_task(
        health_loop(pipeline, cfg.gateway, stop_event=stop_event)
    )

    # Start config file watcher for hot-reload
    watcher_task = asyncio.create_task(
        watch_config(resolved_config_path, pipeline, cfg.gateway, stop_event=stop_event)
    )

    # Create gateway app with callbacks
    import httpx as _httpx

    async def _stats_callback():
        async with _httpx.AsyncClient() as client:
            return await aggregate_stats(pipeline, client)

    async def _health_callback():
        services = {}
        for s in pipeline.services:
            services[s.config.name] = {
                "status": s.status.value,
                "pid": s.pid,
                "port": s.config.port,
                "enabled": s.config.enabled,
            }
        return {"services": services, "gateway": "running"}

    gateway_app = create_app(
        pipeline=pipeline,
        gateway_config=cfg.gateway,
        get_entry_url=lambda: get_entry_url(pipeline, cfg.gateway),
        get_stats=_stats_callback,
        get_health=_health_callback,
    )
    pipeline.gateway_running = True

    # Write PID + port files for `manifold down` / `manifold stats`
    paths.PID_DIR.mkdir(parents=True, exist_ok=True)
    paths.atomic_write_text(paths.PID_FILE, str(os.getpid()))
    paths.atomic_write_text(paths.PORT_FILE, f"{cfg.gateway.host}:{cfg.gateway.port}")

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
    _shutting_down = False

    def _handle_shutdown():
        nonlocal _shutting_down
        if not _shutting_down:
            _shutting_down = True
            log.info("Shutting down gracefully (press Ctrl+C again to force)...")
            server.should_exit = True
        else:
            log.warning("Forced shutdown — killing all services")
            sync_kill_tracked_subprocesses()
            os._exit(1)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_shutdown)

    try:
        await server.startup()
        if not server.should_exit:
            await server.main_loop()
        if server.started:
            await server.shutdown()
    finally:
        stop_event.set()
        health_task.cancel()
        watcher_task.cancel()
        await asyncio.gather(health_task, watcher_task, return_exceptions=True)

        if pipeline.gateway_running:
            log.info("Stopping pipeline services...")
            try:
                await stop_all(pipeline.services)
            except Exception:
                log.exception("Error stopping pipeline services")
            sync_kill_tracked_subprocesses()
            pipeline.gateway_running = False
            paths.PID_FILE.unlink(missing_ok=True)
            paths.PORT_FILE.unlink(missing_ok=True)
            log.info("Manifold stopped.")

        # Remove signal handlers — cleanup is done
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(sig)
            except Exception:
                pass


@app.command()
def up(
    config: str = typer.Option(None, "--config", "-c", help="Path to manifold.yaml"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    """Start all services and the gateway."""
    asyncio.run(_run_pipeline(config, verbose))


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

    typer.echo(f"Gateway: {cfg.gateway.host}:{cfg.gateway.port}")
    typer.echo(f"Fallback upstream: {cfg.gateway.fallback_upstream}")
    typer.echo()

    for svc in cfg.pipeline:
        marker = "✓" if svc.enabled else "✗"
        upstream = upstreams.get(svc.name, "N/A")
        typer.echo(f"  [{marker}] {svc.name}")
        typer.echo(f"      port: {svc.port}")
        typer.echo(f"      upstream: {upstream}")
        typer.echo(f"      health: http://127.0.0.1:{svc.port}{svc.health}")
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


def _read_gateway_address() -> str | None:
    """Read the gateway address from the port file."""
    if paths.PORT_FILE.exists():
        return paths.PORT_FILE.read_text().strip()
    return None


@app.command()
def down() -> None:
    """Stop a running manifold instance."""
    if not paths.PID_FILE.exists():
        typer.echo("No running manifold instance found.", err=True)
        raise typer.Exit(1)

    pid = int(paths.PID_FILE.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        typer.echo(f"Sent SIGTERM to manifold (pid={pid})")
    except ProcessLookupError:
        typer.echo(f"Process {pid} not found — cleaning up stale PID file.")
        paths.PID_FILE.unlink(missing_ok=True)
        paths.PORT_FILE.unlink(missing_ok=True)
    except PermissionError:
        typer.echo(f"Permission denied sending signal to pid={pid}", err=True)
        raise typer.Exit(1)


@app.command()
def stats(
    config: str = typer.Option(None, "--config", "-c", help="Path to manifold.yaml"),
) -> None:
    """Fetch and display stats from a running manifold gateway."""
    addr = _read_gateway_address()
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
