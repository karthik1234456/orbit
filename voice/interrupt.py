"""Keyboard interrupt monitoring for voice output cancellation."""

from contextlib import contextmanager
import threading
from typing import Callable, Optional

# ==============================================================================
# SECURITY ARCHITECTURE NOTE: WHY BACKGROUND KEY MONITORING MUST RESPECT PAUSE
# ==============================================================================
# The InterruptMonitor polling thread continuously reads pending keystrokes from the
# console (Enter, Space, Esc) to interrupt speech playback.
#
# When ORBIT reaches a confirmation prompt for a CONFIRM-level tool, the user is
# prompted in the terminal: "Allow? (y/n): ".
# If the interrupt monitor continued reading console keystrokes while the prompt
# was waiting for user input, the background thread would consume and steal the
# "y" or "n" typed by the user before input() could read it!
#
# Therefore, while disarmed or paused (via `with monitor.paused():`), the monitor
# must NEVER touch, read, or poll pending keystrokes.
# ==============================================================================


class InterruptMonitor:
    """Monitors console keyboard input to trigger an interrupt event during speech playback."""

    def __init__(self, key_source: Optional[Callable[[], Optional[str]]] = None) -> None:
        self.event = threading.Event()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._armed = False
        self._paused = False
        self._closed = False

        if key_source is not None:
            self._key_source = key_source
        else:
            self._key_source = self._create_default_key_source()

        self._thread: Optional[threading.Thread] = None
        self._start_thread()

    @staticmethod
    def _create_default_key_source() -> Callable[[], Optional[str]]:
        try:
            import msvcrt

            def _msvcrt_source() -> Optional[str]:
                if msvcrt.kbhit():
                    return msvcrt.getwch()
                return None

            return _msvcrt_source
        except ImportError:
            # Non-Windows or environments without msvcrt: monitor never triggers
            return lambda: None

    def _start_thread(self) -> None:
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _poll_loop(self) -> None:
        while not self._closed:
            with self._lock:
                should_read = self._armed and not self._paused and not self._closed

            if should_read:
                try:
                    while True:
                        with self._lock:
                            if not self._armed or self._paused or self._closed:
                                break
                        key = self._key_source()
                        if key is None:
                            break
                        if key in ("\r", "\n", " ", "\x1b"):
                            self.event.set()
                            break
                        # Any other key is consumed and ignored
                except Exception:
                    pass

            self._stop_event.wait(0.05)

    def arm(self) -> None:
        """Clear the interrupt event and start consuming keys."""
        with self._lock:
            self.event.clear()
            self._armed = True
            if self._closed:
                self._closed = False
                self._stop_event.clear()
                self._start_thread()
            elif self._thread is None or not self._thread.is_alive():
                self._start_thread()

    def disarm(self) -> None:
        """Stop consuming keys."""
        with self._lock:
            self._armed = False

    @contextmanager
    def paused(self):
        """Context manager during which the polling thread must NOT read keys."""
        with self._lock:
            self._paused = True
        try:
            yield
        finally:
            with self._lock:
                self._paused = False

    def close(self) -> None:
        """Stop the polling thread."""
        with self._lock:
            self._closed = True
            self._armed = False
            self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            if threading.current_thread() != self._thread:
                self._thread.join(timeout=0.5)
