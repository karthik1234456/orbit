"""Provider initialization and launch configuration for ORBIT UI."""

import os
from typing import Optional

from config import settings
from core.offline import enable_network_guard
from core.providers import ProviderChoice, make_provider as core_make_provider


def make_provider(name: Optional[str] = None) -> ProviderChoice:
    """Determine LLM provider, mode label, and startup notice based on environment."""
    if os.getenv("ORBIT_OFFLINE", "").strip().lower() in ("true", "1", "yes", "on") or getattr(settings, "ORBIT_OFFLINE", False):
        enable_network_guard()
    return core_make_provider(name=name)
