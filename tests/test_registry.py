"""Tests for manifold.registry."""

import os
import signal
from unittest.mock import patch

import pytest

from manifold import paths, registry
from manifold.models import ServiceConfig


@pytest.fixture
def tmp_run(tmp_path, monkeypatch):
    """Point paths.PID_DIR at a temp dir so registry writes stay local."""
    monkeypatch.setattr(paths, "PID_DIR", tmp_path)
    return tmp_path


def _svc(**overrides) -> ServiceConfig:
    fields = dict(
        name="redactor",
        directory="/tmp/redactor",
        command="echo {port}",
        port=7789,
        health="/healthz",
    )
    fields.update(overrides)
    return ServiceConfig(**fields)


def _write_entry(identity, pid, owner_pid, port=7789):
    registry.write_service_entry(
        {
            "schema_version": 1,
            "identity": identity,
            "name": identity,
            "directory": "/tmp/svc",
            "command": "echo 7789",
            "port": port,
            "upstream": "http://127.0.0.1:7788",
            "pid": pid,
            "pgid": pid,
            "owner_port": 9000,
            "owner_pid": owner_pid,
            "started_at": 0.0,
        }
    )


# --- paths ----------------------------------------------------------------


def test_paths_derive_from_pid_dir(tmp_run):
    assert registry.services_dir().parent.parent == tmp_run
    assert registry.service_entry_path("abc").name == "abc.json"
    assert registry.lease_path(9000).name == "gateway-9000.json"
    assert registry.lock_path("abc").name == "abc.lock"


# --- identity -------------------------------------------------------------


def test_identity_stable(tmp_run):
    upstream = "http://127.0.0.1:7788/v1"
    first = registry.compute_service_identity(_svc(), upstream)
    second = registry.compute_service_identity(_svc(), upstream)
    assert first == second
    assert len(first) == 64  # sha256 hex


def test_identity_changes_per_field(tmp_run):
    upstream = "http://127.0.0.1:7788"
    base = registry.compute_service_identity(_svc(), upstream)
    variants = [
        _svc(name="other"),
        _svc(directory="/tmp/other"),
        _svc(port=7800),
        _svc(command="echo other"),
    ]
    for variant in variants:
        assert registry.compute_service_identity(variant, upstream) != base
    assert registry.compute_service_identity(_svc(), "http://127.0.0.1:7777") != base


# --- service entries ------------------------------------------------------


def test_service_entry_roundtrip(tmp_run):
    entry = {
        "schema_version": 1,
        "identity": "abc123",
        "name": "svc",
        "directory": "/tmp/svc",
        "command": "echo 7789",
        "port": 7789,
        "upstream": "http://127.0.0.1:7788",
        "pid": 1234,
        "pgid": 1234,
        "owner_port": 9000,
        "owner_pid": 999,
        "started_at": 1000.0,
    }
    registry.write_service_entry(entry)
    assert registry.read_service_entry("abc123") == entry
    entries = registry.list_service_entries()
    assert len(entries) == 1
    assert entries[0] == entry


def test_read_service_entry_corrupt_unlinks(tmp_run):
    path = registry.service_entry_path("bad")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert registry.read_service_entry("bad") is None
    assert not path.exists()
    # non-dict JSON is also treated as corrupt
    path.write_text("[1, 2, 3]")
    assert registry.read_service_entry("bad") is None
    assert not path.exists()


def test_remove_service_entry_idempotent(tmp_run):
    _write_entry("x", pid=1, owner_pid=2)
    registry.remove_service_entry("x")
    registry.remove_service_entry("x")  # no error on second call
    assert registry.read_service_entry("x") is None


# --- leases ---------------------------------------------------------------


def test_lease_roundtrip(tmp_run):
    registry.write_lease(9000, 1234, ["a", "b"])
    lease = registry.read_lease(9000)
    assert lease["gateway_port"] == 9000
    assert lease["gateway_pid"] == 1234
    assert lease["identities"] == ["a", "b"]
    assert lease["isolated"] is False
    assert lease["schema_version"] == registry.SCHEMA_VERSION
    assert "updated_at" in lease

    registry.write_lease(9001, 55, [], isolated=True)
    leases = registry.list_leases()
    assert {lease["gateway_port"] for lease in leases} == {9000, 9001}
    assert registry.read_lease(9001)["isolated"] is True


