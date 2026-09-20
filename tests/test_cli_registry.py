"""Tests for the shared-service registry wiring in the CLI (workstream C).

Covers: port overrides (shared vs isolated), shared preflight, the
plan/adopt/promote/spawn decision loop, full ``manifold up`` runs against a
faked uvicorn, registry teardown, and the rewritten ``down``.
"""

import asyncio
import os
import signal
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from manifold import cli as cli_module
from manifold import registry, service_ops
from manifold.chain import get_entry_url
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
    ServiceStatus,
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


def test_preflight_shared_foreign_shim_names_owner_gateway(tmp_path: Path, caplog):
    """A port held by another gateway's crash shim stays a hard error (we
    cannot stop another process's listener), but the message must name the
    shimming gateway — the old 'non-manifold process' framing sent owners
    chasing ghosts."""
    import logging

    svc = _svc()
    cfg = _cfg(services=[svc])
    with patch("manifold.paths.PID_DIR", tmp_path):
        with (
            patch(
                "manifold.paths.is_port_in_use",
                side_effect=lambda port, host="127.0.0.1": port == 7001,
            ),
            patch.object(service_ops, "find_live_shim_owner", return_value=9100),
        ):
            with caplog.at_level(logging.ERROR, logger="manifold"):
                with pytest.raises(typer.Exit) as excinfo:
                    _preflight_check(cfg, shared=True)
    assert excinfo.value.exit_code == 1
    assert any(
        "crash shim from gateway :9100" in r.getMessage() for r in caplog.records
    )


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


def test_plan_service_own_shim_port_is_reclaimable(tmp_path: Path):
    """A port held by THIS gateway's crash shim is not a conflict: the spawn
    path's choke point (process.start_service) stops the shim before the
    child binds, so planning proceeds to a spawn."""
    svc = _svc()
    with patch("manifold.paths.PID_DIR", tmp_path):
        with (
            patch("manifold.paths.is_port_in_use", return_value=True),
            patch("manifold.shim.get_shim", return_value=object()),
        ):
            decision, payload = service_ops._plan_service(svc, FALLBACK)
    assert decision == "spawn"
    assert payload is None


def test_plan_service_error_names_live_shim_owner(tmp_path: Path):
    """A foreign gateway's shim cannot be reclaimed, but the error must not
    call it a 'non-manifold process' — it names the shimming gateway."""
    svc = _svc()
    with patch("manifold.paths.PID_DIR", tmp_path):
        with (
            patch("manifold.paths.is_port_in_use", return_value=True),
            patch.object(service_ops, "find_live_shim_owner", return_value=9100),
        ):
            decision, payload = service_ops._plan_service(svc, FALLBACK)
    assert decision == "error"
    assert "crash shim from gateway :9100" in str(payload)


