"""Typer CLI — manifold up / down / status / stats / add."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
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
from manifold.gateway import create_app
from manifold.health import health_loop
from manifold.watcher import watch_config
from manifold.models import PipelineState, ServiceState, ServiceStatus, UpstreamVia
from manifold.process import start_service, stop_all
from manifold.stats import aggregate_stats

app = typer.Typer(
    name="manifold",
    help="Proxy mesh gateway — chain LLM proxy services into a single pipeline.",
    add_completion=False,
)

log = logging.getLogger("manifold")

PID_DIR = Path.home() / ".manifold"
PID_FILE = PID_DIR / "manifold.pid"
PORT_FILE = PID_DIR / "manifold.port"


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

    # Wire chain: compute upstreams and patch config files
    upstreams = wire_pipeline(cfg.pipeline, cfg.gateway)

    # Start services in order
    for state in pipeline.services:
        if not state.config.enabled:
            log.info("Skipping disabled service: %s", state.config.name)
            continue
        upstream_url = upstreams[state.config.name]
        await start_service(state, upstream_url)

    # Wait briefly for services to initialize
    log.info("Waiting for services to start...")
    await asyncio.sleep(2.0)

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
    PID_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    PORT_FILE.write_text(f"{cfg.gateway.host}:{cfg.gateway.port}")

    log.info(
        "Manifold gateway listening on %s:%d",
        cfg.gateway.host,
        cfg.gateway.port,
    )
    enabled_names = [s.config.name for s in pipeline.services if s.config.enabled]
    log.info("Pipeline: %s", " → ".join(enabled_names))

    # Run uvicorn
    uvi_config = uvicorn.Config(
        app=gateway_app,
        host=cfg.gateway.host,
        port=cfg.gateway.port,
        log_level="warning",
    )
    server = uvicorn.Server(uvi_config)

    try:
        await server.serve()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        log.info("Shutting down...")
        stop_event.set()
        health_task.cancel()
        watcher_task.cancel()
        await stop_all(pipeline.services)
        pipeline.gateway_running = False
        PID_FILE.unlink(missing_ok=True)
        PORT_FILE.unlink(missing_ok=True)
        log.info("Manifold stopped.")


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
    if PORT_FILE.exists():
        return PORT_FILE.read_text().strip()
    return None


@app.command()
def down() -> None:
    """Stop a running manifold instance."""
    if not PID_FILE.exists():
        typer.echo("No running manifold instance found.", err=True)
        raise typer.Exit(1)

    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        typer.echo(f"Sent SIGTERM to manifold (pid={pid})")
    except ProcessLookupError:
        typer.echo(f"Process {pid} not found — cleaning up stale PID file.")
        PID_FILE.unlink(missing_ok=True)
        PORT_FILE.unlink(missing_ok=True)
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
            typer.echo("No running manifold found and no config to read port from.", err=True)
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

    with open(config_path, "w") as f:
        yaml.safe_dump(raw, f, default_flow_style=False, sort_keys=False)

    typer.echo(f"Added '{name}' to {config_path}")
    typer.echo(f"Pipeline now has {len(raw['pipeline'])} service(s)")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
