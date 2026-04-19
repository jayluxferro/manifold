"""Runtime filesystem locations shared by the CLI and gateway."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

PID_DIR = Path.home() / ".manifold"
PID_FILE = PID_DIR / "manifold.pid"
PORT_FILE = PID_DIR / "manifold.port"


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
