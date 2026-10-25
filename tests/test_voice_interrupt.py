"""Tests for voice/interrupt.py: InterruptMonitor key polling, arm/disarm, pause, and cancellation."""

import time
from voice.interrupt import InterruptMonitor


class FakeKeyQueue:
    def __init__(self, keys: list[str]):
        self.keys = list(keys)

    def pop_key(self) -> str | None:
        if self.keys:
            return self.keys.pop(0)
        return None


def test_enter_space_esc_while_armed_sets_event():
    """Enter, Space, and Esc set the interrupt event when armed."""
    for key in ("\r", "\n", " ", "\x1b"):
        kq = FakeKeyQueue([key])
        monitor = InterruptMonitor(key_source=kq.pop_key)
        try:
            assert not monitor.event.is_set()
            monitor.arm()
            # Wait up to 200ms for polling loop
            start = time.monotonic()
            while not monitor.event.is_set() and (time.monotonic() - start) < 0.2:
                time.sleep(0.01)
            assert monitor.event.is_set()
            assert kq.keys == []
        finally:
            monitor.close()


def test_other_keys_do_not_set_event_but_are_consumed():
    """Non-interrupt keys do not set the event, but are consumed/ignored."""
    kq = FakeKeyQueue(["a", "b", "c"])
    monitor = InterruptMonitor(key_source=kq.pop_key)
    try:
        monitor.arm()
        start = time.monotonic()
        while len(kq.keys) > 0 and (time.monotonic() - start) < 0.2:
            time.sleep(0.01)
        assert len(kq.keys) == 0
        assert not monitor.event.is_set()
    finally:
        monitor.close()


def test_while_disarmed_key_stays_unread():
    """While disarmed, pending keys are never read or touched."""
    kq = FakeKeyQueue(["y", "n"])
    monitor = InterruptMonitor(key_source=kq.pop_key)
    try:
        # Default state is disarmed
        time.sleep(0.12)
        assert kq.keys == ["y", "n"]
        assert not monitor.event.is_set()
    finally:
        monitor.close()


def test_while_paused_key_stays_unread():
    """While paused, polling does not consume keys, protecting confirmation prompts."""
    kq = FakeKeyQueue(["y"])
    monitor = InterruptMonitor(key_source=kq.pop_key)
    try:
        monitor.arm()
        with monitor.paused():
            time.sleep(0.12)
            assert kq.keys == ["y"]
            assert not monitor.event.is_set()

        # Once unpaused, pending key is processed
        start = time.monotonic()
        while len(kq.keys) > 0 and (time.monotonic() - start) < 0.2:
            time.sleep(0.01)
        assert len(kq.keys) == 0
        assert not monitor.event.is_set()
    finally:
        monitor.close()


def test_close_stops_the_thread():
    """Calling close() terminates the polling thread."""
    monitor = InterruptMonitor(key_source=lambda: None)
    assert monitor._thread is not None
    assert monitor._thread.is_alive()
    monitor.close()
    assert not monitor._thread.is_alive()
