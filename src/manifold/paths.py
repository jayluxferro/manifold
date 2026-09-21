"""Runtime filesystem locations and port utilities shared by the CLI and gateway."""

from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path

PID_DIR = Path.home() / ".manifold"
# Legacy singleton paths — kept as defaults for backwards compat
PID_FILE = PID_DIR / "manifold.pid"
PORT_FILE = PID_DIR / "manifold.port"


def pid_file_for(port: int) -> Path:
    """Return the PID file path for a specific gateway port."""
    return PID_DIR / f"manifold-{port}.pid"


def port_file_for(port: int) -> Path:
    """Return the port file path for a specific gateway port."""
    return PID_DIR / f"manifold-{port}.port"


def is_port_in_use(
    port: int, host: str = "127.0.0.1", *, any_address: bool = True
) -> bool:
    """Return True if *port* is already bound on *host* (or, by default,
    on ANY local address).

    The any-address sweep exists because macOS lets a specific-address
    listener (e.g. a crash shim bound to 127.0.0.1:P) coexist with a new
    wildcard bind (0.0.0.0:P): the spawn preflight used to probe only the
    requested host, saw "free", and the service came up "healthy" while
    127.0.0.1 traffic silently kept flowing to the shim — four layers
    bypassed and a latent forwarding loop (found by the Sep-20 forensics).
    Preflight now connects against every address the interface table
    exposes, so ANY listener on the port blocks the spawn (the gateway's
    own shim-reclaim path still clears reclaimable shims first).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.1)
        if s.connect_ex((host, port)) == 0:
            return True
    if not any_address:
        return False
    try:
        import subprocess

        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return bool(out.stdout.strip())
    except Exception:
        # lsof missing/slow: degrade to the single-host probe we had.
        return False


def check_port_collisions(
    gateway_port: int,
    service_ports: dict[str, int],
    host: str = "127.0.0.1",
) -> list[str]:
    """Check all ports for collisions and return a list of error messages.

    Checks the gateway port and every service port. Returns an empty list
    if no collisions are found.
    """
    errors: list[str] = []
    if is_port_in_use(gateway_port, host):
        errors.append(f"Gateway port {gateway_port} is already in use")
    for name, port in service_ports.items():
        if is_port_in_use(port, host):
            errors.append(f"Port {port} for service '{name}' is already in use")
    return errors


def atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via temp file + ``os.replace`` (atomic on POSIX)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as tf:
        tmp_path = Path(tf.name)
        tf.write(content)
    os.replace(tmp_path, path)
