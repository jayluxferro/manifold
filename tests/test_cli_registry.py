"""Tests for the shared-service registry wiring in the CLI (workstream C).

Covers: port overrides (shared vs isolated), shared preflight, the
plan/adopt/promote/spawn decision loop, full ``manifold up`` runs against a
faked uvicorn, registry teardown, and the rewritten ``down``.
"""

import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from manifold import registry, service_ops
from manifold.cli import (
    _apply_port_override,
    _down_one,
    _preflight_check,
    _run_pipeline,
    _shutdown_pipeline,
    app,
)
from manifold.config import load_config
from manifold.models import (
    GatewayConfig,
    ManifoldConfig,
    PipelineState,
    ServiceConfig,
    ServiceState,
    UpstreamVia,
)

runner = CliRunner()

FALLBACK = "https://api.anthropic.com"


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    return _config_file(tmp_path)


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


def _cfg(**overrides) -> ManifoldConfig:
    svcs = overrides.pop("services", [_svc()])
    return ManifoldConfig(gateway=GatewayConfig(port=9000), pipeline=svcs)


def _config_file(tmp_path: Path, name: str = "svc-a", port: int = 7001) -> Path:
    p = tmp_path / "manifold.yaml"
    p.write_text(
        f"""\
gateway:
  host: 127.0.0.1
  port: 9000
pipeline:
  - name: {name}
    directory: /tmp
    command: "echo a --port {{port}} --upstream {{upstream}}"
    port: {port}
    health: /h
    upstream_via: cli_arg
    enabled: true
"""
    )
    return p


def _entry(
    identity: str,
    name: str = "svc-a",
    port: int = 7001,
    owner_port: int = 9000,
    pid: int | None = None,
    owner_pid: int | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "identity": identity,
        "name": name,
        "directory": "/tmp",
        "command": f"echo a --port {port} --upstream {FALLBACK}",
        "port": port,
        "upstream": FALLBACK,
        "pid": pid if pid is not None else os.getpid(),
        "pgid": pid if pid is not None else os.getpid(),
        "owner_port": owner_port,
        "owner_pid": owner_pid if owner_pid is not None else os.getpid(),
        "started_at": 0.0,
    }


class _FakeUvicornServer:
    """Minimal stand-in for uvicorn.Server so `up` runs to completion."""

    def __init__(self, config):
        self.config = config
        self.lifespan = None
        self.started = False
        self.should_exit = False

    async def startup(self):
        self.started = True

    async def main_loop(self):
        pass

    async def shutdown(self):
        pass


def _patch_up_runtime(stack: ExitStack, start_mock=None) -> AsyncMock:
    """Patch everything that would block or run forever during `manifold up`."""
    stack.enter_context(
        patch("manifold.cli.wait_for_services_ready", new_callable=AsyncMock)
    )
    stack.enter_context(patch("manifold.cli.health_loop", new_callable=AsyncMock))
    stack.enter_context(patch("manifold.cli.watch_config", new_callable=AsyncMock))
    stack.enter_context(patch("manifold.cli.uvicorn.Server", new=_FakeUvicornServer))
    if start_mock is None:
        start_mock = AsyncMock()
    stack.enter_context(patch("manifold.process.start_service", start_mock))
    return start_mock


# --- _apply_port_override ---------------------------------------------------


def test_port_override_shared_moves_gateway_only():
    cfg = _cfg(services=[_svc(name="a", port=7001), _svc(name="b", port=7002)])
    _apply_port_override(cfg, 9001, isolated=False)
    assert cfg.gateway.port == 9001
    assert [s.port for s in cfg.pipeline] == [7001, 7002]


def test_port_override_isolated_offsets_gateway_and_services():
    cfg = _cfg(services=[_svc(name="a", port=7001), _svc(name="b", port=7002)])
    _apply_port_override(cfg, 9001, isolated=True)
    assert cfg.gateway.port == 9001
    assert [s.port for s in cfg.pipeline] == [7002, 7003]


