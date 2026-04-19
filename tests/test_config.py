"""Tests for manifold.config module."""

import textwrap
from pathlib import Path

import pytest

from manifold.config import ConfigError, load_config
from manifold.models import UpstreamVia


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    """Write a valid manifold.yaml and return its path."""
    dir_a = tmp_path / "svc-a"
    dir_b = tmp_path / "svc-b"
    dir_a.mkdir()
    dir_b.mkdir()
    content = textwrap.dedent(
        f"""\
        gateway:
          host: 127.0.0.1
          port: 9000

        pipeline:
          - name: svc-a
            directory: {dir_a.as_posix()}
            command: "echo svc-a --port {{port}}"
            port: 7001
            health: /healthz
            stats: /stats
            config_file: config.yaml
            upstream_key: upstream.endpoint
            upstream_via: config_file
            enabled: true

          - name: svc-b
            directory: {dir_b.as_posix()}
            command: "echo svc-b --port {{port}} --upstream {{upstream}}"
            port: 7002
            health: /health
            upstream_via: cli_arg
            enabled: true
    """
    )
    p = tmp_path / "manifold.yaml"
    p.write_text(content)
    return p


def test_load_valid_config(config_file: Path):
    cfg = load_config(config_file)
    assert cfg.gateway.host == "127.0.0.1"
    assert cfg.gateway.port == 9000
    assert len(cfg.pipeline) == 2
    assert cfg.pipeline[0].name == "svc-a"
    assert cfg.pipeline[0].upstream_via == UpstreamVia.CONFIG_FILE
    assert cfg.pipeline[1].upstream_via == UpstreamVia.CLI_ARG


def test_missing_config_file():
    with pytest.raises(ConfigError, match="not found"):
        load_config("/nonexistent/manifold.yaml")


def test_duplicate_names(tmp_path: Path):
    content = textwrap.dedent("""\
        pipeline:
          - name: dup
            directory: /tmp
            command: "echo"
            port: 7001
            health: /h
            upstream_via: cli_arg
          - name: dup
            directory: /tmp
            command: "echo"
            port: 7002
            health: /h
            upstream_via: cli_arg
    """)
    p = tmp_path / "manifold.yaml"
    p.write_text(content)
    with pytest.raises(ConfigError, match="Duplicate service names"):
        load_config(p)


def test_duplicate_ports(tmp_path: Path):
    content = textwrap.dedent("""\
        pipeline:
          - name: a
            directory: /tmp
            command: "echo"
            port: 7001
            health: /h
            upstream_via: cli_arg
          - name: b
            directory: /tmp
            command: "echo"
            port: 7001
            health: /h
            upstream_via: cli_arg
    """)
    p = tmp_path / "manifold.yaml"
    p.write_text(content)
    with pytest.raises(ConfigError, match="Duplicate service ports"):
        load_config(p)


def test_gateway_port_conflict(tmp_path: Path):
    content = textwrap.dedent("""\
        gateway:
          port: 7001
        pipeline:
          - name: a
            directory: /tmp
            command: "echo"
            port: 7001
            health: /h
            upstream_via: cli_arg
    """)
    p = tmp_path / "manifold.yaml"
    p.write_text(content)
    with pytest.raises(ConfigError, match="conflicts"):
        load_config(p)


def test_missing_required_field(tmp_path: Path):
    content = textwrap.dedent("""\
        pipeline:
          - name: a
            directory: /tmp
            health: /h
    """)
    p = tmp_path / "manifold.yaml"
    p.write_text(content)
    with pytest.raises(ConfigError, match="missing required"):
        load_config(p)


def test_config_file_requires_upstream_key(tmp_path: Path):
    content = textwrap.dedent("""\
        pipeline:
          - name: a
            directory: /tmp
            command: "echo"
            port: 7001
            health: /h
            config_file: config.yaml
            upstream_via: config_file
    """)
    p = tmp_path / "manifold.yaml"
    p.write_text(content)
    with pytest.raises(ConfigError, match="upstream_key"):
        load_config(p)


def test_gateway_startup_health_options(tmp_path: Path):
    content = textwrap.dedent("""\
        gateway:
          host: 0.0.0.0
          port: 9000
          startup_health_timeout: 60
          startup_health_poll_interval: 0.5
          startup_health_strict: true
        pipeline:
          - name: a
            directory: /tmp
            command: "echo"
            port: 7001
            health: /h
            upstream_via: cli_arg
    """)
    p = tmp_path / "manifold.yaml"
    p.write_text(content)
    cfg = load_config(p)
    assert cfg.gateway.startup_health_timeout == 60.0
    assert cfg.gateway.startup_health_poll_interval == 0.5
    assert cfg.gateway.startup_health_strict is True


def test_gateway_startup_health_invalid_timeout(tmp_path: Path):
    content = textwrap.dedent("""\
        gateway:
          startup_health_timeout: 0
        pipeline:
          - name: a
            directory: /tmp
            command: "echo"
            port: 7001
            health: /h
            upstream_via: cli_arg
    """)
    p = tmp_path / "manifold.yaml"
    p.write_text(content)
    with pytest.raises(ConfigError, match="startup_health_timeout"):
        load_config(p)


def test_gateway_startup_health_poll_gt_timeout(tmp_path: Path):
    content = textwrap.dedent("""\
        gateway:
          startup_health_timeout: 1
          startup_health_poll_interval: 2
        pipeline:
          - name: a
            directory: /tmp
            command: "echo"
            port: 7001
            health: /h
            upstream_via: cli_arg
    """)
    p = tmp_path / "manifold.yaml"
    p.write_text(content)
    with pytest.raises(ConfigError, match="poll_interval"):
        load_config(p)


def test_default_gateway_values(tmp_path: Path):
    content = textwrap.dedent("""\
        pipeline:
          - name: a
            directory: /tmp
            command: "echo"
            port: 7001
            health: /h
            upstream_via: cli_arg
    """)
    p = tmp_path / "manifold.yaml"
    p.write_text(content)
    cfg = load_config(p)
    assert cfg.gateway.host == "127.0.0.1"
    assert cfg.gateway.port == 9000
    assert cfg.gateway.fallback_upstream == "https://api.anthropic.com"
    assert cfg.gateway.startup_health_timeout == 120.0
    assert cfg.gateway.startup_health_poll_interval == 0.25
    assert cfg.gateway.startup_health_strict is False
