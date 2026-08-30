"""Tests for manifold.watcher module."""

import asyncio
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from manifold import registry
from manifold.config import load_config
from manifold.models import (
    ManifoldConfig,
    PipelineState,
    ServiceConfig,
    ServiceState,
    UpstreamVia,
)
from manifold.watcher import _apply_config_changes, watch_config


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    content = textwrap.dedent("""\
        gateway:
          host: 127.0.0.1
          port: 9000
        pipeline:
          - name: svc-a
            directory: /tmp
            command: "echo a --port {port} --upstream {upstream}"
            port: 7001
            health: /h
            upstream_via: cli_arg
            enabled: true
    """)
    p = tmp_path / "manifold.yaml"
    p.write_text(content)
    return p


def _svc(**overrides) -> ServiceConfig:
    fields = dict(
        name="svc-a",
        directory="/tmp",
        command="echo a --port {port} --upstream {upstream}",
        port=7001,
        health="/h",
        upstream_via=UpstreamVia.CLI_ARG,
    )
    fields.update(overrides)
    return ServiceConfig(**fields)


def _entry(identity: str, name: str = "svc-a", port: int = 7001) -> dict:
    return {
        "schema_version": 1,
        "identity": identity,
        "name": name,
        "directory": "/tmp",
        "command": f"echo a --port {port} --upstream https://api.anthropic.com",
        "port": port,
        "upstream": "https://api.anthropic.com",
        "pid": 4242,
        "pgid": 4242,
        "owner_port": 9001,
        "owner_pid": 4242,
        "started_at": 0.0,
    }


@pytest.mark.asyncio
async def test_watcher_detects_change(config_file: Path):
    """Watcher should detect file modification and reload."""
    cfg = load_config(config_file)
    pipeline = PipelineState(
        services=[ServiceState(config=svc) for svc in cfg.pipeline]
    )
    gateway = cfg.gateway
    stop_event = asyncio.Event()

    # Start watcher with very short interval
    with patch(
        "manifold.watcher._apply_config_changes", new_callable=AsyncMock
    ) as mock_apply:
        task = asyncio.create_task(
            watch_config(
                config_file, pipeline, gateway, interval=0.1, stop_event=stop_event
            )
        )

        # Modify the config file
        await asyncio.sleep(0.15)
        content = config_file.read_text().replace("enabled: true", "enabled: false")
        config_file.write_text(content)

        # Wait for watcher to pick up change
        await asyncio.sleep(0.3)

        stop_event.set()
        await task

        assert mock_apply.call_count >= 1


@pytest.mark.asyncio
async def test_watcher_ignores_invalid_config(config_file: Path):
    """Watcher should skip invalid config changes."""
    cfg = load_config(config_file)
    pipeline = PipelineState(
        services=[ServiceState(config=svc) for svc in cfg.pipeline]
    )
    gateway = cfg.gateway
    stop_event = asyncio.Event()

    with patch(
        "manifold.watcher._apply_config_changes", new_callable=AsyncMock
    ) as mock_apply:
        task = asyncio.create_task(
            watch_config(
                config_file, pipeline, gateway, interval=0.1, stop_event=stop_event
            )
        )

        # Write invalid YAML
        await asyncio.sleep(0.15)
        config_file.write_text("pipeline: not_a_list")

        await asyncio.sleep(0.3)
        stop_event.set()
        await task

        # Should not have applied changes
        assert mock_apply.call_count == 0


# --- shared-mode (registry-aware) reloads -----------------------------------


@pytest.mark.asyncio
async def test_shared_mode_adopted_removal_drops_tracking_only(
    tmp_path: Path, config_file: Path
):
    """Removing an adopted service must not stop the (foreign) process."""
    cfg = load_config(config_file)
    state = ServiceState(config=cfg.pipeline[0])
    state.adopted = True
    state.identity = "id-a"
    pipeline = PipelineState(services=[state])

    new_cfg = ManifoldConfig(gateway=cfg.gateway, pipeline=[])

    with patch("manifold.paths.PID_DIR", tmp_path):
        stop_mock = AsyncMock()
        with patch("manifold.process.stop_service", stop_mock):
            await _apply_config_changes(
                new_cfg, pipeline, cfg.gateway, gw_port=9000, gw_pid=123
            )
        stop_mock.assert_not_awaited()
        assert pipeline.services == []
        lease = registry.read_lease(9000)
        assert lease is not None
        assert lease["identities"] == []


@pytest.mark.asyncio
async def test_shared_mode_new_service_spawns_and_writes_entry(
    tmp_path: Path, config_file: Path
):
    cfg = load_config(config_file)
    pipeline = PipelineState(services=[])

    with patch("manifold.paths.PID_DIR", tmp_path):
        with patch("manifold.paths.is_port_in_use", return_value=False):
            start_mock = AsyncMock()
            with patch("manifold.process.start_service", start_mock):
                await _apply_config_changes(
                    cfg, pipeline, cfg.gateway, gw_port=9000, gw_pid=123
                )
            start_mock.assert_awaited_once()
            assert len(pipeline.services) == 1
            state = pipeline.services[0]
            assert state.identity is not None
            entry = registry.read_service_entry(state.identity)
            assert entry is not None
            assert entry["owner_port"] == 9000
            lease = registry.read_lease(9000)
            assert lease is not None
            assert state.identity in lease["identities"]