def test_port_override_none_is_noop():
    cfg = _cfg(services=[_svc(name="a", port=7001)])
    _apply_port_override(cfg, None, isolated=False)
    assert cfg.gateway.port == 9000
    assert cfg.pipeline[0].port == 7001


# --- shared preflight -------------------------------------------------------


def test_preflight_shared_same_identity_is_ok(tmp_path: Path):
    svc = _svc()
    identity = registry.compute_service_identity(svc, FALLBACK)
    cfg = _cfg(services=[svc])
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(_entry(identity, owner_port=9001))
        with patch(
            "manifold.paths.is_port_in_use",
            side_effect=lambda port, host="127.0.0.1": port == 7001,
        ):
            warnings = _preflight_check(cfg, shared=True)
    assert warnings == []


def test_preflight_shared_different_wiring_is_error(tmp_path: Path):
    svc = _svc()
    other = registry.compute_service_identity(_svc(port=7002), FALLBACK)
    cfg = _cfg(services=[svc])
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(_entry(other, owner_port=9001, port=7001))
        with patch(
            "manifold.paths.is_port_in_use",
            side_effect=lambda port, host="127.0.0.1": port == 7001,
        ):
            with pytest.raises(typer.Exit) as excinfo:
                _preflight_check(cfg, shared=True)
    assert excinfo.value.exit_code == 1


def test_preflight_shared_non_manifold_process_is_error(tmp_path: Path):
    svc = _svc()
    cfg = _cfg(services=[svc])
    with patch("manifold.paths.PID_DIR", tmp_path):
        with patch(
            "manifold.paths.is_port_in_use",
            side_effect=lambda port, host="127.0.0.1": port == 7001,
        ):
            with pytest.raises(typer.Exit) as excinfo:
                _preflight_check(cfg, shared=True)
    assert excinfo.value.exit_code == 1


def test_preflight_gateway_port_in_use_is_error(tmp_path: Path):
    cfg = _cfg()
    with patch("manifold.paths.PID_DIR", tmp_path):
        with patch(
            "manifold.paths.is_port_in_use",
            side_effect=lambda port, host="127.0.0.1": port == 9000,
        ):
            with pytest.raises(typer.Exit) as excinfo:
                _preflight_check(cfg, shared=True)
    assert excinfo.value.exit_code == 1


# --- _plan_service ----------------------------------------------------------


def test_plan_service_adopt(tmp_path: Path):
    svc = _svc()
    identity = registry.compute_service_identity(svc, FALLBACK)
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(_entry(identity, owner_port=9001))
        decision, payload = service_ops._plan_service(svc, FALLBACK)
    assert decision == "adopt"
    assert payload["identity"] == identity


def test_plan_service_promote_when_owner_dead(tmp_path: Path):
    svc = _svc()
    identity = registry.compute_service_identity(svc, FALLBACK)
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(
            _entry(identity, owner_port=9001, owner_pid=999999999)
        )
        decision, payload = service_ops._plan_service(svc, FALLBACK)
        assert decision == "promote"
        assert payload["identity"] == identity
        assert registry.read_service_entry(identity) is not None


def test_plan_service_spawn_when_port_free(tmp_path: Path):
    svc = _svc()
    with patch("manifold.paths.PID_DIR", tmp_path):
        with patch("manifold.paths.is_port_in_use", return_value=False):
            decision, payload = service_ops._plan_service(svc, FALLBACK)
    assert decision == "spawn"
    assert payload is None


def test_plan_service_error_when_port_taken(tmp_path: Path):
    svc = _svc()
    with patch("manifold.paths.PID_DIR", tmp_path):
        with patch("manifold.paths.is_port_in_use", return_value=True):
            decision, payload = service_ops._plan_service(svc, FALLBACK)
    assert decision == "error"
    assert "7001" in str(payload)


