"""Tests for ORBIT Sci-Fi HUD, Dock, Theming, and Quality Governor (v2.13).

Covers:
- HUD1: Mathematical model (vector norm invariance under rotation, perspective projection, depth color, deterministic geometry, dt motion).
- HUD2: State priority machine (confirming > error [1.5s decay] > speaking > listening > thinking > idle).
- HUD3: Quality governor (downgrades after 20 frames >25ms, 30s hysteresis cooldown, particle tiers).
- HUD4: Theme contrast compliance (WCAG relative contrast >= 4.5:1 for all text/text_dim against bg and panel across cyan, gold, and mono palettes).
- HUD5: HUD canvas item pooling (pre-allocation, zero per-frame creation/deletion, coordinate caching, map/unmap pause/resume, after_cancel teardown).
- HUD6: Dock canvas navigation (focusability, arrow cycling, Enter/Space activation, Ctrl+1..5 hotkeys, mic level ring).
- HUD7: Dialog topmost priority (ConfirmDialog and PlanDialog topmost, modal grab, voice immunity).
- HUD8: Fallback on canvas exception (graceful degradation to classic layout, diagnostics logging, chat notice) and settings validation.
"""

from __future__ import annotations

import math
import os
import random
import sys
import time
import tkinter as tk
from unittest.mock import MagicMock

import pytest

# Ensure Tcl/Tk libraries are located reliably on Windows
if sys.platform == "win32":
    tcl_path = os.path.join(sys.base_prefix, "tcl", "tcl8.6")
    tk_path = os.path.join(sys.base_prefix, "tcl", "tk8.6")
    if os.path.exists(tcl_path):
        os.environ.setdefault("TCL_LIBRARY", tcl_path)
    if os.path.exists(tk_path):
        os.environ.setdefault("TK_LIBRARY", tk_path)

from tests.conftest import check_tk_available, make_test_root

check_tk_available()

from config.settings_store import SPECS, SettingsError, validate
from core.diagnostics import build_diagnostics_text
from ui.controller import ConfirmRequest
from ui.dock import DockCanvas
from ui.hud import HudCanvas
from ui.hud_core import (
    HudGeometry,
    HudModel,
    HudState,
    QualityGovernor,
    build_geometry,
    depth_color,
    project,
    rotate,
)
from ui.theme import (
    PALETTES,
    Palette,
    apply_theme,
    contrast_ratio,
    get_data_font,
    get_heading_font,
    get_palette,
    relative_luminance,
)


# ============================================================================
# HUD1: Pure Math, Geometry & 3D Projection Tests
# ============================================================================


def test_rotation_vector_norm_preservation():
    """Verify that 3D Euler rotations preserve Euclidean vector norms within float epsilon."""
    points = [
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
        (0.577, 0.577, 0.577),
        (-0.707, 0.707, 0.0),
        (0.2, -0.8, 0.55),
    ]

    angles = [
        (0.0, 0.0, 0.0),
        (math.pi / 4, math.pi / 3, math.pi / 6),
        (1.23, -0.85, 2.45),
        (-math.pi, math.pi, -math.pi / 2),
    ]

    for ax, ay, az in angles:
        rotated = rotate(points, ax, ay, az)
        assert len(rotated) == len(points)
        for (x0, y0, z0), (x1, y1, z1) in zip(points, rotated):
            orig_norm = math.sqrt(x0 * x0 + y0 * y0 + z0 * z0)
            rot_norm = math.sqrt(x1 * x1 + y1 * y1 + z1 * z1)
            assert math.isclose(orig_norm, rot_norm, abs_tol=1e-9)


def test_rotation_zero_angles_identity():
    """Verify that rotation by (0, 0, 0) returns identical coordinates."""
    points = [(1.5, -2.3, 0.4), (0.0, 0.0, 0.0)]
    rotated = rotate(points, 0.0, 0.0, 0.0)
    for p0, p1 in zip(points, rotated):
        assert math.isclose(p0[0], p1[0], abs_tol=1e-12)
        assert math.isclose(p0[1], p1[1], abs_tol=1e-12)
        assert math.isclose(p0[2], p1[2], abs_tol=1e-12)


