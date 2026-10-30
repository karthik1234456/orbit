"""Tests for desktop screenshot capture tool."""

from pathlib import Path
from security.permissions import PermissionLevel
from tools.screenshot_tools import make_screenshot_tools


class FakeImage:
    def __init__(self):
        self.saved_path = None

    def save(self, path, *args, **kwargs):
        self.saved_path = path


def test_screenshot_naming_and_rate_limit(tmp_path: Path):
    """Verify screenshot timestamp naming and 20/hr rate limit."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    fake_clock_time = [1700000000.0]  # Mon Nov 13 2023 22:13:20 UTC
    fake_img = FakeImage()

    tools = {
        t.name: t
        for t in make_screenshot_tools(
            workspace_dir_getter=lambda: ws,
            grabber=lambda: fake_img,
            disk_free_getter=lambda p: 500 * 1024 * 1024,  # 500MB free
            clock=lambda: fake_clock_time[0],
        )
    }

    t_sc = tools["take_screenshot"]
    assert t_sc.permission == PermissionLevel.SAFE
    assert t_sc.model_visible is False

    res = t_sc.func()
    assert "Saved a screenshot to workspace/screenshots/" in res
    assert fake_img.saved_path is not None
    assert str(fake_img.saved_path).endswith(".png")

    # Rate limiting: 20 per hour
    for _ in range(19):
        t_sc.func()

    # 21st attempt
    res_limited = t_sc.func()
    assert "Rate limit reached" in res_limited

    # Fast forward 3601s -> allowed again
    fake_clock_time[0] += 3601.0
    res_ok = t_sc.func()
    assert "Saved a screenshot" in res_ok


def test_screenshot_low_disk_and_missing_pillow(tmp_path: Path):
    """Verify refusal on low disk space and friendly error when Pillow is missing."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    # Low disk space (< 200 MB)
    tools_low_disk = {
        t.name: t
        for t in make_screenshot_tools(
            workspace_dir_getter=lambda: ws,
            grabber=lambda: FakeImage(),
            disk_free_getter=lambda p: 50 * 1024 * 1024,  # 50MB free
            clock=lambda: 1000.0,
        )
    }
    res_disk = tools_low_disk["take_screenshot"].func()
    assert "low disk space" in res_disk.lower()

    # Missing Pillow error
    def fake_missing_pillow():
        raise RuntimeError("Pillow is required for taking screenshots. Install it with: pip install pillow")

    tools_missing_pil = {
        t.name: t
        for t in make_screenshot_tools(
            workspace_dir_getter=lambda: ws,
            grabber=fake_missing_pillow,
            disk_free_getter=lambda p: 500 * 1024 * 1024,
            clock=lambda: 1000.0,
        )
    }
    res_pil = tools_missing_pil["take_screenshot"].func()
    assert "pip install pillow" in res_pil