def test_plan_service_removes_stale_entry(tmp_path: Path):
    svc = _svc()
    identity = registry.compute_service_identity(svc, FALLBACK)
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(
            _entry(identity, pid=999999999, owner_pid=999999999)
        )
        with patch("manifold.paths.is_port_in_use", return_value=False):
            decision, _ = service_ops._plan_service(svc, FALLBACK)
        assert registry.read_service_entry(identity) is None
    assert decision == "spawn"


# --- `manifold up` end-to-end -----------------------------------------------


def test_up_adopts_live_entry_no_spawn(tmp_path: Path):
    config_file = _config_file(tmp_path)
    cfg = load_config(config_file)
    identity = registry.compute_service_identity(cfg.pipeline[0], FALLBACK)
    lease_writes = []
    real_write_lease = registry.write_lease

    def _record_lease(port, pid, identities, isolated=False):
        lease_writes.append((port, pid, list(identities), isolated))
        real_write_lease(port, pid, identities, isolated)

    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(_entry(identity, owner_port=9000))
        with patch("manifold.paths.is_port_in_use", return_value=False):
            with ExitStack() as stack:
                start_mock = _patch_up_runtime(stack)
                stack.enter_context(
                    patch("manifold.registry.write_lease", side_effect=_record_lease)
                )
                result = runner.invoke(app, ["up", "--config", str(config_file)])
        assert result.exit_code == 0, result.output
        start_mock.assert_not_awaited()
        # lease written before the first spawn AND refreshed after the loop
        assert lease_writes == [(9000, os.getpid(), [identity], False)] * 2
        # adopted entry untouched by this gateway (still owned by :9000)
        assert registry.read_service_entry(identity)["owner_port"] == 9000


def test_up_spawns_writes_entry_and_lease(tmp_path: Path):
    config_file = _config_file(tmp_path)
    cfg = load_config(config_file)
    identity = registry.compute_service_identity(cfg.pipeline[0], FALLBACK)
    entry_writes = []
    entry_removals = []
    lease_writes = []
    real_write_entry = registry.write_service_entry
    real_remove_entry = registry.remove_service_entry
    real_write_lease = registry.write_lease

    def _rec_entry_write(entry):
        entry_writes.append(dict(entry))
        return real_write_entry(entry)

    def _rec_entry_remove(identity):
        entry_removals.append(identity)
        return real_remove_entry(identity)

    def _rec_lease(port, pid, identities, isolated=False):
        lease_writes.append((port, pid, list(identities), isolated))
        return real_write_lease(port, pid, identities, isolated)

    with patch("manifold.paths.PID_DIR", tmp_path):
        with patch("manifold.paths.is_port_in_use", return_value=False):
            with ExitStack() as stack:
                start_mock = _patch_up_runtime(stack)
                stack.enter_context(
                    patch(
                        "manifold.registry.write_service_entry",
                        side_effect=_rec_entry_write,
                    )
                )
                stack.enter_context(
                    patch(
                        "manifold.registry.remove_service_entry",
                        side_effect=_rec_entry_remove,
                    )
                )
                stack.enter_context(
                    patch("manifold.registry.write_lease", side_effect=_rec_lease)
                )
                result = runner.invoke(app, ["up", "--config", str(config_file)])
        assert result.exit_code == 0, result.output
        start_mock.assert_awaited_once()
        assert len(entry_writes) == 1
        written = entry_writes[0]
        assert written["identity"] == identity
        assert written["owner_port"] == 9000
        assert written["owner_pid"] == os.getpid()
        assert written["port"] == 7001
        assert lease_writes == [(9000, os.getpid(), [identity], False)] * 2
        # owned service was stopped + entry removed at shutdown
        assert entry_removals == [identity]


