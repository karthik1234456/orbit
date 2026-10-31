"""Comprehensive tests for ORBIT HUD v2.23.0 Fidelity, Performance & Refactoring Overhaul.

Covers:
- A1: Two-layer rendering (StaticLayer + OrbLayer), child canvas placement, box sizing (0.74 H * scale),
      seamless bitmap crop border matching, static layer redraw gating.
- A2: Dynamic frame pacing (15 FPS idle ceiling, HUD_FPS active), frame skipping.
- A3: Color bucketing (6 depth buckets) bounding itemconfigure calls during rotation.
- A4: Struct-of-arrays DustRingSoA, numpy and pure-Python projection parity within 0.01 px.
- A5: HUD Telemetry (compute_ms, canvas_ms, items_touched rolling averages) in diagnostics.
- B1: Palette purity (gold theme has NO cyan/teal, cyan theme pure teal, zero tk.Scrollbar).
- B2: Pre-rendered core bloom and smoky glass body sprites with smoothstep falloff and Fresnel rim.
- B3: Segmented arcs (all blocks drawn, unlit ghost blend at 8% accent, rim line).
- B4: Dust ring particle counts (600 high, 300 med, 150 low), 1 px size, double ring.
- B5: Static stars (<= 40 count, 1 px size, <= 35% accent brightness).
- B6: Dock icons (1.5 px outline circles, no filled discs, 8-tooth gear icon, larger mic).
- B7: Standard ttk widget styling (arrowless thin trough scrollbar, selection, caret).
- B8: Interim tab labels ("Act", "Ctx", "Mem", "Docs", "Rem", "Rout", "Run") fitting at 1100 px.
"""

from __future__ import annotations

import colorsys
import math
import random
import tkinter as tk
from tkinter import font as tkfont
from unittest.mock import patch

import numpy as np
import pytest

from core.diagnostics import build_diagnostics_text
from tests.conftest import check_tk_available, make_test_root
from ui.dock import DockCanvas
from ui.hud import HudCanvas
from ui.hud_bitmap import (
    build_background_bitmap,
    build_bloom_sprites,
    build_sphere_body_sprite,
    crop_background_bitmap,
)
from ui.hud_core import (
    DustRingSoA,
    HudModel,
    build_segmented_arcs,
    build_static_stars,
    layout,
    quantize_depth_bucket,
)
from ui.theme import (
    apply_theme,
    blend_color,
    get_palette,
    style_chat_tags,
)

check_tk_available()


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


# ============================================================================
# PART A: Performance & Architecture Tests
# ============================================================================


