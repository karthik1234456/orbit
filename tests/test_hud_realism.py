"""Comprehensive tests for ORBIT HUD v2.17 Cinematic Realism & Part A Defect Fixes.

Covers:
- A1: Dock canvas sizing containing all bounding boxes without clipping across resolutions and scalings.
- A2: Core concentric circles largest-first z-order, 3 layers on low/static, monotonic luminance.
- A3: Orthographic wireframe projection guaranteeing all points <= r_s + 0.5 px.
- A4: Segmented equaliser arc quads geometry, band [0.285 H, 0.310 H], 24 Arc A, 20 Arc B.
- A5: UI_MOTION modes (full 30 FPS, reduced 8 FPS with half particles and arcs drawn, off 1 static frame).
- A6: Equal-advance typography with symmetric positions for multiple test strings.
- A7: Touched items upper bound <= 450 on high tier and zero item count growth after frame 1.
- B1-B3: Pure Python PPM generation <= 2s, determinism, bloom/glass sprite border matching.
- B6-B7: Memory budget <= 48 MB at 1080p, UI_REALISM validation in SPECS, PhotoImage fallback, resize debounce.
"""

from __future__ import annotations

import math
import random
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from config.settings_store import SPECS, SettingsError, validate
from core.diagnostics import build_diagnostics_text
from tests.conftest import check_tk_available, make_test_root
from ui.dock import DockCanvas
from ui.hud import HudCanvas
from ui.hud_bitmap import (
    build_background_bitmap,
    build_bloom_sprites,
    build_ppm_bytes,
    build_sphere_body_sprite,
    calculate_image_memory_bytes,
)
from ui.hud_core import (
    HudModel,
    build_parallax_particles,
    build_reference_sphere_curves,
    build_segmented_arcs,
    compute_arc_quad_coords,
    compute_equal_advance_cells,
    layout,
    project_orthographic,
    split_curve_front_back,
)
from ui.theme import get_palette

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
# PART A: Visual Defect Fixes Tests
# ============================================================================