def test_up_promotes_reclaims_dead_owner(tmp_path: Path):
    config_file = _config_file(tmp_path)
    cfg = load_config(config_file)
    identity = registry.compute_service_identity(cfg.pipeline[0], FALLBACK)
    entry_writes = []
    entry_removals = []
    real_write_entry = registry.write_service_entry
    real_remove_entry = registry.remove_service_entry
    kill_mock = MagicMock(return_value=True)

    def _rec_entry_write(entry):
        entry_writes.append(dict(entry))
        return real_write_entry(entry)

    def _rec_entry_remove(identity):
        entry_removals.append(identity)
        return real_remove_entry(identity)

    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(
            _entry(identity, owner_port=9000, owner_pid=999999999)
        )
        with patch("manifold.paths.is_port_in_use", return_value=False):
            with ExitStack() as stack:
                start_mock = _patch_up_runtime(stack)
                stack.enter_context(
                    patch("manifold.registry.kill_entry_processes", kill_mock)
                )
                stack.enter_context(
                    patch(
                        "manifold.registry.write_service_entry",
                        side_effect=_rec_entry_write,
                    )
                )
                stack.enter_context(
                    patch(
                        "manifold.registry.remove_service_entry",
                        side_effect=_rec_entry_remove,
                    )
                )
                result = runner.invoke(app, ["up", "--config", str(config_file)])
        assert result.exit_code == 0, result.output
        start_mock.assert_awaited_once()
        kill_mock.assert_called_once()
        assert kill_mock.call_args[0][0]["identity"] == identity
        assert len(entry_writes) == 1
        assert entry_writes[0]["owner_port"] == 9000
        assert entry_writes[0]["owner_pid"] == os.getpid()
        # old entry removed by reclaim, new one by shutdown
        assert entry_removals == [identity, identity]


def test_up_isolated_offsets_service_ports(tmp_path: Path):
    config_file = _config_file(tmp_path)
    cfg = load_config(config_file)
    shared_identity = registry.compute_service_identity(cfg.pipeline[0], FALLBACK)
    entry_writes = []
    real_write_entry = registry.write_service_entry

    def _rec_entry_write(entry):
        entry_writes.append(dict(entry))
        return real_write_entry(entry)

    with patch("manifold.paths.PID_DIR", tmp_path):
        with patch("manifold.paths.is_port_in_use", return_value=False):
            with ExitStack() as stack:
                start_mock = _patch_up_runtime(stack)
                stack.enter_context(
                    patch(
                        "manifold.registry.write_service_entry",
                        side_effect=_rec_entry_write,
                    )
                )
                result = runner.invoke(
                    app,
                    [
                        "up",
                        "--config",
                        str(config_file),
                        "--port",
                        "9001",
                        "--isolated",
                    ],
                )
        assert result.exit_code == 0, result.output
        start_mock.assert_awaited_once()
        assert len(entry_writes) == 1
        written = entry_writes[0]
        # isolated: services offset by the gateway delta (+1) → distinct wiring
        assert written["port"] == 7002
        assert written["identity"] != shared_identity


# --- spawn contention -------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_owned_lock_contention_adopts(tmp_path: Path):
    identity = "id-lock"
    state = ServiceState(config=_svc())
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(_entry(identity, owner_port=9001))
        assert registry.acquire_spawn_lock(identity)  # we hold the lock
        try:
            result = await service_ops._spawn_owned(
                state, FALLBACK, identity, 9000, 123
            )
        finally:
            registry.release_spawn_lock(identity)
    assert result is False
    assert state.adopted is True
    assert state.identity == identity
    assert state.owner_port == 9001


