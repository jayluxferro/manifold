"""Tests for manifold.paths."""

from manifold.paths import atomic_write_text


def test_atomic_write_text_roundtrip(tmp_path):
    dest = tmp_path / "state.txt"
    atomic_write_text(dest, "pid-42")
    assert dest.read_text() == "pid-42"
