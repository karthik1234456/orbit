"""Tests for LoopGuard: step limits, repeated failure detection, error thresholds, and timeouts."""


from core.guards import LoopGuard
from core.task import RunStatus


def test_total_call_limit():
    """Verify check_before returns STEP_LIMIT when total tool calls reach max_tool_calls."""
    guard = LoopGuard(max_tool_calls=3)

    # 1st call
    assert guard.check_before("calculate", {"expr": "1+1"}) is None
    guard.record("calculate", {"expr": "1+1"}, ok=True)

    # 2nd call
    assert guard.check_before("calculate", {"expr": "2+2"}) is None
    guard.record("calculate", {"expr": "2+2"}, ok=True)

    # 3rd call
    assert guard.check_before("calculate", {"expr": "3+3"}) is None
    guard.record("calculate", {"expr": "3+3"}, ok=True)

    # 4th call exceeds limit of 3
    assert guard.check_before("calculate", {"expr": "4+4"}) == RunStatus.STEP_LIMIT


def test_failing_call_repeated_is_blocked_but_succeeded_call_is_allowed():
    """Verify failing calls repeat threshold triggers LOOP_DETECTED, but successful calls are allowed."""
    guard = LoopGuard(max_repeats=2)

    failing_tool = "broken_tool"
    failing_args = {"param": "crash"}

    # 1st failure
    assert guard.check_before(failing_tool, failing_args) is None
    guard.record(failing_tool, failing_args, ok=False)

    # 2nd failure
    assert guard.check_before(failing_tool, failing_args) is None
    guard.record(failing_tool, failing_args, ok=False)

    # 3rd attempt with same failing call is now blocked
    assert guard.check_before(failing_tool, failing_args) == RunStatus.LOOP_DETECTED

    # In contrast, a call that succeeds can be called multiple times without being blocked
    safe_tool = "read_file"
    safe_args = {"path": "notes.txt"}

    for _ in range(5):
        assert guard.check_before(safe_tool, safe_args) is None
        guard.record(safe_tool, safe_args, ok=True)

    assert guard.check_before(safe_tool, safe_args) is None


def test_different_args_are_not_a_repeat():
    """Verify different arguments for the same tool do not count toward repeated failure threshold."""
    guard = LoopGuard(max_repeats=2)

    # Fail twice with param=1
    guard.record("fetch_data", {"param": 1}, ok=False)
    guard.record("fetch_data", {"param": 1}, ok=False)
    assert guard.check_before("fetch_data", {"param": 1}) == RunStatus.LOOP_DETECTED

    # Call with param=2 is NOT a repeat and should be permitted
    assert guard.check_before("fetch_data", {"param": 2}) is None

    # Args with different key ordering match identically due to sort_keys=True
    guard.record("fetch_data", {"a": 10, "b": 20}, ok=False)
    guard.record("fetch_data", {"b": 20, "a": 10}, ok=False)
    assert guard.check_before("fetch_data", {"a": 10, "b": 20}) == RunStatus.LOOP_DETECTED


def test_consecutive_errors_reset_after_a_success():
    """Verify consecutive error counter increments on failure and resets to 0 upon any success."""
    guard = LoopGuard(max_consecutive_errors=3)

    assert not guard.should_stop_for_errors()

    guard.record("tool_a", {}, ok=False)
    assert guard.consecutive_errors == 1
    assert not guard.should_stop_for_errors()

    guard.record("tool_b", {}, ok=False)
    assert guard.consecutive_errors == 2
    assert not guard.should_stop_for_errors()

    # Success resets consecutive error counter
    guard.record("tool_c", {}, ok=True)
    assert guard.consecutive_errors == 0
    assert not guard.should_stop_for_errors()

    # 3 consecutive errors trigger should_stop_for_errors
    guard.record("tool_d", {}, ok=False)
    guard.record("tool_e", {}, ok=False)
    guard.record("tool_f", {}, ok=False)
    assert guard.consecutive_errors == 3
    assert guard.should_stop_for_errors() is True


def test_timeout_using_fake_clock():
    """Verify elapsed time > max_seconds triggers STEP_LIMIT using a controllable fake clock."""
    current_time = 1000.0

    def fake_clock() -> float:
        return current_time

    guard = LoopGuard(max_seconds=120.0, clock=fake_clock)

    # Immediate call is within time budget
    assert guard.check_before("calculate", {}) is None

    # Advance clock by 60 seconds (total elapsed = 60s <= 120s)
    current_time += 60.0
    assert guard.check_before("calculate", {}) is None

    # Advance clock past 120 seconds total (elapsed = 125s > 120s)
    current_time += 65.0
    assert guard.check_before("calculate", {}) == RunStatus.STEP_LIMIT


def test_reset_clears_counters_and_timers():
    """Verify reset restores guard to initial pristine state."""
    current_time = 500.0

    def fake_clock() -> float:
        return current_time

    guard = LoopGuard(max_tool_calls=2, max_consecutive_errors=2, max_seconds=60.0, clock=fake_clock)

    guard.record("tool", {"x": 1}, ok=False)
    guard.record("tool", {"x": 1}, ok=False)
    assert guard.check_before("tool", {"x": 1}) is not None

    current_time += 70.0

    # Reset guard
    guard.reset()

    assert guard.total_tool_calls == 0
    assert guard.consecutive_errors == 0
    assert len(guard.failed_calls) == 0
    assert guard.start_time == 570.0
    assert guard.check_before("tool", {"x": 1}) is None
