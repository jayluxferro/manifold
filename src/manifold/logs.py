"""Log aggregation — per-service log files and unified log viewer."""

from __future__ import annotations

import logging
from pathlib import Path

LOG_DIR = Path.home() / ".manifold" / "logs"


def setup_service_log(name: str) -> logging.Logger:
    """Create a file logger for a specific service.

    Logs are written to ~/.manifold/logs/<name>.log.
    Returns a logger instance that writes to both the file and the root logger.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{name}.log"

    logger = logging.getLogger(f"manifold.service.{name}")
    logger.setLevel(logging.DEBUG)

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
        result.append({
            "service": p.stem,
            "path": str(p),
            "size_bytes": p.stat().st_size,
        })
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
