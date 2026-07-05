"""Tests for launch admission control (worker cap + memory floor)."""

from __future__ import annotations

from projects_orchestrator.guard import admit, mem_available_bytes


def _meminfo(tmp_path, kib):
    """Write a minimal meminfo file reporting ``kib`` kB available."""
    path = tmp_path / "meminfo"
    path.write_text(f"MemTotal: 100 kB\nMemAvailable: {kib} kB\n", encoding="utf-8")
    return path


def test_mem_available_bytes_parses_kib(tmp_path):
    assert mem_available_bytes(_meminfo(tmp_path, 2048)) == 2048 * 1024


def test_mem_available_bytes_missing_field_returns_none(tmp_path):
    path = tmp_path / "meminfo"
    path.write_text("MemTotal: 100 kB\n", encoding="utf-8")
    assert mem_available_bytes(path) is None


def test_mem_available_bytes_unreadable_returns_none(tmp_path):
    assert mem_available_bytes(tmp_path / "absent") is None


def test_admit_refuses_at_worker_cap():
    assert admit(2, max_workers=2, min_free_bytes=0, available=lambda: 10**12).ok is False


def test_admit_refuses_when_memory_low():
    assert admit(0, max_workers=4, min_free_bytes=1024, available=lambda: 512).ok is False


def test_admit_allows_with_headroom():
    assert admit(0, max_workers=4, min_free_bytes=1024, available=lambda: 4096).ok is True


def test_admit_allows_when_memory_unknown():
    assert admit(0, max_workers=4, min_free_bytes=1024, available=lambda: None).ok is True