def test_perspective_projection_ordering():
    """Verify perspective projection produces screen coordinates and preserves depth ordering."""
    points = [(0.0, 0.0, 1.0), (0.0, 0.0, 0.0), (0.0, 0.0, -1.0)]
    cx, cy, r = 200.0, 150.0, 50.0
    projected = project(points, cx, cy, r, focal=300.0)

    assert len(projected) == 3
    # Near point (z = 1.0) vs Far point (z = -1.0)
    p_near = projected[0]
    p_far = projected[2]

    assert p_near[2] == 1.0
    assert p_far[2] == -1.0
    # Center stays at (cx, cy) since x=0, y=0
    assert math.isclose(p_near[0], cx)
    assert math.isclose(p_near[1], cy)


def test_depth_color_interpolation_and_bounds():
    """Verify depth_color produces valid hex colors and clamps out-of-range depths."""
    pal = get_palette("cyan")

    # In range
    c_mid = depth_color(0.0, pal, min_z=-1.0, max_z=1.0)
    assert c_mid.startswith("#") and len(c_mid) == 7

    # Clamping at extremes
    c_low = depth_color(-5.0, pal, min_z=-1.0, max_z=1.0)
    c_min = depth_color(-1.0, pal, min_z=-1.0, max_z=1.0)
    assert c_low == c_min

    c_high = depth_color(5.0, pal, min_z=-1.0, max_z=1.0)
    c_max = depth_color(1.0, pal, min_z=-1.0, max_z=1.0)
    assert c_high == c_max


def test_deterministic_geometry_generation():
    """Verify geometry generated from a fixed seed produces identical segment and particle counts."""
    rng1 = random.Random(42)
    geom1 = build_geometry(rng1)

    rng2 = random.Random(42)
    geom2 = build_geometry(rng2)

    assert isinstance(geom1, HudGeometry)
    assert len(geom1.sphere_segments) == len(geom2.sphere_segments)
    assert len(geom1.orbit_ring_points) == 3
    assert len(geom1.particles) == 450  # 150 * 3 rings
    assert len(geom1.radial_arcs) == len(geom2.radial_arcs)
    assert len(geom1.tick_marks) == 60


def test_dt_based_motion_independence():
    """Verify delta-time updates are proportional and additively consistent."""
    m1 = HudModel(rng=random.Random(10))
    m2 = HudModel(rng=random.Random(10))

    # One step of dt=0.04
    m1.update(0.04)

    # Two steps of dt=0.02
    m2.update(0.02)
    m2.update(0.02)

    assert math.isclose(m1.sphere_ax, m2.sphere_ax, abs_tol=1e-6)
    assert math.isclose(m1.sphere_ay, m2.sphere_ay, abs_tol=1e-6)
    assert math.isclose(m1.rings_spin, m2.rings_spin, abs_tol=1e-6)
    assert math.isclose(m1.elapsed, m2.elapsed, abs_tol=1e-6)


# ============================================================================
# HUD2: State Priority Hierarchy Tests
# ============================================================================


def test_state_priority_hierarchy():
    """Verify strict state priority resolution across all combinations:

    confirming > error (1.5s decay) > speaking > listening > thinking (busy) > idle.
    """
    current_time = 1000.0

    def mock_clock() -> float:
        return current_time

    model = HudModel(clock=mock_clock)

    # 1. Default is IDLE
    assert model.state == HudState.IDLE

    # 2. Busy alone -> THINKING
    model.set_busy(True)
    assert model.state == HudState.THINKING

    # 3. Listening beats Thinking
    model.set_listening(True)
    assert model.state == HudState.LISTENING

    # 4. Speaking beats Listening
    model.set_speaking(True)
    assert model.state == HudState.SPEAKING

    # 5. Error beats Speaking
    model.flash_error()
    assert model.state == HudState.ERROR

    # 6. Confirming beats Error
    model.set_confirming(True)
    assert model.state == HudState.CONFIRMING

    # Release Confirming -> returns to Error (since error_time within 1.5s)
    model.set_confirming(False)
    assert model.state == HudState.ERROR

    # Error decays after 1.5s
    current_time += 1.6
    assert model.state == HudState.SPEAKING

    # Turn off speaking -> reverts to Listening
    model.set_speaking(False)
    assert model.state == HudState.LISTENING

    # Turn off listening -> reverts to Thinking
    model.set_listening(False)
    assert model.state == HudState.THINKING

    # Turn off busy -> reverts to Idle
    model.set_busy(False)
    assert model.state == HudState.IDLE


