"""Tests for manifold.paths."""

import socket
from unittest.mock import patch

from manifold.paths import (
    atomic_write_text,
    check_port_collisions,
    is_port_in_use,
    pid_file_for,
    port_file_for,
)


def test_atomic_write_text_roundtrip(tmp_path):
    dest = tmp_path / "state.txt"
    atomic_write_text(dest, "pid-42")
    assert dest.read_text() == "pid-42"


def test_pid_file_for():
    p = pid_file_for(9000)
    assert p.name == "manifold-9000.pid"


def test_port_file_for():
    p = port_file_for(9001)
    assert p.name == "manifold-9001.port"


def test_is_port_in_use_free():
    # Find a free port and verify it's not in use
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    assert not is_port_in_use(free_port)


def test_is_port_in_use_bound():
    # Bind a port, then check it's detected as in use
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        bound_port = s.getsockname()[1]
        assert is_port_in_use(bound_port)


def test_check_port_collisions_no_collision():
    # All ports free — mock is_port_in_use to always return False
    with patch("manifold.paths.is_port_in_use", return_value=False):
        errors = check_port_collisions(9000, {"svc-a": 7001, "svc-b": 7002})
    assert errors == []


def test_check_port_collisions_gateway_collision():
    def _mock(port, host="127.0.0.1"):
        return port == 9000

    with patch("manifold.paths.is_port_in_use", side_effect=_mock):
        errors = check_port_collisions(9000, {"svc-a": 7001})
    assert len(errors) == 1
    assert "Gateway port 9000" in errors[0]


def test_check_port_collisions_service_collision():
    def _mock(port, host="127.0.0.1"):
        return port == 7001

    with patch("manifold.paths.is_port_in_use", side_effect=_mock):
        errors = check_port_collisions(9000, {"svc-a": 7001, "svc-b": 7002})
    assert len(errors) == 1
    assert "svc-a" in errors[0]
