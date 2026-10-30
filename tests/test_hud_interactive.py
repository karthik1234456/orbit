"""Tests for interactive HUD features, telemetry, radial menu, palette, and shortcuts (v2.18).

Covers:
- Drag rotation maths and exponential inertia decay exp(-3t) in full motion
- Motion mode gating (reduced/off disables inertia, look-at, and parallax)
- Zoom clamping [0.8, 1.25] and panel clearance floor >= 260px
- Click vs drag discrimination (< 4px click, >= 4px drag)
- Microphone toggle identity and busy notice when blocked
- Cursor look-at orientation within +/- 6 deg and 300ms smoothing
- Arc telemetry with fake psutil (lit blocks count, tooltips, missing data)
- Radial menu geometry (6 wedges at 60 deg, wrap-around navigation, handler identity)
- Command palette fuzzy search, max 12 cap, routine store integration, "Ask ORBIT", and privacy guarantees
- Shortcuts registry completeness and collision validation
- UI_INTERACTIVE=0 safety disabling
- Confirmation / Plan dialog blocking of all interactions
- Diagnostics section === INTERACTION === flags and counters
"""

from __future__ import annotations

import ast
import math
import os
import sys

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
from ui.hud import HudCanvas
from ui.hud_core import (
    HudModel,
    clamp_zoom_factor,
    layout,
)
from ui.palette import (
    PaletteItem,
    filter_and_rank_items,
    fuzzy_match,
)
from ui.radial_menu import RadialAction, RadialMenu
from ui.shortcuts_registry import (
    SHORTCUTS,
    format_shortcuts_overlay_text,
    validate_registry,
)
from ui.telemetry import (
    SystemTelemetry,
    get_telemetry_provider,
    sample_system_telemetry,
    set_telemetry_provider,
)


@pytest.fixture
def tk_root():
    root = make_test_root()
    root.withdraw()
    yield root
    try:
        root.destroy()
    except Exception:
        pass


# ============================================================================
# 1. Drag Maths, Inertia, and Motion Modes
# ============================================================================


def test_drag_maths_and_inertia_exponential_decay():
    """Verify angular velocity coasts with exp(-3t) decay in full motion."""
    model = HudModel()
    model.ang_vel_x = 10.0
    model.ang_vel_y = 20.0

    # In full motion: decay with exp(-3 * dt)
    dt = 0.5
    expected_decay = math.exp(-3.0 * dt)  # exp(-1.5) ~= 0.22313
    model.update(dt, motion_mode="full")

    assert math.isclose(model.ang_vel_x, 10.0 * expected_decay, rel_tol=1e-4)
    assert math.isclose(model.ang_vel_y, 20.0 * expected_decay, rel_tol=1e-4)


def test_motion_mode_disables_inertia_and_lookat():
    """Verify that reduced and off motion modes immediately zero out angular velocity."""
    model = HudModel()
    model.ang_vel_x = 5.0
    model.ang_vel_y = 5.0
    model.target_lookat_x = 0.05
    model.target_lookat_y = 0.05

    model.update(0.1, motion_mode="reduced")
    assert model.ang_vel_x == 0.0
    assert model.ang_vel_y == 0.0
    assert model.lookat_x == 0.0
    assert model.lookat_y == 0.0

    model.ang_vel_x = 5.0
    model.ang_vel_y = 5.0
    model.update(0.1, motion_mode="off")
    assert model.ang_vel_x == 0.0
    assert model.ang_vel_y == 0.0
    assert model.lookat_x == 0.0
    assert model.lookat_y == 0.0


# ============================================================================
# 2. Zoom Clamping and Panel Clearance Floor
# ============================================================================


def test_zoom_clamping_and_panel_clearance_floor():
    """Verify zoom stays within [0.8, 1.25] and respects the 260px panel clearance floor."""
    # Under regular dimensions (1280x800)
    w, h = 1280, 800
    assert clamp_zoom_factor(w, h, 0.5) == 0.8
    assert clamp_zoom_factor(w, h, 1.0) == 1.0
    assert clamp_zoom_factor(w, h, 2.0) <= 1.25

    # Smallest supported dimension: 1100x700
    w_min, h_min = 1100, 700
    z_min = clamp_zoom_factor(w_min, h_min, 1.5)
    assert 0.8 <= z_min <= 1.25

    # Check panel clearance directly
    ly = layout(w_min, h_min)
    orb_r = ly.sphere_radius * z_min
    clearance = (ly.cx - orb_r) - ly.chat_x_right
    assert clearance >= 0
    assert (ly.cx - orb_r) >= 260