@pytest.mark.asyncio
async def test_spawn_owned_promote_reclaims_occupied_port(tmp_path: Path):
    """Promote must reclaim the port, not fail the port-busy check.

    The fake world is consistent with I4: a successful reclaim kill frees
    the port, so the post-reclaim port check passes too.
    """
    identity = "id-a"
    entry = _entry(identity, owner_port=9001, owner_pid=999999999)
    state = ServiceState(config=_svc())
    busy = {"on": True}

    def _kill(_entry: dict) -> bool:
        busy["on"] = False
        return True

    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(entry)
        with patch(
            "manifold.paths.is_port_in_use", side_effect=lambda *a, **k: busy["on"]
        ):
            with patch("manifold.registry.kill_entry_processes", side_effect=_kill):
                start_mock = AsyncMock()
                with patch("manifold.process.start_service", start_mock):
                    result = await service_ops._spawn_owned(
                        state, FALLBACK, identity, 9000, 111, reclaim_entry=entry
                    )
        assert result is True
        start_mock.assert_awaited_once()
        assert registry.read_service_entry(identity)["owner_port"] == 9000


@pytest.mark.asyncio
async def test_spawn_owned_port_busy_raises_without_reclaim(tmp_path: Path):
    """A plain spawn into an occupied port is a hard error."""
    identity = "id-a"
    state = ServiceState(config=_svc())
    with patch("manifold.paths.PID_DIR", tmp_path):
        with patch("manifold.paths.is_port_in_use", return_value=True):
            start_mock = AsyncMock()
            with patch("manifold.process.start_service", start_mock):
                with pytest.raises(typer.Exit):
                    await service_ops._spawn_owned(state, FALLBACK, identity, 9000, 111)
        start_mock.assert_not_awaited()
        assert registry.read_service_entry(identity) is None


# --- _shutdown_pipeline -----------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_pipeline_owned_no_lease_stops_and_removes(tmp_path: Path):
    identity = "id-a"
    state = ServiceState(config=_svc(name="a"), identity=identity, pid=4242)
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(_entry(identity, owner_port=9000))
        stop_mock = AsyncMock()
        rel_mock = AsyncMock()
        with (
            patch("manifold.process.stop_service", stop_mock),
            patch("manifold.process.release_service", rel_mock),
        ):
            await _shutdown_pipeline(PipelineState(services=[state]), 9000, 111)
        stop_mock.assert_awaited_once_with(state)
        rel_mock.assert_not_awaited()
        assert registry.read_service_entry(identity) is None
        assert registry.read_lease(9000) is None


@pytest.mark.asyncio
async def test_shutdown_pipeline_owned_surviving_lease_hands_off(tmp_path: Path):
    identity = "id-a"
    state = ServiceState(config=_svc(name="a"), identity=identity, pid=4242)
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(_entry(identity, owner_port=9000))
        registry.write_lease(9000, 111, [identity])
        registry.write_lease(9001, os.getpid(), [identity])
        stop_mock = AsyncMock()
        rel_mock = AsyncMock()
        with (
            patch("manifold.process.stop_service", stop_mock),
            patch("manifold.process.release_service", rel_mock),
        ):
            await _shutdown_pipeline(PipelineState(services=[state]), 9000, 111)
        stop_mock.assert_not_awaited()
        rel_mock.assert_awaited_once_with(state)
        entry = registry.read_service_entry(identity)
        assert entry["owner_port"] == 9001
        assert entry["owner_pid"] == os.getpid()
        assert registry.read_lease(9000) is None
        assert registry.read_lease(9001) is not None


@pytest.mark.asyncio
async def test_shutdown_pipeline_adopted_untouched(tmp_path: Path):
    identity = "id-a"
    state = ServiceState(
        config=_svc(name="a"),
        identity=identity,
        adopted=True,
        pid=4242,
        owner_port=9000,
    )
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(_entry(identity, owner_port=9000))
        stop_mock = AsyncMock()
        rel_mock = AsyncMock()
        with (
            patch("manifold.process.stop_service", stop_mock),
            patch("manifold.process.release_service", rel_mock),
        ):
            await _shutdown_pipeline(PipelineState(services=[state]), 9000, 111)
        stop_mock.assert_not_awaited()
        rel_mock.assert_not_awaited()
        assert registry.read_service_entry(identity) is not None