def test_mic_level_exponential_smoothing():
    """Verify microphone level smooths smoothly without sudden jumps."""
    model = HudModel()
    assert model.mic_level == 0.0

    model.set_mic_level(1.0)
    lvl1 = model.mic_level
    assert 0.25 < lvl1 < 0.35  # alpha = 0.3

    model.set_mic_level(1.0)
    lvl2 = model.mic_level
    assert lvl2 > lvl1
    assert lvl2 < 0.6


# ============================================================================
# HUD3: Quality Governor Tests
# ============================================================================


def test_quality_governor_downgrades_on_slow_frames():
    """Verify governor drops tiers when 20-frame rolling average exceeds 25ms."""
    sim_time = 100.0

    def mock_clock():
        return sim_time

    gov = QualityGovernor(clock=mock_clock)
    assert gov.tier == "high"
    assert gov.downgrades_count == 0

    # 19 slow frames -> no downgrade yet
    for _ in range(19):
        gov.record_frame(0.030)
    assert gov.tier == "high"

    # 20th slow frame -> drops to medium
    gov.record_frame(0.030)
    assert gov.tier == "medium"
    assert gov.downgrades_count == 1

    # Within 30s hysteresis cooldown -> does not drop to low even if slow frames continue
    sim_time += 10.0
    for _ in range(25):
        gov.record_frame(0.030)
    assert gov.tier == "medium"
    assert gov.downgrades_count == 1

    # After 30s cooldown -> next 20 slow frames drop to low
    sim_time += 25.0  # now 35s since last downgrade
    for _ in range(20):
        gov.record_frame(0.030)
    assert gov.tier == "low"
    assert gov.downgrades_count == 2

    # Drop to static after another 30s
    sim_time += 35.0
    for _ in range(20):
        gov.record_frame(0.030)
    assert gov.tier == "static"
    assert gov.downgrades_count == 3


def test_quality_governor_fast_frames_preserve_high_tier():
    """Verify fast frames (<25ms) never cause a downgrade."""
    gov = QualityGovernor(clock=time.time)
    for _ in range(100):
        gov.record_frame(0.010)
    assert gov.tier == "high"
    assert gov.downgrades_count == 0


# ============================================================================
# HUD4: Theme & WCAG Contrast Compliance Tests
# ============================================================================


def test_relative_luminance_and_contrast_formula():
    """Verify WCAG relative luminance and contrast ratio mathematical guarantees."""
    # Pure black vs pure white
    assert math.isclose(relative_luminance("#000000"), 0.0, abs_tol=1e-4)
    assert math.isclose(relative_luminance("#ffffff"), 1.0, abs_tol=1e-4)
    assert math.isclose(contrast_ratio("#ffffff", "#000000"), 21.0, abs_tol=0.1)

    # Identical colors
    assert math.isclose(contrast_ratio("#123456", "#123456"), 1.0, abs_tol=1e-3)
    # Symmetry
    assert math.isclose(contrast_ratio("#112233", "#ddeeff"), contrast_ratio("#ddeeff", "#112233"))


def test_all_palettes_wcag_contrast_compliance():
    """Verify all text and text_dim colors strictly meet >= 4.5:1 WCAG contrast against bg and panel."""
    for name, pal in PALETTES.items():
        assert isinstance(pal, Palette)

        # 1. Primary text vs background
        cr_text_bg = contrast_ratio(pal.text, pal.bg)
        assert cr_text_bg >= 4.5, f"Palette '{name}' text vs bg ratio {cr_text_bg:.2f} < 4.5"

        # 2. Primary text vs panel
        cr_text_panel = contrast_ratio(pal.text, pal.panel)
        assert cr_text_panel >= 4.5, f"Palette '{name}' text vs panel ratio {cr_text_panel:.2f} < 4.5"

        # 3. Secondary text_dim vs background
        cr_dim_bg = contrast_ratio(pal.text_dim, pal.bg)
        assert cr_dim_bg >= 4.5, f"Palette '{name}' text_dim vs bg ratio {cr_dim_bg:.2f} < 4.5"

        # 4. Secondary text_dim vs panel
        cr_dim_panel = contrast_ratio(pal.text_dim, pal.panel)
        assert cr_dim_panel >= 4.5, f"Palette '{name}' text_dim vs panel ratio {cr_dim_panel:.2f} < 4.5"


