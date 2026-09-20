"""Tests for manifold.chain module."""

import logging
import textwrap
from pathlib import Path

import pytest
import yaml

from manifold import chain as chain_module
from manifold.chain import (
    _deep_get,
    _deep_set,
    compute_upstreams,
    get_entry_route,
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
    name: str,
    port: int,
    enabled: bool = True,
    via: UpstreamVia = UpstreamVia.CLI_ARG,
    upstream_path: str = "",
) -> ServiceConfig:
    return ServiceConfig(
        name=name,
        directory="/tmp",
        command=f"echo {name} --port {{port}} --upstream {{upstream}}",
        port=port,
        health="/h",
        upstream_via=via,
        enabled=enabled,
        upstream_path=upstream_path,
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

    def test_upstream_path_on_mid_service(self):
        """upstream_path is appended to the next-service URL for mid-chain services."""
        services = [
            _svc("a", 7001, upstream_path="/v1"),
            _svc("b", 7002),
            _svc("c", 7003),
        ]
        result = compute_upstreams(services, "https://api.anthropic.com")
        assert result["a"] == "http://127.0.0.1:7002/v1"
        assert result["b"] == "http://127.0.0.1:7003"
        assert result["c"] == "https://api.anthropic.com"

    def test_upstream_path_on_last_service(self):
        """upstream_path is appended to fallback_upstream for the last service."""
        services = [
            _svc("a", 7001),
            _svc("b", 7002, upstream_path="/v1"),
        ]
        result = compute_upstreams(services, "https://api.example.com")
        assert result["a"] == "http://127.0.0.1:7002"
        assert result["b"] == "https://api.example.com/v1"

    def test_upstream_path_only_service(self):
        """A single service with upstream_path appends to fallback_upstream."""
        services = [_svc("only", 7001, upstream_path="/v1")]
        result = compute_upstreams(services, "https://api.example.com")
        assert result["only"] == "https://api.example.com/v1"

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


class TestResolveCommandDefensive:
    """M5: .format() turned every shell brace into a template and died with a
    raw KeyError at `up` time.  Only identifier-shaped unknown placeholders
    are errors; literal shell braces pass through."""

    def test_unknown_identifier_placeholder_raises_clear_error(self):
        svc = _svc("test", 8080)
        svc.command = "run --port {port} --flag {oops}"
        with pytest.raises(ValueError, match=r"\{oops\}"):
            resolve_command(svc, "http://upstream:9090")

    def test_error_names_command_and_the_valid_placeholders(self):
        svc = _svc("test", 8080)
        svc.command = "deploy {oops} --port {port}"
        with pytest.raises(
            ValueError, match=r"deploy \{oops\}.*\{port\} and \{upstream\}"
        ):
            resolve_command(svc, "http://upstream:9090")

    def test_awk_braces_pass_through(self):
        """awk '{print $1}' is not identifier-shaped inside the braces —
        literal shell syntax, not a placeholder."""
        svc = _svc("test", 8080)
        svc.command = "awk '{print $1}' --port {port} --upstream {upstream}"
        cmd = resolve_command(svc, "http://upstream:9090")
        assert cmd == "awk '{print $1}' --port 8080 --upstream http://upstream:9090"

    def test_empty_braces_pass_through(self):
        """Literal `{}` (find/xargs style, shell brace expansion) must not
        IndexError the way str.format's positional slot did."""
        svc = _svc("test", 8080)
        svc.command = "mytool {} --port {port}"
        assert resolve_command(svc, "http://u") == "mytool {} --port 8080"

    def test_numeric_braces_pass_through(self):
        svc = _svc("test", 8080)
        svc.command = "mytool {0} --port {port}"
        assert resolve_command(svc, "http://u") == "mytool {0} --port 8080"

    def test_double_braces_still_escape(self):
        svc = _svc("test", 8080)
        svc.command = "cmd {{literal}} --port {port}"
        assert resolve_command(svc, "http://u") == "cmd {literal} --port 8080"


@pytest.fixture(autouse=True)
def _reset_bypass_warnings():
    """Keep the module-level warned-set from leaking between tests."""
    chain_module._entry_bypass_warned.clear()
    yield
    chain_module._entry_bypass_warned.clear()


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

    def test_returns_none_if_all_down(self):
        pipeline = PipelineState(
            services=[
                ServiceState(config=_svc("a", 7001), status=ServiceStatus.STOPPED),
            ]
        )
        gw = GatewayConfig()
        assert get_entry_url(pipeline, gw) is None

    def test_skips_disabled(self):
        pipeline = PipelineState(
            services=[
                ServiceState(
                    config=_svc("a", 7001, enabled=False), status=ServiceStatus.HEALTHY
                ),
            ]
        )
        gw = GatewayConfig()
        assert get_entry_url(pipeline, gw) is None


class TestEntryBypassPrivacyWarning:
    """c3: an entry-hop bypass of the redactor is a silent privacy event no
    more — one warning per bypass episode."""

    def _pipeline(self):
        return PipelineState(
            services=[
                ServiceState(
                    config=_svc("llm-redactor", 7001),
                    status=ServiceStatus.UNHEALTHY,
                ),
                ServiceState(
                    config=_svc("hivemind", 7002),
                    status=ServiceStatus.HEALTHY,
                ),
            ]
        )

    def test_bypass_warns_privacy_degradation(self, caplog):
        pipeline = self._pipeline()
        with caplog.at_level(logging.WARNING, logger="manifold.chain"):
            url = get_entry_url(pipeline, GatewayConfig())
        assert url == "http://127.0.0.1:7002"
        privacy = [r for r in caplog.records if "privacy" in r.getMessage().lower()]
        assert len(privacy) == 1
        assert "llm-redactor" in privacy[0].getMessage()

    def test_warns_once_per_episode(self, caplog):
        pipeline = self._pipeline()
        with caplog.at_level(logging.WARNING, logger="manifold.chain"):
            get_entry_url(pipeline, GatewayConfig())
            get_entry_url(pipeline, GatewayConfig())  # per-request calls must not spam
        assert (
            len([r for r in caplog.records if "privacy" in r.getMessage().lower()]) == 1
        )

    def test_rewarns_after_recovery(self, caplog):
        pipeline = self._pipeline()
        gw = GatewayConfig()
        with caplog.at_level(logging.WARNING, logger="manifold.chain"):
            get_entry_url(pipeline, gw)
            pipeline.services[0].status = ServiceStatus.HEALTHY
            get_entry_url(pipeline, gw)  # entry returns to the redactor
            pipeline.services[0].status = ServiceStatus.UNHEALTHY
            get_entry_url(pipeline, gw)  # new bypass episode
        assert (
            len([r for r in caplog.records if "privacy" in r.getMessage().lower()]) == 2
        )

    def test_no_warning_for_non_redactor(self, caplog):
        pipeline = PipelineState(
            services=[
                ServiceState(
                    config=_svc("veritas", 7001),
                    status=ServiceStatus.UNHEALTHY,
                ),
                ServiceState(
                    config=_svc("hivemind", 7002),
                    status=ServiceStatus.HEALTHY,
                ),
            ]
        )
        with caplog.at_level(logging.WARNING, logger="manifold.chain"):
            assert get_entry_url(pipeline, GatewayConfig()) == "http://127.0.0.1:7002"
        assert not [r for r in caplog.records if "privacy" in r.getMessage().lower()]


class TestGetEntryRoute:
    """get_entry_route is get_entry_url plus the per-request bypass names the
    gateway stamps onto responses as x-manifold-bypassed."""

    def test_returns_url_and_bypassed_names(self):
        pipeline = PipelineState(
            services=[
                ServiceState(config=_svc("a", 7001), status=ServiceStatus.UNHEALTHY),
                ServiceState(config=_svc("b", 7002), status=ServiceStatus.UNHEALTHY),
                ServiceState(config=_svc("c", 7003), status=ServiceStatus.HEALTHY),
            ]
        )
        url, bypassed = get_entry_route(pipeline, GatewayConfig())
        assert url == "http://127.0.0.1:7003"
        assert bypassed == ["a", "b"]

    def test_healthy_chain_bypasses_nothing(self):
        pipeline = PipelineState(
            services=[
                ServiceState(config=_svc("a", 7001), status=ServiceStatus.HEALTHY)
            ]
        )
        url, bypassed = get_entry_route(pipeline, GatewayConfig())
        assert url == "http://127.0.0.1:7001"
        assert bypassed == []

    def test_fully_down_yields_no_url_and_all_names(self):
        pipeline = PipelineState(
            services=[
                ServiceState(config=_svc("a", 7001), status=ServiceStatus.STOPPED),
                ServiceState(config=_svc("b", 7002), status=ServiceStatus.STOPPED),
            ]
        )
        url, bypassed = get_entry_route(pipeline, GatewayConfig())
        assert url is None
        assert bypassed == ["a", "b"]

    def test_disabled_services_are_not_bypasses(self):
        pipeline = PipelineState(
            services=[
                ServiceState(
                    config=_svc("a", 7001, enabled=False),
                    status=ServiceStatus.STOPPED,
                ),
                ServiceState(config=_svc("b", 7002), status=ServiceStatus.HEALTHY),
            ]
        )
        url, bypassed = get_entry_route(pipeline, GatewayConfig())
        assert url == "http://127.0.0.1:7002"
        assert bypassed == []