@pytest.mark.asyncio
async def test_shared_mode_adopted_restart_releases_and_respawns(
    tmp_path: Path, config_file: Path
):
    """Wiring change on an adopted service: release + plan the new wiring."""
    cfg = load_config(config_file)
    state = ServiceState(config=cfg.pipeline[0])
    state.adopted = True
    state.identity = "old-id"
    state.pid = 4242
    pipeline = PipelineState(services=[state])

    new_cfg = ManifoldConfig(gateway=cfg.gateway, pipeline=[_svc(port=7002)])

    with patch("manifold.paths.PID_DIR", tmp_path):
        with patch("manifold.paths.is_port_in_use", return_value=False):
            rel_mock = AsyncMock()
            start_mock = AsyncMock()
            with (
                patch("manifold.process.release_service", rel_mock),
                patch("manifold.process.start_service", start_mock),
            ):
                await _apply_config_changes(
                    new_cfg, pipeline, cfg.gateway, gw_port=9000, gw_pid=123
                )
            rel_mock.assert_awaited_once_with(state)
            start_mock.assert_awaited_once()
            assert state.config.port == 7002
            assert state.adopted is False
            lease = registry.read_lease(9000)
            assert lease is not None
            assert state.identity in lease["identities"]


@pytest.mark.asyncio
async def test_shared_mode_owned_restart_refreshes_entry(
    tmp_path: Path, config_file: Path
):
    """Owned service restart after wiring change: entry moves to new identity."""
    cfg = load_config(config_file)
    identity = registry.compute_service_identity(
        cfg.pipeline[0], "https://api.anthropic.com"
    )
    state = ServiceState(config=cfg.pipeline[0])
    state.identity = identity
    state.pid = 4242
    state.pgid = 4242
    pipeline = PipelineState(services=[state])

    new_cfg = ManifoldConfig(gateway=cfg.gateway, pipeline=[_svc(port=7002)])

    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(_entry(identity))
        with (
            patch("manifold.process.start_service", new_callable=AsyncMock),
            patch("manifold.process.stop_service", new_callable=AsyncMock),
        ):
            await _apply_config_changes(
                new_cfg, pipeline, cfg.gateway, gw_port=9000, gw_pid=123
            )
        # old wiring entry removed, new one written with fresh owner fields
        assert registry.read_service_entry(identity) is None
        new_entry = registry.read_service_entry(state.identity)
        assert new_entry is not None
        assert new_entry["owner_port"] == 9000
        assert state.identity != identity
        lease = registry.read_lease(9000)
        assert state.identity in lease["identities"]


@pytest.mark.asyncio
async def test_shared_mode_upstream_change_restarts_owned_service(
    tmp_path: Path, config_file: Path
):
    """I6 truthfulness: an upstream-only change must restart the OWNED
    service — the running process keeps its old wiring until restarted, and
    the registry entry must match reality (review fix)."""
    cfg = load_config(config_file)
    state = ServiceState(config=cfg.pipeline[0])
    state.adopted = False
    state.identity = "id-old"
    state.upstream_url = "http://old-upstream"
    pipeline = PipelineState(services=[state])

    new_cfg = ManifoldConfig(gateway=cfg.gateway, pipeline=cfg.pipeline)

    with patch("manifold.paths.PID_DIR", tmp_path):
        with (
            patch("manifold.paths.is_port_in_use", return_value=False),
            patch(
                "manifold.process.restart_service", new_callable=AsyncMock
            ) as restart_mock,
        ):
            await _apply_config_changes(
                new_cfg, pipeline, cfg.gateway, gw_port=9000, gw_pid=123
            )
        restart_mock.assert_awaited_once()
        # The identity changed (new upstream) — old entry must be gone.
        assert registry.read_service_entry("id-old") is None


@pytest.mark.asyncio
async def test_shared_mode_upstream_change_releases_adopted_service(
    tmp_path: Path, config_file: Path
):
    """Adopted + upstream change: release tracking and replan for the new
    wiring (never touch the foreign process)."""
    cfg = load_config(config_file)
    state = ServiceState(config=cfg.pipeline[0])
    state.adopted = True
    state.identity = "id-old"
    state.upstream_url = "http://old-upstream"
    pipeline = PipelineState(services=[state])

    new_cfg = ManifoldConfig(gateway=cfg.gateway, pipeline=cfg.pipeline)

    with patch("manifold.paths.PID_DIR", tmp_path):
        with (
            patch("manifold.paths.is_port_in_use", return_value=False),
            patch(
                "manifold.process.release_service", new_callable=AsyncMock
            ) as release_mock,
            patch(
                "manifold.process.start_service", new_callable=AsyncMock
            ) as start_mock,
        ):
            await _apply_config_changes(
                new_cfg, pipeline, cfg.gateway, gw_port=9000, gw_pid=123
            )
        release_mock.assert_awaited_once()
        # Replan: old wiring is gone; nothing occupies the port in the fake
        # world, so this gateway spawns its own copy.
        start_mock.assert_awaited_once()
        assert pipeline.services[0].adopted is False