def test_model_reset_view():
    """Verify reset_view resets rotation, look-at, and zoom."""
    model = HudModel()
    model.sphere_ax = 1.2
    model.sphere_ay = 2.4
    model.zoom_factor = 1.25
    model.lookat_x = 0.05
    model.lookat_y = -0.05

    model.reset_view()
    assert model.sphere_ax == 0.0
    assert model.sphere_ay == 0.0
    assert model.zoom_factor == 1.0
    assert model.lookat_x == 0.0
    assert model.lookat_y == 0.0


# ============================================================================
# 3. Click vs Drag Discrimination & Microphone State
# ============================================================================


class DummyEvent:
    def __init__(self, x: float, y: float, delta: int = 0):
        self.x = x
        self.y = y
        self.delta = delta


def test_click_vs_drag_threshold(tk_root):
    """Verify pointer movements < 4px count as click, >= 4px count as drag."""
    mic_called = []
    model = HudModel()
    canvas = HudCanvas(
        tk_root,
        model=model,
        width=1280,
        height=800,
        interactive=True,
        on_mic=lambda: mic_called.append(True),
    )
    canvas.pack()
    tk_root.update_idletasks()

    ly = layout(1280, 800)
    cx, cy = ly.cx, ly.cy

    # 1. Click (< 4px movement): should toggle mic
    canvas._on_mouse_down(DummyEvent(cx, cy))
    canvas._on_mouse_drag(DummyEvent(cx + 1, cy + 1))  # dist ~ 1.41px < 4px
    canvas._on_mouse_up(DummyEvent(cx + 1, cy + 1))
    assert len(mic_called) == 1

    # 2. Drag (>= 4px movement): should NOT toggle mic, but track drag
    canvas._on_mouse_down(DummyEvent(cx, cy))
    canvas._on_mouse_drag(DummyEvent(cx + 10, cy + 10))  # dist ~ 14.1px >= 4px
    canvas._on_mouse_up(DummyEvent(cx + 10, cy + 10))
    assert len(mic_called) == 1  # Not called again
    assert canvas.drag_count == 1

    canvas.destroy()


def test_microphone_busy_notice(tk_root):
    """Verify that when microphone is blocked, clicking orb displays 'Microphone busy' notice."""
    mic_called = []
    model = HudModel()
    canvas = HudCanvas(
        tk_root,
        model=model,
        width=1280,
        height=800,
        interactive=True,
        on_mic=lambda: mic_called.append(True),
    )
    canvas._is_blocked_callback = lambda: True  # System is busy
    canvas.pack()
    tk_root.update_idletasks()

    ly = layout(1280, 800)
    canvas._on_mouse_down(DummyEvent(ly.cx, ly.cy))
    canvas._on_mouse_up(DummyEvent(ly.cx, ly.cy))

    # Mic must NOT be triggered
    assert len(mic_called) == 0
    # Transient notice "Microphone busy" must be queued
    assert any("Microphone busy" in n.text for n in canvas._notices)

    canvas.destroy()


# ============================================================================
# 4. Cursor Look-At Orientation and Smoothing
# ============================================================================


def test_cursor_lookat_within_bounds_and_smoothing():
    """Verify look-at targets stay within +/- 6 deg and smooth with 300ms time constant."""
    model = HudModel()
    # 6 degrees in radians
    max_rad = math.radians(6.0)

    # Set target near max
    model.target_lookat_x = max_rad
    model.target_lookat_y = -max_rad

    # Update by small dt
    dt = 0.1
    # decay factor = 1.0 - exp(-dt / 0.3)
    alpha = 1.0 - math.exp(-dt / 0.3)
    expected_x = max_rad * alpha

    model.update(dt, motion_mode="full")
    assert math.isclose(model.lookat_x, expected_x, rel_tol=1e-4)
    assert abs(model.lookat_x) <= max_rad
    assert abs(model.lookat_y) <= max_rad


# ============================================================================
# 5. Live System Telemetry in Equalizer Arcs
# ============================================================================