def test_remove_lease_idempotent(tmp_run):
    registry.write_lease(9000, 1, [])
    registry.remove_lease(9000)
    registry.remove_lease(9000)  # no error on second call
    assert registry.read_lease(9000) is None


def test_live_other_leases(tmp_run, monkeypatch):
    registry.write_lease(9000, gateway_pid=100, identities=[])
    registry.write_lease(9001, gateway_pid=200, identities=[])
    registry.write_lease(9002, gateway_pid=300, identities=[])
    monkeypatch.setattr(registry, "pid_alive", lambda pid: pid in (100, 300))
    others = registry.live_other_leases(9002)
    assert [lease["gateway_port"] for lease in others] == [9000]


# --- pid liveness ---------------------------------------------------------


def test_pid_alive(tmp_run):
    assert registry.pid_alive(None) is False
    with patch("os.kill") as mock_kill:
        assert registry.pid_alive(100) is True
        mock_kill.assert_called_with(100, 0)
    with patch("os.kill", side_effect=ProcessLookupError):
        assert registry.pid_alive(100) is False
    with patch("os.kill", side_effect=PermissionError):
        assert registry.pid_alive(100) is True


def test_entry_is_live_requires_both_pids(tmp_run, monkeypatch):
    entry = {"pid": 100, "owner_pid": 200}
    monkeypatch.setattr(registry, "pid_alive", lambda pid: pid in (100, 200))
    assert registry.entry_is_live(entry) is True
    monkeypatch.setattr(registry, "pid_alive", lambda pid: pid == 100)
    assert registry.entry_is_live(entry) is False  # owner dead
    monkeypatch.setattr(registry, "pid_alive", lambda pid: False)
    assert registry.entry_is_live(entry) is False  # service dead


def test_find_live_entry_by_port(tmp_run, monkeypatch):
    _write_entry("a", pid=100, owner_pid=200, port=7789)
    _write_entry("b", pid=999, owner_pid=998, port=7790)
    monkeypatch.setattr(registry, "pid_alive", lambda pid: pid in (100, 200))
    found = registry.find_live_entry_by_port(7789)
    assert found is not None
    assert found["identity"] == "a"
    assert registry.find_live_entry_by_port(7790) is None  # owner dead
    assert registry.find_live_entry_by_port(12345) is None


# --- kill_entry_processes -------------------------------------------------


def test_kill_entry_processes_sigterm_then_sigkill(tmp_run, monkeypatch):
    entry = {"pid": 123, "pgid": 456}
    monkeypatch.setattr(registry, "pid_alive", lambda pid: True)
    monkeypatch.setattr(registry.os, "getpgid", lambda pid: 456)
    kills = []
    monkeypatch.setattr(
        registry.os, "killpg", lambda pgid, sig: kills.append((pgid, sig))
    )
    # deadline comes from the first monotonic call; the loop check returns a
    # time past the deadline so the poll loop exits without sleeping.
    clock = iter([0.0, 2.5])
    monkeypatch.setattr(registry.time, "monotonic", lambda: next(clock))
    assert registry.kill_entry_processes(entry) is True
    assert kills == [(456, signal.SIGTERM), (456, signal.SIGKILL)]


def test_kill_entry_processes_sigterm_suffices(tmp_run, monkeypatch):
    entry = {"pid": 123, "pgid": 456}
    alive = iter([True, False, False])
    monkeypatch.setattr(registry, "pid_alive", lambda pid: next(alive))
    monkeypatch.setattr(registry.os, "getpgid", lambda pid: 456)
    kills = []
    monkeypatch.setattr(
        registry.os, "killpg", lambda pgid, sig: kills.append((pgid, sig))
    )
    assert registry.kill_entry_processes(entry) is True
    assert kills == [(456, signal.SIGTERM)]  # died within grace: no SIGKILL


def test_kill_entry_processes_pgid_mismatch(tmp_run, monkeypatch):
    entry = {"pid": 123, "pgid": 456}
    monkeypatch.setattr(registry, "pid_alive", lambda pid: True)
    monkeypatch.setattr(registry.os, "getpgid", lambda pid: 999)
    kills = []
    monkeypatch.setattr(
        registry.os, "killpg", lambda pgid, sig: kills.append((pgid, sig))
    )
    assert registry.kill_entry_processes(entry) is False  # I4: pid/pgid reuse
    assert kills == []


