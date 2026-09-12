"""GPU memory, as nvidia-smi reports it (spec §10b, ROADMAP subsystem 11).

Verified 2026-09-10 on the target box (16 GB RTX-generation laptop GPU, Windows):

    nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits
    1482, 16376

MiB, one line per GPU. The per-process query (`--query-compute-apps=pid,...,used_memory`) prints
`[N/A]` for every process under Windows' WDDM driver model, so a model's own share cannot be read
directly; it is measured as the difference in total use before and after loading it.

Never fatal: no nvidia-smi, a timeout or odd output all read as "unknown" (None).
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

QUERY = ["--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"]


@dataclass(frozen=True)
class Reading:
    used_mib: int
    total_mib: int

    @property
    def used_gb(self) -> float:
        return self.used_mib / 1024.0

    @property
    def total_gb(self) -> float:
        return self.total_mib / 1024.0


def parse(text: str) -> Reading | None:
    """First GPU's `used, total` line -> Reading. Anything else -> None."""
    line = next((l for l in (text or "").splitlines() if l.strip()), "")
    parts = [p.strip() for p in line.split(",")]
    try:
        return Reading(int(float(parts[0])), int(float(parts[1])))
    except (IndexError, ValueError):
        return None


def read(timeout: float = 5.0) -> Reading | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        done = subprocess.run([exe, *QUERY], capture_output=True, text=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return parse(done.stdout) if done.returncode == 0 else None


def over(reading: Reading | None, limit_gb: float) -> bool:
    """Above the spec §10b cap (VRAM_WARN_GB). Unknown is not over: no false alarms."""
    return reading is not None and reading.used_mib > limit_gb * 1024.0
