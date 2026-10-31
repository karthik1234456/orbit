"""Audio cue beeper module for ORBIT hands-free events.

Plays short distinctive audio tones via winsound.Beep on a daemon background
thread so the calling thread and UI are never blocked. Silently disables itself
when winsound is absent (e.g. non-Windows) or HANDS_FREE_CUES is turned off.
Cues never carry any text.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

try:
    import winsound
except ImportError:
    winsound = None  # type: ignore[assignment]


def _default_beep(frequency: int, duration_ms: int) -> None:
    """Default beeper using Windows winsound.Beep."""
    if winsound is not None and hasattr(winsound, "Beep"):
        try:
            winsound.Beep(int(frequency), int(duration_ms))
        except Exception:
            pass


class Beeper:
    """Thread-safe, rate-limited audio tone player for hands-free state transitions."""

    def __init__(
        self,
        beep_fn: Optional[Callable[[int, int], None]] = None,
        enabled: Optional[Callable[[], bool]] = None,
        clock: Optional[Callable[[], float]] = None,
        min_interval_s: float = 0.05,
    ) -> None:
        self._beep_fn = beep_fn if beep_fn is not None else _default_beep
        self._enabled_fn = enabled if enabled is not None else (lambda: True)
        self._clock = clock if clock is not None else time.monotonic
        self._min_interval_s = float(min_interval_s)
        self._last_play_time: float = -999.0
        self._lock = threading.Lock()
        self.history: list[str] = []

    def is_enabled(self) -> bool:
        """Check whether beeper sound is currently enabled."""
        if winsound is None and self._beep_fn == _default_beep:
            return False
        try:
            return bool(self._enabled_fn())
        except Exception:
            return False

    def play(self, cue: str) -> None:
        """Play a named cue asynchronously on a daemon thread.

        Supported cues:
        - "wake": One rising tone (e.g. 800 Hz -> 1200 Hz).
        - "done": One soft tone (600 Hz).
        - "error": Two descending tones (500 Hz -> 350 Hz).
        - "attention": Two quick tones (880 Hz, pause, 880 Hz).
        - "off": One low tone (300 Hz).
        """
        cue_lower = str(cue).strip().lower()
        if not self.is_enabled():
            return

        now = self._clock()
        with self._lock:
            if (now - self._last_play_time) < self._min_interval_s:
                return
            self._last_play_time = now
            self.history.append(cue_lower)

        worker = threading.Thread(
            target=self._play_tones,
            args=(cue_lower,),
            daemon=True,
            name=f"BeeperThread-{cue_lower}",
        )
        worker.start()

    def _play_tones(self, cue: str) -> None:
        """Execute frequency/duration pattern for the given cue."""
        try:
            if cue == "wake":
                # One rising tone
                self._beep_fn(800, 80)
                self._beep_fn(1200, 100)
            elif cue == "done":
                # One soft tone
                self._beep_fn(600, 100)
            elif cue == "error":
                # Two descending tones
                self._beep_fn(500, 90)
                self._beep_fn(350, 120)
            elif cue == "attention":
                # Two quick alert tones
                self._beep_fn(880, 60)
                time.sleep(0.04)
                self._beep_fn(880, 60)
            elif cue == "off":
                # One low tone
                self._beep_fn(300, 140)
        except Exception as exc:
            logger.debug("Beeper tone execution failed: %s", exc)