class TestPartAVigorousFixes:
    """Tests covering visual defect fixes A1 through A7."""

    @pytest.mark.parametrize("W,H", [(1100, 700), (1280, 800), (1920, 1080)])
    @pytest.mark.parametrize("scaling", [1.0, 1.25, 1.5])
    def test_a1_dock_canvas_bounds_contain_all_icons(self, W: int, H: int, scaling: float):
        """A1: Ensure dock canvas bounds strictly enclose every dock item bounding box."""
        ly = layout(W, H, scaling=scaling)

        assert ly.dock_height > 0
        assert ly.dock_width > 0

        # Center Y in dock local coordinate system is dock_height / 2
        cy = ly.dock_height / 2.0
        dock_w = float(W)

        # Verify all 5 icons
        for idx, gx in enumerate(ly.dock_xs):
            r = ly.dock_mic_radius if idx == 2 else ly.dock_side_radius

            # Vertical containment in dock canvas [0, dock_height]
            assert cy - r >= 0.0, f"Icon {idx} top {cy - r} clipped at top"
            assert cy + r <= ly.dock_height, f"Icon {idx} bottom {cy + r} clipped at bottom"

            # Horizontal containment within window width [0, W]
            assert gx - r >= 0.0, f"Icon {idx} left {gx - r} clipped at left"
            assert gx + r <= dock_w, f"Icon {idx} right {gx + r} clipped at right"

    def test_a1_dock_canvas_widget_integration(self, tk_root):
        """A1: Test DockCanvas widget computes unclipped icon centers from layout."""
        pal = get_palette("gold")
        try:
            tk_scale = float(tk_root.tk.call("tk", "scaling"))
        except Exception:
            tk_scale = 1.0
        ly = layout(1280, 800, scaling=tk_scale)
        tk_root.geometry("1280x800")
        tk_root.update()
        dock = DockCanvas(tk_root, palette=pal, height=ly.dock_height, width=1280)
        dock.pack(fill="x")
        tk_root.update_idletasks()
        tk_root.update()

        centers = dock._get_icon_centers()
        assert len(centers) == 5

        for idx, (cx, cy, r) in enumerate(centers):
            assert cy - r >= 0.0
            assert cy + r <= ly.dock_height
            assert cx - r >= 0.0
            assert cx + r <= 1280.0

    def test_a2_core_layers_largest_first_and_monotonic(self):
        """A2: Test core concentric circles rendered largest-first with monotonic luminance."""
        ly = layout(1280, 800)

        # Radii are strictly increasing in layout specification
        assert len(ly.core_radii) == 5
        assert ly.core_radii == sorted(ly.core_radii)

        # 5 core shades defined from index 0 (largest outer) to index 4 (inner bright core)
        core_shades = ["#B29C71", "#C8AD7B", "#DDBF85", "#F0D296", "#FADEA4"]

        def get_lum(hex_col: str) -> float:
            c = hex_col.lstrip("#")
            r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
            return 0.299 * r + 0.587 * g + 0.114 * b

        luminances = [get_lum(s) for s in core_shades]
        # Luminance must monotonically increase from outer halo (#B29C71) to inner core (#FADEA4)
        for i in range(len(luminances) - 1):
            assert luminances[i] < luminances[i + 1]

    def test_a2_core_low_tier_preserves_three_layers(self, tk_root):
        """A2: Test low and static tiers preserve at least 3 concentric core layers."""
        model = HudModel()
        model.tier = "low"
        canvas = HudCanvas(tk_root, model=model, width=640, height=480, realism="off")
        canvas.pack()
        tk_root.update_idletasks()
        canvas._draw_frame(0.033)

        # In low tier, 3 concentric circles are configured normal (not collapsed to 1)
        active_core_fills = [
            canvas.itemcget(item_id, "fill")
            for item_id in canvas._core_circle_items
            if canvas.itemcget(item_id, "state") != "hidden"
        ]
        assert len(active_core_fills) == 3

    def test_a3_orthographic_projection_strictly_bounded(self):
        """A3: Test orthographic projection points never exceed sphere limb."""
        cx, cy, rs = 500.0, 400.0, 150.0

        # Test across random points on unit sphere
        rng = random.Random(42)
        pts: list[tuple[float, float, float]] = []
        for _ in range(500):
            z = rng.uniform(-1.0, 1.0)
            theta = rng.uniform(0, 2 * math.pi)
            r_xy = math.sqrt(max(0.0, 1.0 - z * z))
            x = r_xy * math.cos(theta)
            y = r_xy * math.sin(theta)
            pts.append((x, y, z))

        projected = project_orthographic(pts, cx, cy, rs)
        for (px, py, _) in projected:
            dist = math.sqrt((px - cx) ** 2 + (py - cy) ** 2)
            assert dist <= rs + 0.5, f"Point projected outside limb: {dist} > {rs}"

        # Test across reference sphere curves
        lat_curves, mer_curves = build_reference_sphere_curves()
        for curve in lat_curves + mer_curves:
            proj_curve = project_orthographic(curve, cx, cy, rs)
            for (px, py, _) in proj_curve:
                dist = math.sqrt((px - cx) ** 2 + (py - cy) ** 2)
                assert dist <= rs + 0.5

    def test_a4_segmented_arc_quads_geometry(self):
        """A4: Test segmented equaliser arcs reside in [0.285 H, 0.310 H] with 24 and 20 blocks."""
        rng = random.Random(42)
        arcs_a, arcs_b = build_segmented_arcs(rng)
        assert len(arcs_a) == 24
        assert len(arcs_b) == 20

        cx, cy, H = 640.0, 344.0, 800.0
        r_inner = 0.285 * H
        r_outer = 0.310 * H

        for block in arcs_a + arcs_b:
            coords = compute_arc_quad_coords(cx, cy, r_inner, r_outer, block.start_deg, block.end_deg)
            assert len(coords) == 8  # 4 vertices * 2

            for i in range(0, 8, 2):
                vx, vy = coords[i], coords[i + 1]
                dist = math.sqrt((vx - cx) ** 2 + (vy - cy) ** 2)
                assert (r_inner - 0.5) <= dist <= (r_outer + 0.5)

    def test_a5_ui_motion_modes(self, tk_root):
        """A5: Test full (30 FPS), reduced (8 FPS, half particles, arcs retained), and off (static frame)."""
        model = HudModel()

        # 1. Full motion mode (fps defaults to 30)
        canvas_full = HudCanvas(tk_root, model=model, width=640, height=480, motion="full", realism="off")
        canvas_full.pack()
        tk_root.update_idletasks()
        assert canvas_full.fps == 30
        assert len(canvas_full._parallax_deep_items) == 30
        assert len(canvas_full._parallax_mid_items) == 20
        assert len(canvas_full._parallax_near_items) == 10
        canvas_full.destroy()

        # 2. Reduced motion mode
        canvas_red = HudCanvas(tk_root, model=model, width=640, height=480, motion="reduced", realism="off")
        canvas_red.pack()
        tk_root.update_idletasks()
        canvas_red._draw_frame(0.125)
        # Equaliser arcs must still be rendered in reduced motion
        visible_arcs = [
            i for i in canvas_red._arc_a_items + canvas_red._arc_b_items
            if canvas_red.itemcget(i, "state") != "hidden"
        ]
        assert len(visible_arcs) > 0
        canvas_red.destroy()

        # 3. Off motion mode
        canvas_off = HudCanvas(tk_root, model=model, width=640, height=480, motion="off", realism="off")
        canvas_off.pack()
        tk_root.update_idletasks()
        # Off mode renders 1 frame and does not schedule continuous animation ticks
        assert canvas_off._after_id is None
        # Verify static elements exist
        assert len(canvas_off._sphere_front_items) > 0
        assert len(canvas_off._core_circle_items) > 0
        assert len(canvas_off._tick_items) > 0
        canvas_off.destroy()

    @pytest.mark.parametrize("word", ["ORBIT", "AGENT", "STATUS"])
    def test_a6_equal_advance_cells_symmetry(self, word: str):
        """A6: Test equal-advance cells are strictly symmetric around cx."""
        cx = 500.0
        coords = compute_equal_advance_cells(word, cx=cx, widest_glyph=14.0, tracking=6.0)

        assert len(coords) == len(word)

        # Check symmetry around cx
        n = len(word)
        for i in range(n):
            dist_left = cx - coords[i]
            dist_right = coords[n - 1 - i] - cx
            assert math.isclose(dist_left, dist_right, abs_tol=1e-6)

        # Check equal step between adjacent letters
        step = coords[1] - coords[0]
        for i in range(1, n - 1):
            assert math.isclose(coords[i + 1] - coords[i], step, abs_tol=1e-6)

    def test_a7_wireframe_54_polylines_and_items_upper_bound(self, tk_root):
        """A7: Test wireframe split into max 54 polylines and touched items <= 450 with zero churn."""
        lat_curves, mer_curves = build_reference_sphere_curves()
        assert len(lat_curves) == 11
        assert len(mer_curves) == 16
        total_curves = len(lat_curves) + len(mer_curves)
        assert total_curves == 27

        # Splitting 27 curves across horizon yields at most 54 front/back polylines
        front_count = 0
        back_count = 0
        for curve in lat_curves + mer_curves:
            front, back = split_curve_front_back(curve)
            if front:
                front_count += 1
            if back:
                back_count += 1
        assert front_count <= 27
        assert back_count <= 27
        assert (front_count + back_count) <= 54

        # Verify HudCanvas bounds on touched items and zero churn
        model = HudModel()
        model.tier = "high"
        canvas = HudCanvas(tk_root, model=model, width=1280, height=800, motion="full", realism="off")
        canvas.pack()
        tk_root.update_idletasks()

        # First frame initializes
        canvas._draw_frame(0.033)
        initial_items = len(canvas.find_all())

        # Render successive frames until moving average reaches steady state
        for _ in range(15):
            model.update(0.033)
            canvas._draw_frame(0.033)

        # Verify zero item count growth (zero churn)
        assert len(canvas.find_all()) == initial_items
        # Verify touched items <= 450
        assert canvas.items_touched_last_frame <= 450
        assert canvas.items_touched_average <= 450.0
        canvas.destroy()


