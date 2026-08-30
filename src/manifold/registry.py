"""Filesystem registry of running services, leases, and spawn locks.

The registry is the source of truth for which services are running and which
gateway owns them. State lives under ``paths.PID_DIR / "run"`` as JSON files;
all paths are derived lazily at call time so tests can patch ``paths.PID_DIR``.

Invariants (details in SPEC-shared-pipeline.md):
- I1 single writer: only the entry's owner gateway may spawn/kill/restart/patch.
- I2 entry life: entries written atomically after spawn; removed by owner-stop,
  a reaper with a verified-dead owner gateway, or a sweep with a dead service pid.
- I3 kill authority: owner fields authorize kills; a lease list alone never
  authorizes killing a service another live lease still lists.
- I4 verified kill: killpg only when ``os.getpgid(pid) == entry["pgid"]``.
- I5 platform: POSIX killpg; win32 falls back to ``os.kill(pid, SIGTERM)``.
- I6 sharing boundary: adoption only on exact identity match.
- I7 stop on last leave: services stop when their last leasing gateway leaves.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from manifold import chain, paths
from manifold.models import ServiceConfig

SCHEMA_VERSION = 1

_log = logging.getLogger(__name__)


def run_dir() -> Path:
    """Root directory for all registry state (``~/.manifold/run``)."""
    return paths.PID_DIR / "run"


def services_dir() -> Path:
    """Directory holding one JSON entry per running service."""
    return run_dir() / "services"


def leases_dir() -> Path:
    """Directory holding one JSON lease per running gateway."""
    return run_dir() / "leases"


def locks_dir() -> Path:
    """Directory holding O_EXCL spawn locks (one per identity)."""
    return run_dir() / "locks"


def service_entry_path(identity: str) -> Path:
    """Path of the service entry file for *identity*."""
    return services_dir() / f"{identity}.json"


def lease_path(gateway_port: int) -> Path:
    """Path of the lease file for gateway *gateway_port*."""
    return leases_dir() / f"gateway-{gateway_port}.json"


def lock_path(identity: str) -> Path:
    """Path of the spawn lock file for *identity*."""
    return locks_dir() / f"{identity}.lock"


def pid_alive(pid: int | None) -> bool:
    """True if *pid* exists (``os.kill(pid, 0)``); None/not-found -> False."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def entry_is_live(entry: dict) -> bool:
    """True if the entry's service process (and, on POSIX, its owner) is alive."""
    if not pid_alive(entry.get("pid")):
        return False
    if sys.platform == "win32":
        return True
    return pid_alive(entry.get("owner_pid"))


def read_service_entry(identity: str) -> dict | None:
    """Load the entry for *identity*; corrupt files are unlinked and yield None."""
    path = service_entry_path(identity)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        _log.warning("Removing corrupt service entry %s", path)
        path.unlink(missing_ok=True)
        return None
    if not isinstance(data, dict):
        _log.warning("Removing non-dict service entry %s", path)
        path.unlink(missing_ok=True)
        return None
    data["identity"] = identity  # filename is canonical
    return data


def write_service_entry(entry: dict) -> None:
    """Atomically persist a service entry under ``services/<identity>.json``."""
    paths.atomic_write_text(
        service_entry_path(entry["identity"]),
        json.dumps(entry, indent=2),
    )


def remove_service_entry(identity: str) -> None:
    """Delete the service entry for *identity* (no-op if absent)."""
    service_entry_path(identity).unlink(missing_ok=True)


def list_service_entries() -> list[dict]:
    """Return all service entries, each with its identity from the filename."""
    entries = []
    for path in services_dir().glob("*.json"):
        entry = read_service_entry(path.stem)
        if entry is not None:
            entries.append(entry)
    return entries


