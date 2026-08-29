"""Tests for manifold.logs module."""

import asyncio
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

import manifold.logs as logs_mod
from manifold.logs import (
    ServiceColorFormatter,
    clear_logs,
    console_supports_color,
    list_logs,
    service_color,
    setup_service_log,
    tail_log,
)


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


@pytest.fixture(autouse=True)
def _reset_service_colors():
    logs_mod._service_colors.clear()
    yield
    logs_mod._service_colors.clear()


def test_service_color_is_stable_and_distinct():
    first = service_color("alpha")
    assert service_color("alpha") == first  # stable across calls
    others = {service_color(name) for name in ("beta", "gamma", "delta")}
    assert first not in others
    assert len(others) == 3  # distinct services get distinct colors


def test_service_color_cycles_when_palette_exhausted():
    names = [f"svc-{i}" for i in range(len(logs_mod._SERVICE_PALETTE) + 1)]
    colors = [service_color(n) for n in names]
    assert colors[-1] == colors[0]  # wraps around the palette


def _record(msg: str, service_name: str | None = None) -> logging.LogRecord:
    record = logging.LogRecord(
        "manifold.process", logging.INFO, __file__, 0, msg, (), None
    )
    if service_name is not None:
        record.service_name = service_name
    return record


def test_formatter_colors_service_lines_when_enabled():
    fmt = ServiceColorFormatter("%(message)s", use_color=True)
    out = fmt.format(_record("[palisade/stderr] hello", service_name="palisade"))
    assert out == f"{service_color('palisade')}[palisade/stderr] hello\x1b[0m"


def test_formatter_leaves_manifold_lines_plain():
    fmt = ServiceColorFormatter("%(message)s", use_color=True)
    assert fmt.format(_record("Starting palisade")) == "Starting palisade"


def test_formatter_never_colors_when_disabled():
    fmt = ServiceColorFormatter("%(message)s", use_color=False)
    out = fmt.format(_record("[palisade/stderr] hello", service_name="palisade"))
    assert "\x1b[" not in out


def test_console_supports_color_gating(monkeypatch):
    class _Tty:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    assert console_supports_color(_Tty())
    assert not console_supports_color(stream=object())  # no isatty at all
    monkeypatch.setenv("NO_COLOR", "1")
    assert not console_supports_color(_Tty())


async def test_stream_output_tags_records_with_service_name():
    from manifold.process import _stream_output

    reader = asyncio.StreamReader()
    reader.feed_data(b"first line\nsecond line\n")
    reader.feed_eof()

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    proc_log = logging.getLogger("manifold.process")
    handler = _Capture()
    old_level = proc_log.level
    proc_log.setLevel(logging.DEBUG)  # root defaults to WARNING; INFO must pass
    proc_log.addHandler(handler)
    try:
        await _stream_output("palisade", reader, "stderr")
    finally:
        proc_log.removeHandler(handler)
        proc_log.setLevel(old_level)

    assert [r.getMessage() for r in records] == [
        "[palisade/stderr] first line",
        "[palisade/stderr] second line",
    ]
    assert all(getattr(r, "service_name", None) == "palisade" for r in records)
