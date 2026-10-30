"""System telemetry provider for ORBIT live HUD arc visualizers.

Samples CPU and Memory utilization safely using psutil with injectable sampling
providers and zero-exception guarantees.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class SystemTelemetry:
    """Snapshot of current system telemetry."""

    cpu_percent: Optional[float]
    mem_percent: Optional[float]
    mem_used_gb: Optional[float]
    mem_total_gb: Optional[float]

    @property
    def has_data(self) -> bool:
        return self.cpu_percent is not None and self.mem_percent is not None

    def format_cpu_tooltip(self) -> str:
        if self.cpu_percent is None:
            return "CPU: n/a"
        return f"CPU {int(round(self.cpu_percent))}%"

    def format_memory_tooltip(self) -> str:
        if self.mem_percent is None or self.mem_used_gb is None or self.mem_total_gb is None:
            return "Memory: n/a"
        return f"Memory {int(round(self.mem_percent))}% ({self.mem_used_gb:.1f} of {self.mem_total_gb:.1f} GB)"


_CUSTOM_PROVIDER: Optional[Callable[[], SystemTelemetry]] = None


def set_telemetry_provider(provider: Optional[Callable[[], SystemTelemetry]]) -> None:
    """Inject a custom telemetry provider (used for testing or mocking)."""
    global _CUSTOM_PROVIDER
    _CUSTOM_PROVIDER = provider


def get_telemetry_provider() -> Optional[Callable[[], SystemTelemetry]]:
    """Retrieve the currently active custom telemetry provider, if any."""
    return _CUSTOM_PROVIDER


def sample_system_telemetry() -> SystemTelemetry:
    """Sample current CPU and memory statistics safely without blocking or raising."""
    if _CUSTOM_PROVIDER is not None:
        try:
            return _CUSTOM_PROVIDER()
        except Exception:
            return SystemTelemetry(None, None, None, None)

    try:
        import psutil

        cpu = float(psutil.cpu_percent(interval=None))
        vmem = psutil.virtual_memory()
        mem_pct = float(vmem.percent)
        used_gb = float(vmem.used) / (1024.0**3)
        total_gb = float(vmem.total) / (1024.0**3)
        return SystemTelemetry(
            cpu_percent=max(0.0, min(100.0, cpu)),
            mem_percent=max(0.0, min(100.0, mem_pct)),
            mem_used_gb=used_gb,
            mem_total_gb=total_gb,
        )
    except Exception:
        return SystemTelemetry(None, None, None, None)

