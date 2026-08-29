"""Log aggregation — per-service log files and unified log viewer."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

LOG_DIR = Path.home() / ".manifold" / "logs"

_RESET = "\x1b[0m"

# 256-color palette chosen for visual distinctness on dark terminals.  Colors
# are assigned in registration order (which is pipeline order, since services
# log their first line when started sequentially), cycling if a config ever
# has more services than colors.  Per-process by design: each manifold
# instance has its own console, so cross-instance consistency is unnecessary.
_SERVICE_PALETTE = (51, 82, 207, 75, 220, 168, 149, 147, 189, 121)
_service_colors: dict[str, int] = {}


def service_color(name: str) -> str:
    """Return the ANSI 256-color escape assigned to a service (stable per name)."""
    if name not in _service_colors:
        _service_colors[name] = _SERVICE_PALETTE[
            len(_service_colors) % len(_SERVICE_PALETTE)
        ]
    return f"\x1b[38;5;{_service_colors[name]}m"


def console_supports_color(stream=None) -> bool:
    """Color only on a real terminal; NO_COLOR (https://no-color.org) opts out.

    Defaults to stderr because logging.StreamHandler writes there.
    """
    stream = stream if stream is not None else sys.stderr
    return (
        hasattr(stream, "isatty") and stream.isatty() and not os.environ.get("NO_COLOR")
    )


class ServiceColorFormatter(logging.Formatter):
    """Color the whole console line in the originating service's color.

    Records relayed from service subprocesses carry ``extra={"service_name": ...}``
    (see process._stream_output); manifold's own lines have no service_name and
    stay in the default color, which keeps the control plane visually distinct
    from the pipeline layers.
    """

    def __init__(self, *args, use_color: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        name = getattr(record, "service_name", None)
        if self.use_color and name:
            return f"{service_color(name)}{text}{_RESET}"
        return text


def setup_service_log(name: str) -> logging.Logger:
    """Create a file logger for a specific service.

    Logs are written to ~/.manifold/logs/<name>.log.
    Returns a logger instance that writes to both the file and the root logger.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{name}.log"

    logger = logging.getLogger(f"manifold.service.{name}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # file-only; console output handled by process module

    # Avoid duplicate handlers on restart
    if not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        handler = logging.FileHandler(log_path)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)

    return logger


def get_log_path(name: str) -> Path:
    """Return the log file path for a service."""
    return LOG_DIR / f"{name}.log"


def tail_log(name: str, lines: int = 50) -> list[str]:
    """Read the last N lines from a service's log file."""
    log_path = get_log_path(name)
    if not log_path.exists():
        return []
    all_lines = log_path.read_text().splitlines()
    return all_lines[-lines:]


def list_logs() -> list[dict]:
    """List all available service logs with sizes."""
    if not LOG_DIR.exists():
        return []
    result = []
    for p in sorted(LOG_DIR.glob("*.log")):
        result.append(
            {
                "service": p.stem,
                "path": str(p),
                "size_bytes": p.stat().st_size,
            }
        )
    return result


def clear_logs(name: str | None = None) -> int:
    """Clear log files. If name is given, clear only that service's log.

    Returns the number of files cleared.
    """
    if not LOG_DIR.exists():
        return 0
    count = 0
    if name:
        p = get_log_path(name)
        if p.exists():
            p.unlink()
            count = 1
    else:
        for p in LOG_DIR.glob("*.log"):
            p.unlink()
            count += 1
    return count