def read_lease(gateway_port: int) -> dict | None:
    """Load the lease for gateway *gateway_port*, or None if absent/corrupt."""
    try:
        data = json.loads(lease_path(gateway_port).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data["gateway_port"] = gateway_port  # filename is canonical
    return data


def write_lease(
    gateway_port: int,
    gateway_pid: int,
    identities: list[str],
    isolated: bool = False,
) -> None:
    """Atomically persist a gateway lease under ``leases/gateway-<port>.json``."""
    lease = {
        "schema_version": SCHEMA_VERSION,
        "gateway_port": gateway_port,
        "gateway_pid": gateway_pid,
        "isolated": isolated,
        "identities": list(identities),
        "updated_at": time.time(),
    }
    paths.atomic_write_text(lease_path(gateway_port), json.dumps(lease, indent=2))


def remove_lease(gateway_port: int) -> None:
    """Delete the lease for gateway *gateway_port* (no-op if absent)."""
    lease_path(gateway_port).unlink(missing_ok=True)


def list_leases() -> list[dict]:
    """Return all gateway leases, each with its port from the filename."""
    leases = []
    for path in leases_dir().glob("gateway-*.json"):
        try:
            port = int(path.stem.removeprefix("gateway-"))
        except ValueError:
            continue
        lease = read_lease(port)
        if lease is not None:
            leases.append(lease)
    return leases


def live_other_leases(gateway_port: int) -> list[dict]:
    """Leases of *other* gateways whose gateway process is still alive (I7)."""
    return [
        lease
        for lease in list_leases()
        if lease.get("gateway_port") != gateway_port
        and pid_alive(lease.get("gateway_pid"))
    ]


def find_live_entry_by_port(port: int) -> dict | None:
    """First live service entry bound to *port*, else None (used by preflight)."""
    for entry in list_service_entries():
        if entry.get("port") == port and entry_is_live(entry):
            return entry
    return None


def kill_entry_processes(entry: dict, grace: float = 2.0) -> bool:
    """Kill the entry's processes per I4/I5; True if a kill was attempted."""
    pid = entry.get("pid")
    if pid is None or not pid_alive(pid):
        return False
    if sys.platform == "win32":
        os.kill(pid, signal.SIGTERM)
        return True
    pgid = entry.get("pgid")
    if pgid is None:
        return False
    try:
        if os.getpgid(pid) != pgid:
            return False
    except ProcessLookupError:
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.monotonic() + grace
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if pid_alive(pid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
    return True


def acquire_spawn_lock(identity: str) -> bool:
    """Try once to take the spawn lock for *identity*; False if already held (I1)."""
    path = lock_path(identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    return True


def release_spawn_lock(identity: str) -> None:
    """Release the spawn lock for *identity* (no-op if absent)."""
    lock_path(identity).unlink(missing_ok=True)


def sweep_stale() -> None:
    """Remove dead entries/leases and reap orphaned services (I2/I3/I7)."""
    for entry in list_service_entries():
        identity = entry["identity"]
        if not pid_alive(entry.get("pid")):
            _log.info("Sweeping dead service entry %s", identity)
            remove_service_entry(identity)
            continue
        if pid_alive(entry.get("owner_pid")):
            continue  # owned by a live gateway; single writer owns its fate (I1)
        if any(
            identity in lease.get("identities", [])
            for lease in list_leases()
            if pid_alive(lease.get("gateway_pid"))
        ):
            continue  # a live lease still depends on it (I3); its reaper decides
        killed = kill_entry_processes(entry)
        _log.info(
            "Sweeping orphaned service %s (owner gateway dead, killed=%s)",
            identity,
            killed,
        )
        remove_service_entry(identity)  # regardless of kill result (I2)
    for lease in list_leases():
        if not pid_alive(lease.get("gateway_pid")):
            _log.info("Sweeping dead lease for gateway %s", lease["gateway_port"])
            remove_lease(lease["gateway_port"])


def compute_service_identity(svc: ServiceConfig, upstream_url: str) -> str:
    """Canonical identity for a service wiring (I6): sha256 of the 5-tuple."""
    payload = json.dumps(
        [
            svc.name,
            svc.directory,
            svc.port,
            upstream_url,
            chain.resolve_command(svc, upstream_url),
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