def test_find_live_shim_owner_reports_shimming_gateway(tmp_path: Path):
    """Walks the live leases' /_manifold/config endpoints and returns the
    gateway whose pipeline reports shim=true on the port."""

    seen: dict[str, str] = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"pipeline": [{"name": "svc-a", "port": 7001, "shim": True}]}

    class _FakeClient:
        def __init__(self, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            seen["url"] = url
            return _FakeResponse()

    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_lease(9100, os.getpid(), [], isolated=False)
        with patch.object(service_ops.httpx, "Client", _FakeClient):
            assert service_ops.find_live_shim_owner(7001) == 9100
            assert seen["url"] == "http://127.0.0.1:9100/_manifold/config"
            # a port the gateway is not shimming finds no owner
            assert service_ops.find_live_shim_owner(7002) is None


def test_find_live_shim_owner_ignores_dead_leases(tmp_path: Path):
    """A lease whose gateway pid is dead is not asked (its shims die with it
    anyway — they are asyncio listeners in that process)."""

    class _Boom:
        def __init__(self, **_kw):
            raise AssertionError("no HTTP client may be built for a dead lease")

    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_lease(9100, 999999999, [], isolated=False)
        with patch.object(service_ops.httpx, "Client", _Boom):
            assert service_ops.find_live_shim_owner(7001) is None


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


@pytest.mark.asyncio
async def test_spawn_owned_reclaims_own_shim_port(tmp_path: Path):
    """Promote-after-shim: the corpse's port is held by OUR crash shim (the
    adopter shimmed it before promoting).  That occupant is reclaimable — the
    spawn stops the shim — not a 'still occupied' failure."""
    identity = "id-a"
    entry = _entry(identity, owner_port=9001, owner_pid=999999999)
    state = ServiceState(config=_svc())
    with patch("manifold.paths.PID_DIR", tmp_path):
        registry.write_service_entry(entry)
        with (
            patch("manifold.paths.is_port_in_use", return_value=True),
            patch("manifold.shim.get_shim", return_value=object()),
        ):
            start_mock = AsyncMock()
            with patch("manifold.process.start_service", start_mock):
                result = await service_ops._spawn_owned(
                    state, FALLBACK, identity, 9000, 111, reclaim_entry=entry
                )
        assert result is True
        start_mock.assert_awaited_once()
        assert registry.read_service_entry(identity)["owner_port"] == 9000


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


# --- mid-spawn failure teardown (M3) -----------------------------------------


def _config_file_two_services(tmp_path: Path) -> Path:
    p = tmp_path / "manifold.yaml"
    p.write_text(
        """\
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
  - name: svc-b
    directory: /tmp
    command: "echo b --port {port} --upstream {upstream}"
    port: 7002
    health: /h
    upstream_via: cli_arg
    enabled: true
"""
    )
    return p


def test_up_mid_spawn_failure_tears_down_half_chain(tmp_path: Path):
    """M3: the spawn loop used to sit OUTSIDE the runtime try/finally — a
    typer.Exit on the SECOND service's spawn left the first running with a
    lease written and nobody cleaning up.  Now the same _shutdown_pipeline
    path must run: stop called, entries + lease removed."""
    config_file = _config_file_two_services(tmp_path)
    stop_mock = AsyncMock()
    real_spawn = service_ops._spawn_owned
    spawn_calls = {"n": 0}

    async def _fail_second(
        state, upstream_url, identity, gw_port, gw_pid, reclaim_entry=None
    ):
        spawn_calls["n"] += 1
        if spawn_calls["n"] == 2:
            raise typer.Exit(1)
        return await real_spawn(
            state,
            upstream_url,
            identity,
            gw_port,
            gw_pid,
            reclaim_entry=reclaim_entry,
        )

    with patch("manifold.paths.PID_DIR", tmp_path):
        with patch("manifold.paths.is_port_in_use", return_value=False):
            with ExitStack() as stack:
                _patch_up_runtime(stack)
                stack.enter_context(patch("manifold.process.stop_service", stop_mock))
                stack.enter_context(
                    patch(
                        "manifold.service_ops._spawn_owned",
                        side_effect=_fail_second,
                    )
                )
                result = runner.invoke(app, ["up", "--config", str(config_file)])
        # Registry assertions stay INSIDE the PID_DIR patch: outside it they
        # would read the real live state in ~/.manifold/run.
        assert result.exit_code == 1
        assert spawn_calls["n"] == 2  # failed on the SECOND service
        stop_mock.assert_awaited_once()
        assert stop_mock.await_args.args[0].config.name == "svc-a"  # the FIRST one
        assert registry.read_lease(9000) is None
        assert registry.list_service_entries() == []


# --- crash-path rewiring (contained M1 items) --------------------------------


def _patch_up_runtime_with(
    stack: ExitStack,
    captured: dict,
    start_mock: AsyncMock,
    server_cls: type,
    capture_crash: bool = False,
) -> None:
    """Like _patch_up_runtime but with a custom Server class and app capture."""
    stack.enter_context(
        patch("manifold.cli.wait_for_services_ready", new_callable=AsyncMock)
    )
    stack.enter_context(patch("manifold.cli.health_loop", new_callable=AsyncMock))
    stack.enter_context(patch("manifold.cli.watch_config", new_callable=AsyncMock))
    stack.enter_context(patch("manifold.cli.uvicorn.Server", new=server_cls))
    stack.enter_context(patch("manifold.process.start_service", start_mock))
    real_create_app = cli_module.create_app

    def _capture_create_app(**kwargs):
        captured["pipeline"] = kwargs["pipeline"]
        return real_create_app(**kwargs)

    stack.enter_context(
        patch("manifold.cli.create_app", side_effect=_capture_create_app)
    )
    if capture_crash:
        stack.enter_context(
            patch(
                "manifold.process.set_on_crash",
                side_effect=lambda cb: captured.update(crash=cb),
            )
        )


def test_auto_restart_rewires_around_dead_dependency(tmp_path: Path, monkeypatch):
    """c1: the crashed service's restart must compute its upstream from the
    currently-LIVE services — restarting into a dependency that is itself
    still dead used to guarantee an instant second failure."""
    monkeypatch.setattr(cli_module, "_BASE_RESTART_DELAY", 0.01)

    p = tmp_path / "manifold.yaml"
    p.write_text(
        """\
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
  - name: svc-b
    directory: /tmp
    command: "echo b --port {port} --upstream {upstream}"
    port: 7002
    health: /h
    upstream_via: cli_arg
    enabled: true
  - name: svc-c
    directory: /tmp
    command: "echo c --port {port} --upstream {upstream}"
    port: 7003
    health: /h
    upstream_via: cli_arg
    enabled: true
"""
    )
    captured: dict = {}
    start_mock = AsyncMock()

    class _CrashyServer(_FakeUvicornServer):
        async def main_loop(self):
            pipeline = captured["pipeline"]
            # start_service is mocked, so statuses never left STOPPED; set the
            # realistic mid-flight world: svc-c healthy, svc-b dead earlier,
            # and now svc-a crashes too.
            pipeline.get_service("svc-c").status = ServiceStatus.HEALTHY
            pipeline.get_service("svc-b").status = ServiceStatus.UNHEALTHY
            state_a = pipeline.get_service("svc-a")
            state_a.status = ServiceStatus.UNHEALTHY
            captured["crash"](state_a)
            for _ in range(200):
                if start_mock.await_count >= 4:  # 3 initial spawns + 1 restart
                    break
                await asyncio.sleep(0.01)

    with patch("manifold.paths.PID_DIR", tmp_path):
        with patch("manifold.paths.is_port_in_use", return_value=False):
            with ExitStack() as stack:
                _patch_up_runtime_with(
                    stack, captured, start_mock, _CrashyServer, capture_crash=True
                )
                result = runner.invoke(app, ["up", "--config", str(p)])
    assert result.exit_code == 0, result.output
    assert start_mock.await_count == 4
    restarted_state, upstream_url = start_mock.await_args_list[3].args
    assert restarted_state.config.name == "svc-a"
    # upstream must be svc-C (live), NOT svc-B (the dead dependency)
    assert upstream_url == "http://127.0.0.1:7003"


def test_adopted_crash_rewires_but_does_not_stop(tmp_path: Path):
    """c2: an adopted service's crash performs the bookkeeping rewire so OUR
    chain routes around it, while kill/restart stay with the owner gateway
    (I1) — before the fix the handler returned before rewiring."""
    config_file = _config_file_two_services(tmp_path)
    cfg = load_config(config_file)
    identity_a = registry.compute_service_identity(
        cfg.pipeline[0], "http://127.0.0.1:7002"
    )
    captured: dict = {}
    start_mock = AsyncMock()
    stop_mock = AsyncMock()

    class _CrashyServer(_FakeUvicornServer):
        async def main_loop(self):
            pipeline = captured["pipeline"]
            state_a = pipeline.get_service("svc-a")
            assert state_a.adopted is True
            # start_service is mocked → b never left STOPPED; a healthy
            # svc-b is what the entry hop should fall through to.
            pipeline.get_service("svc-b").status = ServiceStatus.HEALTHY
            state_a.status = ServiceStatus.UNHEALTHY
            captured["crash"](state_a)
            await asyncio.sleep(0.05)

    with patch("manifold.paths.PID_DIR", tmp_path):
        # svc-a runs, owned by ANOTHER gateway (:9001) → this `up` adopts it.
        # (Inside the PID_DIR patch — never write to the real ~/.manifold/run.)
        registry.write_service_entry(
            {
                "schema_version": 1,
                "identity": identity_a,
                "name": "svc-a",
                "directory": "/tmp",
                "command": "echo a --port 7001 --upstream http://127.0.0.1:7002",
                "port": 7001,
                "upstream": "http://127.0.0.1:7002",
                "pid": os.getpid(),
                "pgid": os.getpid(),
                "owner_port": 9001,
                "owner_pid": os.getpid(),
                "started_at": 0.0,
            }
        )
        with patch("manifold.paths.is_port_in_use", return_value=False):
            with ExitStack() as stack:
                _patch_up_runtime_with(
                    stack, captured, start_mock, _CrashyServer, capture_crash=True
                )
                stack.enter_context(patch("manifold.process.stop_service", stop_mock))
                result = runner.invoke(app, ["up", "--config", str(config_file)])
        # Registry assertions stay INSIDE the PID_DIR patch (live-state leak).
        assert result.exit_code == 0, result.output
        # bypass state updated: entry hop now skips the dead adopted service
        pipeline = captured["pipeline"]
        assert pipeline.get_service("svc-a").status == ServiceStatus.UNHEALTHY
        assert get_entry_url(pipeline, GatewayConfig()) == "http://127.0.0.1:7002"
        # no kill of the adopted service — teardown only ever stopped svc-b
        stopped_names = [c.args[0].config.name for c in stop_mock.await_args_list]
        assert "svc-a" not in stopped_names
        # owner's registry entry untouched
        entry = registry.read_service_entry(identity_a)
        assert entry is not None
        assert entry["owner_port"] == 9001


# --- gateway kill identity check (M6) -----------------------------------------


def test_down_live_foreign_pid_not_signaled(tmp_path: Path):
    """M6: a stale pid file pointing at a LIVE but non-manifold process must
    not be signaled (pid reuse) — files are still cleaned up."""
    pid_file = tmp_path / "manifold-9000.pid"
    pid_file.write_text(str(os.getpid()))
    (tmp_path / "manifold-9000.port").write_text("127.0.0.1:9000")
    ps_mock = MagicMock(returncode=0, stdout="vim notes.txt")
    with patch("manifold.paths.PID_DIR", tmp_path):
        with patch("manifold.registry.pid_alive", lambda pid: pid == os.getpid()):
            with patch("manifold.cli.subprocess.run", return_value=ps_mock) as run_mock:
                kill_mock = MagicMock()
                with patch("manifold.cli.os.kill", kill_mock):
                    with patch(
                        "manifold.cli._lsof_ports_for_config",
                        MagicMock(return_value=[]),
                    ):
                        _down_one(9000, None)
    run_mock.assert_called_once()  # the identity check consulted ps
    kill_mock.assert_not_called()  # never signal a foreign process
    assert not pid_file.exists()  # files still cleaned up
    assert not (tmp_path / "manifold-9000.port").exists()


def test_down_live_own_pid_signaled(tmp_path: Path):
    """M6: a live gateway whose ps command mentions manifold IS signaled."""
    pid_file = tmp_path / "manifold-9000.pid"
    pid_file.write_text(str(os.getpid()))
    (tmp_path / "manifold-9000.port").write_text("127.0.0.1:9000")
    ps_mock = MagicMock(
        returncode=0,
        stdout=f"python .venv/bin/manifold up --port 9000  # {os.getpid()}",
    )
    # pid_alive: gate check -> alive, then the post-SIGTERM poll sees it gone.
    alive = iter([True, False, False])
    with patch("manifold.paths.PID_DIR", tmp_path):
        with patch("manifold.registry.pid_alive", lambda pid: next(alive)):
            with patch("manifold.cli.subprocess.run", return_value=ps_mock):
                kill_mock = MagicMock()
                with patch("manifold.cli.os.kill", kill_mock):
                    with patch(
                        "manifold.cli._lsof_ports_for_config",
                        MagicMock(return_value=[]),
                    ):
                        _down_one(9000, None)
    kill_mock.assert_called_once_with(os.getpid(), signal.SIGTERM)


def test_down_pid_reuse_skip_logged(tmp_path: Path, caplog):
    """The skip must be visible: a warning naming the pid-reuse suspicion."""
    import logging

    (tmp_path / "manifold-9000.pid").write_text(str(os.getpid()))
    ps_mock = MagicMock(returncode=0, stdout="nginx: master process")
    with caplog.at_level(logging.WARNING, logger="manifold"):
        with patch("manifold.paths.PID_DIR", tmp_path):
            with patch("manifold.registry.pid_alive", lambda pid: pid == os.getpid()):
                with patch("manifold.cli.subprocess.run", return_value=ps_mock):
                    with patch(
                        "manifold.cli._lsof_ports_for_config",
                        MagicMock(return_value=[]),
                    ):
                        _down_one(9000, None)
    assert any(
        "does not look like a manifold gateway" in r.getMessage()
        for r in caplog.records
    )