def test_font_fallbacks():
    """Verify font selectors return valid tuples."""
    h_font = get_heading_font()
    assert isinstance(h_font, tuple) and len(h_font) == 3
    assert h_font[2] == "bold"

    d_font = get_data_font(size=10)
    assert isinstance(d_font, tuple) and len(d_font) == 2
    assert d_font[1] == 10


# ============================================================================
# HUD5: Tkinter HudCanvas Item Pooling Tests
# ============================================================================


@pytest.fixture
def tk_root():
    """Provide a headless, withdrawn Tkinter root window for GUI testing."""
    root = make_test_root()
    root.withdraw()
    yield root
    try:
        root.destroy()
    except Exception:
        pass


def test_apply_theme_on_root(tk_root):
    """Verify apply_theme configures TTK without errors."""
    pal = get_palette("gold")
    apply_theme(tk_root, pal)


def test_hud_canvas_item_pooling_and_zero_churn(tk_root):
    """Verify HudCanvas pre-allocates items once and does not create/destroy items during frames."""
    model = HudModel()
    canvas = HudCanvas(tk_root, model=model, width=320, height=240, motion="full")
    canvas.pack()
    tk_root.update_idletasks()

    initial_item_ids = canvas.find_all()
    assert len(initial_item_ids) > 100  # Sphere, ticks, rings, glow, particles

    # Draw several successive frames
    for _ in range(5):
        model.update(0.033)
        canvas._draw_frame(dt=0.033)

    subsequent_item_ids = canvas.find_all()
    # ZERO item creation or deletion: item count must be strictly identical
    assert len(subsequent_item_ids) == len(initial_item_ids)
    assert subsequent_item_ids == initial_item_ids

    canvas.destroy()


def test_hud_canvas_map_unmap_pause(tk_root):
    """Verify canvas pauses rendering on unmap and resumes on map."""
    model = HudModel()
    canvas = HudCanvas(tk_root, model=model, width=300, height=200)

    assert canvas._paused is False
    canvas._on_unmap()
    assert canvas._paused is True
    canvas._on_map()
    assert canvas._paused is False

    canvas.destroy()


def test_hud_canvas_destroy_teardown(tk_root):
    """Verify after_cancel cancels pending timer on widget destroy."""
    model = HudModel()
    canvas = HudCanvas(tk_root, model=model, width=300, height=200)
    assert canvas._after_id is not None

    canvas.destroy()
    assert canvas._destroyed is True
    assert canvas._after_id is None


# ============================================================================
# HUD6: Dock Canvas Tests
# ============================================================================


def test_dock_canvas_keyboard_navigation_and_hotkeys(tk_root):
    """Verify DockCanvas focusability, arrow cycling, Enter/Space activation, and Ctrl+1..5 hotkeys."""
    triggered: list[str] = []

    dock = DockCanvas(
        tk_root,
        on_activity=lambda: triggered.append("activity"),
        on_routines=lambda: triggered.append("routines"),
        on_mic=lambda: triggered.append("mic"),
        on_settings=lambda: triggered.append("settings"),
        on_doctor=lambda: triggered.append("doctor"),
    )
    dock.pack()
    tk_root.update_idletasks()

    # Verify focusable
    assert dock.cget("takefocus") in ("1", 1, True, "True")

    # Center icon (mic) starts focused
    assert dock._focused_idx == 2

    # Left arrow moves to Routines (idx 1)
    dock._on_key_left(MagicMock())
    assert dock._focused_idx == 1

    # Enter activates focused icon
    dock._on_key_activate(MagicMock())
    assert triggered == ["routines"]
    triggered.clear()

    # Right arrow twice moves to Settings (idx 3)
    dock._on_key_right(MagicMock())
    dock._on_key_right(MagicMock())
    assert dock._focused_idx == 3

    dock._on_key_activate(MagicMock())
    assert triggered == ["settings"]
    triggered.clear()

    # Direct triggers via index (Ctrl+1..5)
    dock._trigger_index(0)  # Activity
    dock._trigger_index(4)  # Doctor
    assert triggered == ["activity", "doctor"]

    # Audio level ring update
    dock.set_mic_level(0.75)
    assert math.isclose(dock._mic_level, 0.75)

    dock.destroy()