# --- _promote_adopted -------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_adopted_entry_missing_is_noop(tmp_path: Path):
    state = ServiceState(
        config=_svc(), adopted=True, identity="id-x", upstream_url=FALLBACK
    )
    with patch("manifold.paths.PID_DIR", tmp_path):
        start_mock = AsyncMock()
        with patch("manifold.process.start_service", start_mock):
            result = await service_ops._promote_adopted(state, 9000, 111)
        start_mock.assert_not_awaited()
    assert result is False


@pytest.mark.asyncio
async def test_promote_adopted_owner_alive_leaves_recovery_to_owner(
    tmp_path: Path,
):
    identity = "id-a"
    state = ServiceState(
        config=_svc(), adopted=True, identity=identity, upstream_url=FALLBACK
    )
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(_entry(identity, owner_port=9000))
        start_mock = AsyncMock()
        with patch("manifold.process.start_service", start_mock):
            result = await service_ops._promote_adopted(state, 9000, 111)
        start_mock.assert_not_awaited()
    assert result is False


@pytest.mark.asyncio
async def test_promote_adopted_dead_service_entry_removed(tmp_path: Path):
    identity = "id-a"
    state = ServiceState(
        config=_svc(), adopted=True, identity=identity, upstream_url=FALLBACK
    )
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(
            _entry(identity, pid=999999999, owner_pid=999999999)
        )
        start_mock = AsyncMock()
        with patch("manifold.process.start_service", start_mock):
            result = await service_ops._promote_adopted(state, 9000, 111)
        start_mock.assert_not_awaited()
        assert registry.read_service_entry(identity) is None
    assert result is False


@pytest.mark.asyncio
async def test_promote_adopted_reclaims_dead_owner(tmp_path: Path):
    identity = "id-a"
    state = ServiceState(
        config=_svc(), adopted=True, identity=identity, upstream_url=FALLBACK
    )
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(
            _entry(identity, owner_port=9000, owner_pid=999999999)
        )
        kill_mock = MagicMock(return_value=True)
        start_mock = AsyncMock()
        with (
            patch("manifold.paths.is_port_in_use", return_value=False),
            patch("manifold.registry.kill_entry_processes", kill_mock),
            patch("manifold.process.start_service", start_mock),
        ):
            result = await service_ops._promote_adopted(state, 9000, 111)
        kill_mock.assert_called_once()
        start_mock.assert_awaited_once()
        entry = registry.read_service_entry(identity)
        assert entry is not None
        assert entry["owner_port"] == 9000
        assert entry["owner_pid"] == 111
    assert result is True
    assert state.adopted is False


# --- down -------------------------------------------------------------------


def test_down_hung_gateway_reaps_registry(tmp_path: Path):
    identity = "id-a"
    (tmp_path / "manifold-9000.pid").write_text("999999999")
    (tmp_path / "manifold-9000.port").write_text("127.0.0.1:9000")
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(
            _entry(identity, owner_port=9000, owner_pid=999999999)
        )
        registry.write_lease(9000, 999999999, [identity])
        kill_mock = MagicMock(return_value=True)
        lsof_mock = MagicMock(return_value=[])
        with (
            patch("manifold.registry.kill_entry_processes", kill_mock),
            patch("manifold.cli._lsof_ports_for_config", lsof_mock),
        ):
            result = runner.invoke(app, ["down", "--port", "9000"])
        assert result.exit_code == 0, result.output
        kill_mock.assert_called_once()
        assert kill_mock.call_args[0][0]["identity"] == identity
        # lease existed → no legacy lsof fallback
        lsof_mock.assert_not_called()
        assert registry.read_service_entry(identity) is None
        assert registry.read_lease(9000) is None
        assert not (tmp_path / "manifold-9000.pid").exists()
        assert not (tmp_path / "manifold-9000.port").exists()