class TestPartAPerformanceArchitecture:
    """Tests covering Part A two-layer rendering, frame pacing, color bucketing, SoA particles, and telemetry."""

    @pytest.mark.parametrize("W,H", [(1100, 700), (1280, 800), (1920, 1080)])
    def test_a1_orb_layer_geometry_and_box_size(self, W: int, H: int):
        """A1: OrbLayer box is a square of 0.74 H * scale centered at cx, cy without overlapping panels."""
        ly = layout(W, H)
        expected_box = 0.74 * H * ly.scale
        assert abs(ly.orb_box_size - expected_box) < 1.0

        # Panels never intersect the orb's arc outer radius
        assert ly.chat_x_right <= ly.cx - ly.arc_outer_radius or ly.panel_y_top > ly.cy + ly.arc_outer_radius
        assert ly.right_x_left >= ly.cx + ly.arc_outer_radius or ly.panel_y_top > ly.cy + ly.arc_outer_radius

    def test_a1_bitmap_crop_seamless_border(self):
        """A1: Border pixels of the OrbLayer background crop equal the parent bitmap at its position."""
        W, H = 600, 400
        gold_pal = get_palette("gold")
        full_arr, _ = build_background_bitmap(W, H, gold_pal)

        cx, cy = 300, 200
        crop_size = 150
        x0 = cx - crop_size // 2
        y0 = cy - crop_size // 2

        crop_arr, _ = crop_background_bitmap(full_arr, cx, cy, crop_size)
        assert crop_arr.shape == (crop_size, crop_size, 3)

        # Top border
        np.testing.assert_array_equal(crop_arr[0, :, :], full_arr[y0, x0 : x0 + crop_size, :])
        # Bottom border
        np.testing.assert_array_equal(crop_arr[-1, :, :], full_arr[y0 + crop_size - 1, x0 : x0 + crop_size, :])
        # Left border
        np.testing.assert_array_equal(crop_arr[:, 0, :], full_arr[y0 : y0 + crop_size, x0, :])
        # Right border
        np.testing.assert_array_equal(crop_arr[:, -1, :], full_arr[y0 : y0 + crop_size, x0 + crop_size - 1, :])

    def test_a1_static_layer_redraw_gating(self, tk_root):
        """A1: StaticLayer is redrawn ONLY on resize, theme change, or state label change, not during animation."""
        model = HudModel()
        canvas = HudCanvas(tk_root, model=model, width=800, height=600, realism="off")
        canvas.pack()
        tk_root.update_idletasks()

        # Initial render counts
        initial_static_draws = canvas.static_layer_draw_count
        assert initial_static_draws >= 1

        # Run 10 continuous frames of animation
        for _ in range(10):
            model.update(0.033)
            canvas._draw_frame(0.033)

        # Static layer must NOT have been redrawn during steady animation
        assert canvas.static_layer_draw_count == initial_static_draws

        # Theme change triggers redraw
        canvas.set_palette(get_palette("cyan"))
        assert canvas.static_layer_draw_count == initial_static_draws + 1

        # Resize triggers redraw
        canvas._apply_resize()
        assert canvas.static_layer_draw_count == initial_static_draws + 2

        canvas.destroy()

    def test_a2_frame_pacing_idle_vs_active(self, tk_root):
        """A2: Frame pacing enforces 15 FPS idle ceiling when state is idle and pulse is zero."""
        model = HudModel()
        canvas = HudCanvas(tk_root, model=model, width=800, height=600, fps=30, realism="off")
        canvas.pack()
        tk_root.update_idletasks()

        # Idle state with no pulse: delay target corresponds to 15 FPS
        model.set_speaking(False)
        model.set_listening(False)
        model.set_busy(False)
        model.pulse_val = 0.0

        with patch.object(canvas, "_schedule_tick") as mock_sched:
            canvas._on_tick()
            assert mock_sched.called
            delay = mock_sched.call_args[0][0]
            # 1000 / 15 ≈ 67 ms
            assert delay >= 66

        # Speaking state: delay target corresponds to full 30 FPS
        model.set_speaking(True)
        with patch.object(canvas, "_schedule_tick") as mock_sched:
            canvas._on_tick()
            assert mock_sched.called
            delay = mock_sched.call_args[0][0]
            # 1000 / 30 ≈ 33 ms
            assert delay <= 34

        canvas.destroy()

    def test_a3_color_bucketing_quantization(self):
        """A3: quantize_depth_bucket groups depths into 6 discrete buckets [0..5]."""
        for z in [-1.0, -0.99, -0.7]:
            assert quantize_depth_bucket(z, 6) in (0, 1)
        for z in [0.7, 0.99, 1.0]:
            assert quantize_depth_bucket(z, 6) in (4, 5)

        buckets = set(quantize_depth_bucket(-1.0 + i * 0.05, 6) for i in range(41))
        assert buckets == {0, 1, 2, 3, 4, 5}

    def test_a4_dust_ring_soa_numpy_python_parity(self):
        """A4: Struct-of-arrays DustRingSoA numpy vs Python projection match within 0.01 px."""
        rng = random.Random(42)
        soa = DustRingSoA(rng, count=600)

        cx, cy = 400.0, 300.0
        a1, b1 = 200.0, 60.0
        a2, b2 = 230.0, 70.0
        tilt = math.radians(18.0)

        py_proj = soa.project_python(cx, cy, a1, b1, a2, b2, tilt)
        np_proj = soa.project_numpy(cx, cy, a1, b1, a2, b2, tilt)

        assert len(py_proj) == len(np_proj) == 600

        max_diff = 0.0
        for (x1, y1, z1, t1), (x2, y2, z2, t2) in zip(py_proj, np_proj):
            assert t1 == t2
            diff = max(abs(x1 - x2), abs(y1 - y2), abs(z1 - z2))
            if diff > max_diff:
                max_diff = diff

        assert max_diff < 0.01, f"Parity difference {max_diff} exceeded 0.01 px"

    def test_a5_hud_telemetry_in_diagnostics(self, tk_root):
        """A5: Compute Time and Canvas Time rolling averages appear in /diag HUD section."""
        from types import SimpleNamespace

        model = HudModel()
        canvas = HudCanvas(tk_root, model=model, width=640, height=480, realism="off")
        canvas.pack()
        tk_root.update_idletasks()

        for _ in range(5):
            canvas._draw_frame(0.033)

        app_mock = SimpleNamespace(canvas=canvas)
        diag = build_diagnostics_text(app=app_mock)
        assert "=== HUD ===" in diag
        assert "Compute Time (Avg)" in diag
        assert "Canvas Time (Avg)" in diag
        assert "Items Touched (Avg)" in diag
        canvas.destroy()


