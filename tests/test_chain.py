"""Tests for manifold.chain module."""

import textwrap
from pathlib import Path

import yaml

from manifold.chain import (
    _deep_get,
    _deep_set,
    compute_upstreams,
    get_entry_url,
    patch_service_config,
    resolve_command,
)
from manifold.models import (
    GatewayConfig,
    PipelineState,
    ServiceConfig,
    ServiceState,
    ServiceStatus,
    UpstreamVia,
)


def _svc(
    name: str, port: int, enabled: bool = True, via: UpstreamVia = UpstreamVia.CLI_ARG
) -> ServiceConfig:
    return ServiceConfig(
        name=name,
        directory="/tmp",
        command=f"echo {name} --port {{port}} --upstream {{upstream}}",
        port=port,
        health="/h",
        upstream_via=via,
        enabled=enabled,
    )


class TestDeepSetGet:
    def test_shallow(self):
        d = {"a": "old"}
        _deep_set(d, "a", "new")
        assert d["a"] == "new"
        assert _deep_get(d, "a") == "new"

    def test_nested(self):
        d = {"x": {"y": {"z": "old"}}}
        _deep_set(d, "x.y.z", "new")
        assert d["x"]["y"]["z"] == "new"
        assert _deep_get(d, "x.y.z") == "new"

    def test_creates_intermediate(self):
        d = {}
        _deep_set(d, "a.b.c", "val")
        assert d["a"]["b"]["c"] == "val"

    def test_get_missing(self):
        assert _deep_get({}, "a.b") is None
        assert _deep_get({"a": 1}, "a.b") is None


class TestComputeUpstreams:
    def test_single_service(self):
        services = [_svc("a", 7001)]
        result = compute_upstreams(services, "https://api.anthropic.com")
        assert result == {"a": "https://api.anthropic.com"}

    def test_chain_of_three(self):
        services = [_svc("a", 7001), _svc("b", 7002), _svc("c", 7003)]
        result = compute_upstreams(services, "https://api.anthropic.com")
        assert result == {
            "a": "http://127.0.0.1:7002",
            "b": "http://127.0.0.1:7003",
            "c": "https://api.anthropic.com",
        }

    def test_disabled_skipped(self):
        services = [
            _svc("a", 7001),
            _svc("b", 7002, enabled=False),
            _svc("c", 7003),
        ]
        result = compute_upstreams(services, "https://api.anthropic.com")
        assert "b" not in result
        assert result["a"] == "http://127.0.0.1:7003"
        assert result["c"] == "https://api.anthropic.com"


class TestResolveCommand:
    def test_template_substitution(self):
        svc = _svc("test", 8080)
        cmd = resolve_command(svc, "http://upstream:9090")
        assert cmd == "echo test --port 8080 --upstream http://upstream:9090"


class TestPatchServiceConfig:
    def test_patches_yaml(self, tmp_path: Path):
        config_content = textwrap.dedent("""\
            cloud_target:
              endpoint: https://api.anthropic.com
              model: claude-3
        """)
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(config_content)

        svc = ServiceConfig(
            name="test",
            directory=str(tmp_path),
            command="echo",
            port=7001,
            health="/h",
            config_file="config.yaml",
            upstream_key="cloud_target.endpoint",
            upstream_via=UpstreamVia.CONFIG_FILE,
        )

        patch_service_config(svc, "http://127.0.0.1:7002")

        data = yaml.safe_load(cfg_path.read_text())
        assert data["cloud_target"]["endpoint"] == "http://127.0.0.1:7002"
        assert data["cloud_target"]["model"] == "claude-3"  # preserved

    def test_skips_cli_arg_service(self, tmp_path: Path):
        svc = _svc("test", 7001, via=UpstreamVia.CLI_ARG)
        # Should not raise even though no config file exists
        patch_service_config(svc, "http://127.0.0.1:7002")


class TestGetEntryUrl:
    def test_returns_first_healthy(self):
        pipeline = PipelineState(
            services=[
                ServiceState(config=_svc("a", 7001), status=ServiceStatus.UNHEALTHY),
                ServiceState(config=_svc("b", 7002), status=ServiceStatus.HEALTHY),
            ]
        )
        gw = GatewayConfig()
        assert get_entry_url(pipeline, gw) == "http://127.0.0.1:7002"

    def test_returns_starting_if_no_healthy(self):
        pipeline = PipelineState(
            services=[
                ServiceState(config=_svc("a", 7001), status=ServiceStatus.STARTING),
            ]
        )
        gw = GatewayConfig()
        assert get_entry_url(pipeline, gw) == "http://127.0.0.1:7001"

    def test_returns_fallback_if_all_down(self):
        pipeline = PipelineState(
            services=[
                ServiceState(config=_svc("a", 7001), status=ServiceStatus.STOPPED),
            ]
        )
        gw = GatewayConfig()
        assert get_entry_url(pipeline, gw) == "https://api.anthropic.com"

    def test_skips_disabled(self):
        pipeline = PipelineState(
            services=[
                ServiceState(
                    config=_svc("a", 7001, enabled=False), status=ServiceStatus.HEALTHY
                ),
            ]
        )
        gw = GatewayConfig()
        assert get_entry_url(pipeline, gw) == "https://api.anthropic.com"