def test_down_transfers_survivor_to_live_lease(tmp_path: Path):
    identity = "id-a"
    (tmp_path / "manifold-9000.pid").write_text("999999999")
    (tmp_path / "manifold-9000.port").write_text("127.0.0.1:9000")
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(
            _entry(identity, owner_port=9000, owner_pid=999999999)
        )
        registry.write_lease(9000, 999999999, [identity])
        registry.write_lease(9001, os.getpid(), [identity])
        kill_mock = MagicMock(return_value=True)
        with patch("manifold.registry.kill_entry_processes", kill_mock):
            result = runner.invoke(app, ["down", "--port", "9000"])
        assert result.exit_code == 0, result.output
        kill_mock.assert_not_called()
        entry = registry.read_service_entry(identity)
        assert entry["owner_port"] == 9001
        assert entry["owner_pid"] == os.getpid()
        assert registry.read_lease(9000) is None
        assert registry.read_lease(9001) is not None
        assert not (tmp_path / "manifold-9000.pid").exists()


def test_down_all_stops_every_instance(tmp_path: Path):
    (tmp_path / "manifold-9000.pid").write_text("999999999")
    (tmp_path / "manifold-9001.pid").write_text("999999999")
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(
            _entry("id-a", name="a", owner_port=9000, owner_pid=999999999)
        )
        registry.write_service_entry(
            _entry("id-b", name="b", owner_port=9001, owner_pid=999999999)
        )
        registry.write_lease(9000, 999999999, ["id-a"])
        registry.write_lease(9001, 999999999, ["id-b"])
        kill_mock = MagicMock(return_value=True)
        with patch("manifold.registry.kill_entry_processes", kill_mock):
            result = runner.invoke(app, ["down", "--all"])
        assert result.exit_code == 0, result.output
        assert kill_mock.call_count == 2
        assert registry.read_service_entry("id-a") is None
        assert registry.read_service_entry("id-b") is None
        assert registry.read_lease(9000) is None
        assert registry.read_lease(9001) is None
        assert not (tmp_path / "manifold-9000.pid").exists()
        assert not (tmp_path / "manifold-9001.pid").exists()