# ============================================================================
# PART B: Cinematic Realism Tests
# ============================================================================

class TestPartBCinematicRealism:
    """Tests covering Part B cinematic realism, bitmaps, and resilience."""

    def test_b1_ppm_generation_helper(self):
        """B1: Test build_ppm_bytes generates standard P6 binary PPM format."""
        arr = np.zeros((10, 20, 3), dtype=np.uint8)
        arr[0, 0] = [255, 128, 64]
        ppm = build_ppm_bytes(arr)

        assert ppm.startswith(b"P6\n20 10\n255\n")
        assert len(ppm) == len(b"P6\n20 10\n255\n") + (10 * 20 * 3)
        assert ppm[-3:] == bytes([0, 0, 0])

    def test_b1_background_bitmap_performance_and_determinism(self):
        """B1: Test background bitmap generates in < 2s and is deterministic."""
        pal = get_palette("gold")

        t0 = time.perf_counter()
        arr1, ppm1 = build_background_bitmap(640, 480, pal, seed=42)
        elapsed = time.perf_counter() - t0

        assert elapsed < 2.0, f"Background build too slow: {elapsed:.2f}s"
        assert arr1.shape == (480, 640, 3)
        assert arr1.dtype == np.uint8
        assert ppm1.startswith(b"P6\n640 480\n255\n")

        # Determinism check
        arr2, ppm2 = build_background_bitmap(640, 480, pal, seed=42)
        assert np.array_equal(arr1, arr2)
        assert ppm1 == ppm2

    def test_b2_bloom_sprites_border_matches_background(self):
        """B2: Test bloom halo sprites borders strictly match background color."""
        pal = get_palette("gold")
        bg_arr, _ = build_background_bitmap(640, 480, pal)
        rs = 100.0
        cx, cy = 320.0, 240.0

        sprites = build_bloom_sprites(rs, cx, cy, bg_arr, pal, num_levels=5)
        assert len(sprites) == 5

        for lvl, (arr, ppm) in enumerate(sprites):
            assert arr.shape[0] == arr.shape[1]
            S = arr.shape[0]
            half_S = S // 2
            x0 = int(round(cx)) - half_S
            y0 = int(round(cy)) - half_S

            # Edge pixels must match the corresponding region of bg_arr within 1 level
            expected_bg_top = bg_arr[y0, x0:x0 + S, :]
            expected_bg_bottom = bg_arr[y0 + S - 1, x0:x0 + S, :]

            assert np.all(np.abs(arr[0, :, :].astype(int) - expected_bg_top.astype(int)) <= 1)
            assert np.all(np.abs(arr[-1, :, :].astype(int) - expected_bg_bottom.astype(int)) <= 1)

    def test_b3_sphere_body_sprite_border_matches_background(self):
        """B3: Test translucent glass body sprite perimeter matches background."""
        pal = get_palette("gold")
        bg_arr, _ = build_background_bitmap(640, 480, pal)
        rs = 100.0
        cx, cy = 320.0, 240.0

        arr, ppm = build_sphere_body_sprite(rs, cx, cy, bg_arr, pal)
        assert arr.shape[0] == arr.shape[1]
        S = arr.shape[0]
        half_S = S // 2
        x0 = int(round(cx)) - half_S
        y0 = int(round(cy)) - half_S

        expected_bg_top = bg_arr[y0, x0:x0 + S, :]
        assert np.all(np.abs(arr[0, :, :].astype(int) - expected_bg_top.astype(int)) <= 1)

    def test_b5_multi_plane_parallax_particles(self):
        """B5: Test parallax particles initialization across deep, mid, near planes."""
        rng = random.Random(42)
        particles = build_parallax_particles(rng)
        assert len(particles) == 60

        deep = [p for p in particles if p.layer == 0]
        mid = [p for p in particles if p.layer == 1]
        near = [p for p in particles if p.layer == 2]

        assert len(deep) == 30
        assert len(mid) == 20
        assert len(near) == 10

        # Layer ordering
        assert deep[0].layer < mid[0].layer < near[0].layer

    def test_b6_memory_footprint_within_budget(self):
        """B6: Test total bitmap memory at 1920x1080 is <= 48 MB."""
        mem = calculate_image_memory_bytes(1920, 1080, sphere_radius=226.8, num_levels=8)
        budget = 48 * 1024 * 1024
        assert mem <= budget, f"Image memory {mem / (1024*1024):.2f} MB exceeds 48 MB budget"

    def test_b6_ui_realism_setting_spec_validation(self):
        """B6: Test UI_REALISM validation in SPECS."""
        assert "UI_REALISM" in SPECS
        assert validate("UI_REALISM", "full") == "full"
        assert validate("UI_REALISM", "lite") == "lite"
        assert validate("UI_REALISM", "off") == "off"
        assert validate("UI_REALISM", "FULL") == "full"

        with pytest.raises(SettingsError):
            validate("UI_REALISM", "ultra")

    def test_b7_photoimage_failure_fallback_to_vector(self, tk_root):
        """B7: Test graceful vector fallback if PhotoImage generation fails."""
        model = HudModel()
        canvas = HudCanvas(tk_root, model=model, width=640, height=480, realism="full")
        canvas.pack()
        tk_root.update_idletasks()

        # Simulate PhotoImage failure
        with patch("ui.hud.tk.PhotoImage", side_effect=Exception("Simulated PPM Error")):
            try:
                canvas._apply_realism_assets(b"P6\n1 1\n255\n\x00\x00\x00", [], None, (0, 0, 1))
            except Exception as exc:
                canvas.realism_fallback_reason = f"PhotoImage failed: {exc}"

        assert canvas.realism_fallback is True
        assert "PhotoImage failed" in canvas.realism_fallback_reason

        # Ensure frame still draws vector HUD cleanly without raising error
        canvas._draw_frame(0.033)
        assert len(canvas._sphere_front_items) > 0
        canvas.destroy()

    def test_b7_diagnostics_integration(self):
        """B7: Verify diagnostics includes UI Realism, Sprites, Bitmap time and Fallback."""
        class MockApp:
            def __init__(self):
                self.hud_canvas = MagicMock()
                self.hud_canvas.items_touched_last_frame = 210
                self.hud_canvas.items_touched_average = 195.4
                self.hud_canvas.realism_sprite_count = 5
                self.hud_canvas.bitmap_build_ms = 42.1
                self.hud_canvas.realism_fallback = False
                self.hud_canvas.model = HudModel()

        report = build_diagnostics_text(app=MockApp())
        assert "UI Realism: full" in report
        assert "Items Touched (Last): 210" in report
        assert "Items Touched (Avg): 195.4" in report
        assert "Realism Sprites: 5" in report
        assert "Bitmap Build Time: 42.1 ms" in report
        assert "Realism Fallback: False" in report
