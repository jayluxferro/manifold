"""Tests for manifold.watcher module."""

import asyncio
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from manifold.config import load_config
from manifold.models import PipelineState, ServiceState
from manifold.watcher import watch_config


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