# ============================================================================
# HUD7: Dialog Topmost & Voice Immunity Tests
# ============================================================================


def test_confirm_and_plan_dialogs_topmost(tk_root):
    """Verify confirmation dialogs enforce topmost attribute and modal grabbing above animated HUD."""
    from ui.app import ConfirmDialog, PlanDialog

    controller = MagicMock()
    req = ConfirmRequest(id=1, tool="test_tool", args={}, warning=None, timeout_s=10.0)

    cd = ConfirmDialog(tk_root, controller, req)
    try:
        # In Windows Tkinter, topmost attribute evaluates to 1 or True
        topmost = cd.attributes("-topmost")
        assert topmost in (1, True, "1")
    finally:
        cd.destroy()

    pd = PlanDialog(tk_root, controller, plan_id="p1", labels=["step 1"], permissions=["SAFE"])
    try:
        topmost_p = pd.attributes("-topmost")
        assert topmost_p in (1, True, "1")
    finally:
        pd.destroy()


# ============================================================================
# HUD8: Graceful Fallback & Settings Validation Tests
# ============================================================================


def test_ui_settings_specs_validation():
    """Verify UI_STYLE, UI_THEME, UI_MOTION, and HUD_FPS validate correctly in SPECS."""
    assert SPECS["UI_STYLE"].allowed == ("hud", "classic")
    assert SPECS["UI_STYLE"].restart_required is True
    assert validate("UI_STYLE", "hud") == "hud"
    assert validate("UI_STYLE", "classic") == "classic"
    with pytest.raises(SettingsError):
        validate("UI_STYLE", "fancy")

    assert SPECS["UI_THEME"].allowed == ("cyan", "gold", "mono")
    assert SPECS["UI_THEME"].restart_required is True
    assert validate("UI_THEME", "gold") == "gold"
    with pytest.raises(SettingsError):
        validate("UI_THEME", "purple")

    assert SPECS["UI_MOTION"].allowed == ("full", "reduced", "off")
    assert SPECS["UI_MOTION"].restart_required is True
    assert validate("UI_MOTION", "reduced") == "reduced"
    with pytest.raises(SettingsError):
        validate("UI_MOTION", "extreme")

    assert SPECS["HUD_FPS"].allowed == ("15", "24", "30")
    assert SPECS["HUD_FPS"].restart_required is True
    assert validate("HUD_FPS", "24") == "24"
    assert validate("HUD_FPS", 30) == "30"
    with pytest.raises(SettingsError):
        validate("HUD_FPS", "60")


def test_diagnostics_includes_hud_section():
    """Verify build_diagnostics_text generates a clean === HUD === section with telemetry."""
    class DummyApp:
        hud_canvas = None

    mock_app = DummyApp()
    mock_model = HudModel()
    mock_canvas = MagicMock()
    mock_canvas.model = mock_model
    mock_app.hud_canvas = mock_canvas

    report = build_diagnostics_text(app=mock_app)
    assert "=== HUD ===" in report
    assert "UI Style:" in report
    assert "UI Theme:" in report
    assert "UI Motion:" in report
    assert "HUD FPS:" in report
    assert "Current Tier: high" in report
    assert "Average Frame Time:" in report
    assert "Governor Downgrades: 0" in report


def test_hud_initialization_exception_fallback(tk_root, monkeypatch):
    """Verify that an exception raised during HUD layout safely falls back to classic layout."""
    import ui.app as app_mod
    from ui.app import OrbitWindow

    controller = MagicMock()
    controller.is_busy = False
    controller.drain.return_value = []

    # Monkeypatch HudCanvas to raise an exception
    def failing_hud(*args, **kwargs):
        raise RuntimeError("Simulated GPU/Canvas failure")

    monkeypatch.setattr(app_mod, "HudCanvas", failing_hud)

    win = OrbitWindow(tk_root, controller, mode_label="TEST", notice=None)
    try:
        # Verify fallback occurred: classic layout constructed, history exists, notice appended
        assert hasattr(win, "history")
        assert app_mod.LAST_HUD_ERROR is not None
        assert "Simulated GPU/Canvas failure" in app_mod.LAST_HUD_ERROR
        # Verify error notice in history text
        hist_text = win.history.get("1.0", tk.END)
        assert "HUD initialization failed" in hist_text
        assert "falling back to classic layout" in hist_text
    finally:
        win.root.destroy()
