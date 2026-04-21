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


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Return True if *port* is already bound on *host*."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.1)
        return s.connect_ex((host, port)) == 0


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
