"""Tests for manifold.logs module."""

from pathlib import Path
from unittest.mock import patch

from manifold.logs import clear_logs, get_log_path, list_logs, setup_service_log, tail_log


def test_setup_service_log(tmp_path: Path):
    with patch("manifold.logs.LOG_DIR", tmp_path):
        logger = setup_service_log("test-svc")
        assert logger.name == "manifold.service.test-svc"
        log_path = tmp_path / "test-svc.log"
        logger.info("hello from test")
        assert log_path.exists()
        assert "hello from test" in log_path.read_text()


def test_tail_log(tmp_path: Path):
    log_path = tmp_path / "svc.log"
    log_path.write_text("\n".join(f"line {i}" for i in range(100)))
    with patch("manifold.logs.LOG_DIR", tmp_path):
        lines = tail_log("svc", lines=5)
        assert len(lines) == 5
        assert lines[-1] == "line 99"


def test_tail_log_missing(tmp_path: Path):
    with patch("manifold.logs.LOG_DIR", tmp_path):
        assert tail_log("nonexistent") == []


def test_list_logs(tmp_path: Path):
    (tmp_path / "a.log").write_text("data")
    (tmp_path / "b.log").write_text("more data")
    with patch("manifold.logs.LOG_DIR", tmp_path):
        result = list_logs()
        assert len(result) == 2
        names = [r["service"] for r in result]
        assert "a" in names
        assert "b" in names


def test_clear_logs_specific(tmp_path: Path):
    (tmp_path / "a.log").write_text("data")
    (tmp_path / "b.log").write_text("data")
    with patch("manifold.logs.LOG_DIR", tmp_path):
        cleared = clear_logs("a")
        assert cleared == 1
        assert not (tmp_path / "a.log").exists()
        assert (tmp_path / "b.log").exists()


def test_clear_logs_all(tmp_path: Path):
    (tmp_path / "a.log").write_text("data")
    (tmp_path / "b.log").write_text("data")
    with patch("manifold.logs.LOG_DIR", tmp_path):
        cleared = clear_logs()
        assert cleared == 2
        assert not list(tmp_path.glob("*.log"))