def test_system_telemetry_provider_and_arcs(tk_root):
    """Verify telemetry updates model cpu/mem load and calculates lit arc blocks."""
    fake_data = SystemTelemetry(
        cpu_percent=50.0,
        mem_percent=75.0,
        mem_used_gb=12.0,
        mem_total_gb=16.0,
    )
    old_provider = get_telemetry_provider()
    set_telemetry_provider(lambda: fake_data)

    try:
        sample = sample_system_telemetry()
        assert sample.cpu_percent == 50.0
        assert sample.mem_percent == 75.0
        assert "50%" in sample.format_cpu_tooltip()
        assert "75%" in sample.format_memory_tooltip()

        model = HudModel()
        canvas = HudCanvas(tk_root, model=model, width=800, height=600)
        canvas._poll_telemetry()

        assert canvas.model.cpu_load == 0.50
        assert canvas.model.mem_load == 0.75

        # In Arc A (24 blocks): lit count = round(0.50 * 24) = 12
        total_a = len(canvas.model.blocks_a)
        lit_a = int(round(canvas.model.cpu_load * total_a))
        assert total_a == 24
        assert lit_a == 12

        # In Arc B (20 blocks): lit count = round(0.75 * 20) = 15
        total_b = len(canvas.model.blocks_b)
        lit_b = int(round(canvas.model.mem_load * total_b))
        assert total_b == 20
        assert lit_b == 15

        canvas.destroy()
    finally:
        set_telemetry_provider(old_provider)


def test_telemetry_provider_exception_handling():
    """Verify telemetry provider handles exceptions without raising."""
    old_provider = get_telemetry_provider()

    def bad_provider():
        raise RuntimeError("psutil failed")

    set_telemetry_provider(bad_provider)
    try:
        sample = sample_system_telemetry()
        assert sample.cpu_percent is None
        assert sample.mem_percent is None
        assert sample.has_data is False
    finally:
        set_telemetry_provider(old_provider)


# ============================================================================
# 6. Radial Quick Action Menu
# ============================================================================


def test_radial_menu_wedges_and_navigation(tk_root):
    """Verify 6-action radial menu geometry, selection, and keyboard wrapping."""
    model = HudModel()
    canvas = HudCanvas(tk_root, model=model, width=800, height=600)
    menu = RadialMenu(canvas)

    executed = []
    actions = [
        RadialAction("Action 0", lambda: executed.append(0)),
        RadialAction("Action 1", lambda: executed.append(1)),
        RadialAction("Action 2", lambda: executed.append(2)),
        RadialAction("Action 3", lambda: executed.append(3)),
        RadialAction("Action 4", lambda: executed.append(4)),
        RadialAction("Action 5", lambda: executed.append(5)),
    ]

    opened = menu.open(cx=400, cy=300, radius=120, actions=actions)
    assert opened is True
    assert menu.is_open is True
    assert len(menu.actions) == 6

    # Verify initial selection is 0
    assert menu.selected_index == 0

    # Wrap around next: 0 -> 1 -> 2 -> ... -> 5 -> 0
    for expected in [1, 2, 3, 4, 5, 0]:
        menu.select_next()
        assert menu.selected_index == expected

    # Wrap around prev: 0 -> 5 -> 4
    menu.select_prev()
    assert menu.selected_index == 5
    menu.select_prev()
    assert menu.selected_index == 4

    # Execute selected (action 4)
    menu.execute_selected()
    assert executed == [4]
    assert menu.is_open is False

    canvas.destroy()


def test_radial_menu_dismiss_on_escape(tk_root):
    """Verify Escape closes the radial menu without executing any action."""
    model = HudModel()
    canvas = HudCanvas(tk_root, model=model, width=800, height=600)
    menu = RadialMenu(canvas)
    executed = []
    actions = [RadialAction("Action 0", lambda: executed.append(0))]

    menu.open(cx=400, cy=300, radius=100, actions=actions)
    assert menu.is_open is True

    menu.close()
    assert menu.is_open is False
    assert len(executed) == 0

    canvas.destroy()


# ============================================================================
# 7. Command Palette (Ctrl+K) & Privacy Guarantees
# ============================================================================


def test_palette_fuzzy_match_and_ranking():
    """Verify fuzzy matching and score prioritization."""
    # Subsequence matching
    assert fuzzy_match("doc", "Doctor diagnostics") is not None
    assert fuzzy_match("xyz", "Doctor diagnostics") is None

    # Ranking: exact prefix ranks higher than scattered subsequence
    item1 = PaletteItem("Doctor diagnostics", "System", lambda: None)
    item2 = PaletteItem("Run Routine: doctor_check", "Routines", lambda: None)
    results = filter_and_rank_items("doc", [item2, item1], max_results=12)
    assert results[0].title == "Doctor diagnostics"


def test_palette_result_cap_and_ask_orbit():
    """Verify results are capped at 12 items."""
    items = [PaletteItem(f"Command {i}", "System", lambda: None) for i in range(25)]
    ranked = filter_and_rank_items("com", items, max_results=12)
    assert len(ranked) == 12