def test_kill_entry_processes_pid_dead(tmp_run, monkeypatch):
    entry = {"pid": 123, "pgid": 456}
    monkeypatch.setattr(registry, "pid_alive", lambda pid: False)
    kills = []
    monkeypatch.setattr(
        registry.os, "killpg", lambda pgid, sig: kills.append((pgid, sig))
    )
    assert registry.kill_entry_processes(entry) is False
    assert kills == []
    assert registry.kill_entry_processes({}) is False  # missing pid


# --- spawn locks ----------------------------------------------------------


def test_spawn_lock_exclusive_and_release(tmp_run):
    assert registry.acquire_spawn_lock("svc") is True
    assert registry.acquire_spawn_lock("svc") is False  # already held
    lock = registry.lock_path("svc")
    assert lock.read_text().strip() == str(os.getpid())
    registry.release_spawn_lock("svc")
    assert registry.acquire_spawn_lock("svc") is True
    registry.release_spawn_lock("svc")
    registry.release_spawn_lock("svc")  # idempotent


# --- sweep_stale ----------------------------------------------------------


def test_sweep_removes_dead_entry(tmp_run, monkeypatch):
    _write_entry("dead-svc", pid=1, owner_pid=2)
    monkeypatch.setattr(registry, "pid_alive", lambda pid: False)
    registry.sweep_stale()
    assert registry.read_service_entry("dead-svc") is None


def test_sweep_kills_orphan_with_dead_owner(tmp_run, monkeypatch):
    _write_entry("orphan", pid=100, owner_pid=999)
    killed = []
    monkeypatch.setattr(
        registry,
        "kill_entry_processes",
        lambda entry, grace=2.0: killed.append(entry["identity"]) or True,
    )
    monkeypatch.setattr(registry, "pid_alive", lambda pid: pid == 100)
    registry.sweep_stale()
    assert killed == ["orphan"]
    assert registry.read_service_entry("orphan") is None


def test_sweep_removes_orphan_even_if_kill_fails(tmp_run, monkeypatch):
    _write_entry("orphan", pid=100, owner_pid=999)
    monkeypatch.setattr(
        registry, "kill_entry_processes", lambda entry, grace=2.0: False
    )
    monkeypatch.setattr(registry, "pid_alive", lambda pid: pid == 100)
    registry.sweep_stale()
    assert registry.read_service_entry("orphan") is None  # I2: remove regardless


def test_sweep_leaves_leased_service(tmp_run, monkeypatch):
    _write_entry("leased", pid=100, owner_pid=200)
    registry.write_lease(9000, gateway_pid=300, identities=["leased"])
    killed = []
    monkeypatch.setattr(
        registry,
        "kill_entry_processes",
        lambda entry, grace=2.0: killed.append(entry["identity"]) or True,
    )
    monkeypatch.setattr(registry, "pid_alive", lambda pid: pid in (100, 200, 300))
    registry.sweep_stale()
    assert killed == []
    assert registry.read_service_entry("leased") is not None


def test_sweep_does_not_kill_identity_listed_in_live_lease(tmp_run, monkeypatch):
    # I3: a dead owner does not authorize killing a service a live lease lists.
    _write_entry("shared", pid=100, owner_pid=999)
    registry.write_lease(9000, gateway_pid=300, identities=["shared"])
    killed = []
    monkeypatch.setattr(
        registry,
        "kill_entry_processes",
        lambda entry, grace=2.0: killed.append(entry["identity"]) or True,
    )
    monkeypatch.setattr(registry, "pid_alive", lambda pid: pid in (100, 300))
    registry.sweep_stale()
    assert killed == []
    assert registry.read_service_entry("shared") is not None


def test_sweep_removes_dead_lease(tmp_run, monkeypatch):
    registry.write_lease(9000, gateway_pid=999, identities=[])
    registry.write_lease(9001, gateway_pid=300, identities=[])
    monkeypatch.setattr(registry, "pid_alive", lambda pid: pid == 300)
    registry.sweep_stale()
    assert registry.read_lease(9000) is None
    assert registry.read_lease(9001) is not None
