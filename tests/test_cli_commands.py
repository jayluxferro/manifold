"""Tests for CLI commands: down, stats, validate, add."""

import json
import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from manifold.cli import PID_DIR, PID_FILE, PORT_FILE, app

runner = CliRunner()


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


def test_validate_valid(config_file: Path):
    result = runner.invoke(app, ["validate", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "Valid" in result.output


def test_validate_invalid(tmp_path: Path):
    p = tmp_path / "bad.yaml"
    p.write_text("pipeline: not_a_list")
    result = runner.invoke(app, ["validate", "--config", str(p)])
    assert result.exit_code == 1
    assert "Invalid" in result.output


def test_status_shows_chain(config_file: Path):
    result = runner.invoke(app, ["status", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "svc-a" in result.output
    assert "Chain:" in result.output


def test_down_no_pidfile(tmp_path: Path):
    with patch("manifold.cli.PID_FILE", tmp_path / "no.pid"):
        result = runner.invoke(app, ["down"])
        assert result.exit_code == 1
        assert "No running" in result.output


def test_down_stale_pid(tmp_path: Path):
    pid_file = tmp_path / "manifold.pid"
    port_file = tmp_path / "manifold.port"
    pid_file.write_text("999999999")  # unlikely to exist
    with patch("manifold.cli.PID_FILE", pid_file), \
         patch("manifold.cli.PORT_FILE", port_file):
        result = runner.invoke(app, ["down"])
        assert result.exit_code == 0
        assert "not found" in result.output
        assert not pid_file.exists()


def test_stats_no_gateway(config_file: Path, tmp_path: Path):
    with patch("manifold.cli.PORT_FILE", tmp_path / "no.port"), \
         patch("manifold.cli._read_gateway_address", return_value="127.0.0.1:19999"):
        result = runner.invoke(app, ["stats", "--config", str(config_file)])
        assert result.exit_code == 1


def test_add_command(tmp_path: Path):
    config_file = tmp_path / "manifold.yaml"
    config_file.write_text("gateway:\n  port: 9000\npipeline: []\n")

    result = runner.invoke(app, ["add", "--config", str(config_file)], input=(
        "new-svc\n"
        "/tmp/new-svc\n"
        "echo start --port {port} --upstream {upstream}\n"
        "7099\n"
        "/healthz\n"
        "\n"  # skip stats
        "cli_arg\n"
    ))
    assert result.exit_code == 0
    assert "Added 'new-svc'" in result.output

    import yaml
    with open(config_file) as f:
        data = yaml.safe_load(f)
    assert len(data["pipeline"]) == 1
    assert data["pipeline"][0]["name"] == "new-svc"
    assert data["pipeline"][0]["port"] == 7099