def test_down_legacy_lsof_fallback(tmp_path: Path, config_file: Path):
    (tmp_path / "manifold-9000.pid").write_text("999999999")
    (tmp_path / "manifold-9000.port").write_text("127.0.0.1:9000")
    with patch("manifold.paths.PID_DIR", tmp_path):
        lsof_mock = MagicMock(return_value=[1234, 5678])
        with patch("manifold.cli._lsof_ports_for_config", lsof_mock):
            result = runner.invoke(
                app,
                ["down", "--port", "9000", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        lsof_mock.assert_called_once_with(str(config_file))
        assert "Killed 2 legacy process(es) via lsof port scan" in result.output
        assert not (tmp_path / "manifold-9000.pid").exists()


def test_down_legacy_no_lsof_when_registry_reaped(tmp_path: Path):
    """A registry-tracked reap must never reach the lsof fallback."""
    identity = "id-a"
    (tmp_path / "manifold-9000.pid").write_text("999999999")
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(
            _entry(identity, owner_port=9000, owner_pid=999999999)
        )
        registry.write_lease(9000, 999999999, [identity])
        lsof_mock = MagicMock(return_value=[])
        with (
            patch("manifold.registry.kill_entry_processes", return_value=True),
            patch("manifold.cli._lsof_ports_for_config", lsof_mock),
        ):
            result = runner.invoke(app, ["down", "--port", "9000"])
        assert result.exit_code == 0, result.output
        lsof_mock.assert_not_called()


def test_down_one_hung_gateway_with_lease_cleanup(tmp_path: Path):
    """_down_one directly: dead gateway pid, lease + entry owned by it."""
    identity = "id-a"
    (tmp_path / "manifold-9000.pid").write_text("999999999")
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(
            _entry(identity, owner_port=9000, owner_pid=999999999)
        )
        registry.write_lease(9000, 999999999, [identity])
        with (
            patch("manifold.registry.kill_entry_processes", return_value=True),
            patch("manifold.cli._lsof_ports_for_config", return_value=[]),
        ):
            _down_one(9000, None)
        assert registry.read_service_entry(identity) is None
        assert registry.read_lease(9000) is None
        assert not (tmp_path / "manifold-9000.pid").exists()


# --- _run_pipeline direct ---------------------------------------------------


@pytest.mark.asyncio
async def test_run_pipeline_adopt_skips_spawn_and_writes_lease(tmp_path: Path):
    config_file = _config_file(tmp_path)
    cfg = load_config(config_file)
    identity = registry.compute_service_identity(cfg.pipeline[0], FALLBACK)
    lease_writes = []
    real_write_lease = registry.write_lease

    def _record_lease(port, pid, identities, isolated=False):
        lease_writes.append((port, pid, list(identities), isolated))
        real_write_lease(port, pid, identities, isolated)

    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(_entry(identity, owner_port=9000))
        with patch("manifold.paths.is_port_in_use", return_value=False):
            start_mock = AsyncMock()
            with (
                patch("manifold.process.start_service", start_mock),
                patch("manifold.registry.write_lease", side_effect=_record_lease),
            ):
                with ExitStack() as stack:
                    _patch_up_runtime(stack)
                    await _run_pipeline(str(config_file), verbose=False)
            start_mock.assert_not_awaited()
            assert lease_writes == [(9000, os.getpid(), [identity], False)] * 2


def test_down_kills_entry_owned_by_dead_other_gateway(tmp_path: Path):
    """I7: the last live lease holder reaps a leased service even when the
    recorded owner was a DIFFERENT gateway that is now dead."""
    identity = "id-a"
    (tmp_path / "manifold-9000.pid").write_text("999999999")
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(
            _entry(identity, owner_port=9001, owner_pid=999999999)
        )
        registry.write_lease(9000, 999999999, [identity])
        kill_mock = MagicMock(return_value=True)
        with patch("manifold.registry.kill_entry_processes", kill_mock):
            result = runner.invoke(app, ["down", "--port", "9000"])
        assert result.exit_code == 0, result.output
        kill_mock.assert_called_once()
        assert registry.read_service_entry(identity) is None
        assert registry.read_lease(9000) is None


def test_down_without_pid_file_still_reaps_via_registry(tmp_path: Path):
    """A dead gateway may have lost its pid file while registry entries
    remain — `down --port` must not require the pid file (review fix)."""
    identity = "id-a"
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(
            _entry(identity, owner_port=9000, owner_pid=999999999)
        )
        registry.write_lease(9000, 999999999, [identity])
        kill_mock = MagicMock(return_value=True)
        with patch("manifold.registry.kill_entry_processes", kill_mock):
            result = runner.invoke(app, ["down", "--port", "9000"])
        assert result.exit_code == 0, result.output
        kill_mock.assert_called_once()
        assert registry.read_service_entry(identity) is None
        assert registry.read_lease(9000) is None


async def test_spawn_owned_promote_aborts_when_reclaim_fails(tmp_path: Path):
    """I4: when the reclaim kill could not free the port (pid/pgid reuse or
    SIGKILL survivor), _spawn_owned must abort instead of spawning on an
    occupied port."""
    identity = "id-a"
    state = ServiceState(config=_svc())
    entry = _entry(identity, pid=os.getpid())
    with patch("manifold.paths.PID_DIR", tmp_path):
        with (
            patch("manifold.registry.kill_entry_processes", return_value=False),
            patch("manifold.paths.is_port_in_use", return_value=True),
        ):
            with pytest.raises(typer.Exit):
                await service_ops._spawn_owned(
                    state, FALLBACK, identity, 9000, os.getpid(), reclaim_entry=entry
                )