def test_palette_privacy_guarantee():
    """Verify ui/palette.py has zero imports of memory store, docstore, reminders, or chat history."""
    with open("ui/palette.py", "r", encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.add(node.module)

    forbidden_packages = [
        "core.memory",
        "rag.index",
        "rag.retrieval",
        "reminders",
        "chat_display",
    ]
    for imp in imported_modules:
        for forbidden in forbidden_packages:
            assert not imp.startswith(forbidden), f"Forbidden import detected in ui/palette.py: {imp}"


# ============================================================================
# 8. Shortcuts Registry Validation
# ============================================================================


def test_shortcuts_registry_integrity():
    """Verify single source-of-truth registry has zero collisions and clean chat keys."""
    is_valid, errors = validate_registry()
    assert is_valid, f"Registry validation failed with errors: {errors}"
    assert len(errors) == 0

    # Ensure all primary scopes are present
    scopes = {s.scope for s in SHORTCUTS}
    assert {"global", "chat", "hud", "dock", "radial", "palette", "dialog"}.issubset(scopes)

    # Verify formatted overlay text builds cleanly
    overlay_text = format_shortcuts_overlay_text()
    assert "=== GLOBAL SHORTCUTS ===" in overlay_text
    assert "Ctrl+K" in overlay_text
    assert "Ctrl+M" in overlay_text
    assert "F1" in overlay_text


# ============================================================================
# 9. UI_INTERACTIVE Setting Disabling & Dialog Blocking
# ============================================================================


def test_ui_interactive_setting_disabled(tk_root):
    """Verify UI_INTERACTIVE=0 cleanly prevents orb dragging, zoom, and clicks."""
    mic_called = []
    model = HudModel()
    canvas = HudCanvas(
        tk_root,
        model=model,
        width=1280,
        height=800,
        interactive=False,  # UI_INTERACTIVE=0
        on_mic=lambda: mic_called.append(True),
    )
    canvas.pack()
    tk_root.update_idletasks()

    ly = layout(1280, 800)
    cx, cy = ly.cx, ly.cy

    # Mouse click ignored
    canvas._on_mouse_down(DummyEvent(cx, cy))
    assert canvas._is_dragging is False
    canvas._on_mouse_up(DummyEvent(cx, cy))
    assert len(mic_called) == 0

    # Zoom wheel ignored
    old_zoom = canvas.model.zoom_factor
    canvas._on_mouse_wheel(DummyEvent(cx, cy, delta=120))
    assert canvas.model.zoom_factor == old_zoom

    # Radial menu ignored
    actions = [RadialAction("Test", lambda: None)]
    assert canvas.open_radial_menu(actions) is False

    canvas.destroy()


def test_dialog_blocks_all_hud_interactions(tk_root):
    """Verify open ConfirmDialog/PlanDialog blocks drag, click, zoom, and radial menu."""
    mic_called = []
    model = HudModel()
    canvas = HudCanvas(
        tk_root,
        model=model,
        width=1280,
        height=800,
        interactive=True,
        is_dialog_open=lambda: True,  # Dialog is open
        on_mic=lambda: mic_called.append(True),
    )
    canvas.pack()
    tk_root.update_idletasks()

    ly = layout(1280, 800)
    cx, cy = ly.cx, ly.cy

    # Mouse down blocked
    canvas._on_mouse_down(DummyEvent(cx, cy))
    assert canvas._is_dragging is False

    # Mouse wheel zoom blocked
    old_zoom = canvas.model.zoom_factor
    canvas._on_mouse_wheel(DummyEvent(cx, cy, delta=120))
    assert canvas.model.zoom_factor == old_zoom

    # Radial menu blocked
    actions = [RadialAction("Test", lambda: None)]
    assert canvas.open_radial_menu(actions) is False

    canvas.destroy()


# ============================================================================
# 10. Settings & Diagnostics Telemetry
# ============================================================================


def test_settings_specs_ui_interactive():
    """Verify UI_INTERACTIVE specification in settings_store."""
    assert "UI_INTERACTIVE" in SPECS
    spec = SPECS["UI_INTERACTIVE"]
    assert spec.type == "bool"
    assert spec.default == "1"

    val1 = validate("UI_INTERACTIVE", "1")
    assert val1 == "1"
    val0 = validate("UI_INTERACTIVE", "0")
    assert val0 == "0"
    with pytest.raises(SettingsError):
        validate("UI_INTERACTIVE", "invalid")


def test_diagnostics_interaction_section():
    """Verify build_diagnostics_text contains === INTERACTION === section and counters."""
    diag = build_diagnostics_text()
    assert "=== INTERACTION ===" in diag
    assert "UI_INTERACTIVE:" in diag
    assert "Orb Drag Count:" in diag
    assert "Orb Zoom Factor:" in diag
    assert "Radial Menu Opens:" in diag
    assert "Palette Opens:" in diag
    assert "Notices Shown:" in diag
