"""GPU memory readings (spec §10b). Hermetic: nvidia-smi output is given, never run."""
from __future__ import annotations

from backend import vram


def test_the_verified_output_parses():
    """The exact line nvidia-smi printed on the target box, 2026-09-10."""
    r = vram.parse("1482, 16376\n")
    assert r == vram.Reading(1482, 16376)
    assert round(r.used_gb, 2) == 1.45 and round(r.total_gb) == 16


def test_the_first_gpu_is_the_one_read():
    assert vram.parse("2000, 16376\n9000, 24576\n") == vram.Reading(2000, 16376)


def test_odd_output_is_unknown_not_an_error():
    for text in ("", "[N/A], [N/A]", "No devices were found", "12"):
        assert vram.parse(text) is None


def test_over_the_cap_and_unknown_is_never_an_alarm():
    assert vram.over(vram.Reading(10_300, 16_376), 10)
    assert not vram.over(vram.Reading(5_700, 16_376), 10)
    assert not vram.over(None, 10)


def test_no_nvidia_smi_reads_as_unknown(monkeypatch):
    monkeypatch.setattr(vram.shutil, "which", lambda name: None)
    assert vram.read() is None