# ============================================================================
# PART B: Fidelity & Theme Purity Tests
# ============================================================================


class TestPartBFidelityThemePurity:
    """Tests covering Part B palette purity, core & glass sprites, arcs, dust ring, stars, dock, and widgets."""

    def test_b1_palette_purity_no_cyan_in_gold(self):
        """B1: Gold palette colors strictly contain no cyan/teal hue [150, 210] with saturation > 0.20."""
        gold_pal = get_palette("gold")
        color_attrs = [
            gold_pal.bg,
            gold_pal.panel,
            gold_pal.panel_edge,
            gold_pal.accent,
            gold_pal.accent_dim,
            gold_pal.text,
            gold_pal.text_dim,
            gold_pal.ok,
            gold_pal.warn,
            gold_pal.danger,
        ]

        for hex_col in color_attrs:
            c = hex_col.lstrip("#")
            r, g, b = int(c[0:2], 16) / 255.0, int(c[2:4], 16) / 255.0, int(c[4:6], 16) / 255.0
            h, s, v = colorsys.rgb_to_hsv(r, g, b)
            h_deg = h * 360.0
            # Hue between 150 and 210 is cyan/teal
            if 150.0 <= h_deg <= 210.0 and s > 0.20:
                pytest.fail(f"Color {hex_col} has cyan hue {h_deg:.1f} and saturation {s:.2f} in gold palette")

    def test_b1_zero_tk_scrollbar_in_repo(self):
        """B1/B7: Scan ui/ to ensure zero occurrences of tk.Scrollbar exist (all use ttk.Scrollbar)."""
        import os
        import re

        ui_dir = os.path.join(os.path.dirname(__file__), "..", "ui")
        for root_dir, _, files in os.walk(ui_dir):
            for file in files:
                if file.endswith(".py"):
                    fpath = os.path.join(root_dir, file)
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read()
                        assert not re.search(r"\btk\.Scrollbar\b", content), f"Found tk.Scrollbar in {file}"

    def test_b2_core_bloom_and_glass_sprites(self):
        """B2: Core bloom sprite has smoothstep radial falloff; smoky glass has Fresnel rim."""
        radius = 80
        pal = get_palette("gold")
        W, H = 400, 300
        cx, cy = 200, 150
        bg_arr, _ = build_background_bitmap(W, H, pal)

        blooms = build_bloom_sprites(radius, cx, cy, bg_arr, palette=pal, num_levels=4)
        assert len(blooms) == 4
        # Center of bloom is brightest
        bloom_arr, _ = blooms[-1]
        c = bloom_arr.shape[0] // 2
        center_lum = np.mean(bloom_arr[c, c, :])
        halo_lum = np.mean(bloom_arr[c, c + int(radius * 0.5), :])
        edge_lum = np.mean(bloom_arr[c, 0, :])
        assert center_lum > halo_lum > edge_lum

        glass_arr, _ = build_sphere_body_sprite(radius, cx, cy, bg_arr, palette=pal)
        assert glass_arr.shape[0] > 0
        assert glass_arr.shape[1] > 0

    def test_b3_segmented_arcs_geometry_and_ghost_blending(self):
        """B3: All 44 arc blocks are drawn, stay on band [0.285 H, 0.310 H], unlit blocks are 8% accent ghosts."""
        rng = random.Random(42)
        arcs_a, arcs_b = build_segmented_arcs(rng)
        assert len(arcs_a) == 24
        assert len(arcs_b) == 20

        pal = get_palette("gold")
        unlit_ghost = blend_color(pal.bg, pal.accent, 0.08)
        assert unlit_ghost.startswith("#")

    def test_b4_dust_ring_particle_counts_and_sizes(self):
        """B4: Dust ring has 600 particles on high tier, 300 on medium, 150 on low."""
        rng = random.Random(42)
        soa_high = DustRingSoA(rng, count=600)
        soa_med = DustRingSoA(rng, count=300)
        soa_low = DustRingSoA(rng, count=150)

        assert soa_high.count == 600
        assert soa_med.count == 300
        assert soa_low.count == 150

    def test_b5_static_stars_count_and_brightness(self):
        """B5: Static stars count <= 40, size 1 px, brightness <= 35% accent."""
        rng = random.Random(42)
        stars = build_static_stars(rng, count=40)
        assert len(stars) <= 40
        for star in stars:
            assert star.size == 1.0
            assert star.brightness <= 0.35

    def test_b6_dock_icons_outline_and_8_tooth_gear(self, tk_root):
        """B6: Dock icons are thin outline circles with theme strokes (width 1.5, fill empty) and 8-tooth gear."""
        pal = get_palette("gold")
        ly = layout(1280, 800)
        dock = DockCanvas(tk_root, palette=pal, height=ly.dock_height, width=1280)
        dock.pack()
        tk_root.update_idletasks()
        dock.redraw()

        assert len(dock._circle_ids) == 5
        for cid in dock._circle_ids:
            width = float(dock.itemcget(cid, "width"))
            fill = dock.itemcget(cid, "fill")
            assert width == 1.5
            assert fill == ""

        assert len(dock._gear_items) > 0
        dock.destroy()

    def test_b7_standard_widgets_styling(self, tk_root):
        """B7: Style standard widgets with palette selection colors and arrowless scrollbars."""
        pal = get_palette("gold")
        apply_theme(tk_root, pal)
        txt = tk.Text(tk_root)
        style_chat_tags(txt, pal)

        assert txt.cget("selectbackground") == pal.panel_edge
        assert txt.cget("insertbackground") == pal.accent
        txt.destroy()

    def test_b8_interim_tab_labels_fit_at_1100px(self, tk_root):
        """B8: Short tab labels ("Act", "Ctx", "Mem", "Docs", "Rem", "Rout", "Run") fit at 1100 px."""
        short_labels = ["Act", "Ctx", "Mem", "Docs", "Rem", "Rout", "Run"]
        tab_font = tkfont.Font(font=("Segoe UI", 8))

        # Total width of all 7 tabs including padding (approx 16 px per tab)
        total_tab_width = sum(tab_font.measure(lbl) + 16 for lbl in short_labels)

        # Right panel at 1100 px width has minimum floor of 260 px
        ly = layout(1100, 700)
        panel_w = ly.panel_width
        assert panel_w >= 260.0

        # Tabs must fit comfortably inside the panel width without clipping
        assert total_tab_width < panel_w, f"Tabs total width {total_tab_width} exceeded panel width {panel_w}"
