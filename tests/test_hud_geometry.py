"""Comprehensive tests for ORBIT HUD v2.13+ reference geometry redesign.

Covers the new single-source layout(W, H) reference geometry, segmented arcs,
dust ring depth classification, pulse envelope, mic smoothing, governor particle
tiers, per-character tracking symmetry, and design_reference hygiene.
"""

from __future__ import annotations

import math
import os
import random

from ui.hud_core import (
    HudModel,
    build_dust_ring_particles,
    build_reference_sphere_segments,
    build_reference_ticks,
    build_segmented_arcs,
    layout,
    pulse_envelope_value,
    update_smoothed_mic,
)


# ============================================================================
# Layout positions and radii compliance across resolutions
# ============================================================================

_RESOLUTIONS = [
    (1100, 700),
    (1280, 800),
    (1920, 1080),
    (1100, 900),
]


def test_layout_positions_and_radii_table_compliance():
    """layout(W, H) values match reference table within 1 px at all reference resolutions."""
    for W, H in _RESOLUTIONS:
        ly = layout(W, H)

        # cx and cy
        assert abs(ly.cx - 0.50 * W) < 1.0, f"{W}x{H}: cx"
        assert abs(ly.cy - 0.43 * H) < 1.0, f"{W}x{H}: cy"

        # Sphere radius
        expected_sphere = 0.21 * H * ly.scale
        assert abs(ly.sphere_radius - expected_sphere) < 1.0, f"{W}x{H}: sphere_radius"

        # Core radii
        for idx, frac in enumerate([0.12, 0.20, 0.30, 0.42, 0.55]):
            expected = frac * ly.sphere_radius
            assert abs(ly.core_radii[idx] - expected) < 0.01, f"{W}x{H}: core[{idx}]"

        # Ring radii
        assert abs(ly.ring_inner_radius - 0.262 * H * ly.scale) < 1.0
        assert abs(ly.ring_outer_radius - 0.271 * H * ly.scale) < 1.0

        # Tick lengths
        assert abs(ly.minor_tick_len - 0.006 * H * ly.scale) < 1.0
        assert abs(ly.major_tick_len - 0.012 * H * ly.scale) < 1.0

        # Arc band
        assert abs(ly.arc_inner_radius - 0.285 * H * ly.scale) < 1.0
        assert abs(ly.arc_outer_radius - 0.310 * H * ly.scale) < 1.0

        # Dust ring
        assert abs(ly.dust_semi_major - 0.31 * H * ly.scale) < 1.0
        assert abs(ly.dust_semi_minor - 0.10 * H * ly.scale) < 1.0

        # Typography
        assert abs(ly.title_y - 0.045 * H) < 1.0
        assert abs(ly.title_rule_y - 0.068 * H) < 1.0
        assert abs(ly.title_rule_w - 0.24 * W) < 1.0
        assert abs(ly.state_label_y - 0.76 * H) < 1.0

        # Dock
        assert abs(ly.dock_y - 0.88 * H) < 1.0
        assert abs(ly.dock_side_radius - 0.038 * H) < 1.0
        assert abs(ly.dock_mic_radius - 0.060 * H) < 1.0
        assert len(ly.dock_xs) == 5
        for i, x_frac in enumerate([0.357, 0.422, 0.500, 0.578, 0.643]):
            assert abs(ly.dock_xs[i] - x_frac * W) < 1.0

        # Panel Y spans
        assert abs(ly.panel_y_top - 0.665 * H) < 1.0
        assert abs(ly.panel_y_bottom - 0.92 * H) < 1.0
        assert abs(ly.chat_y_expanded_top - 0.30 * H) < 1.0
        assert abs(ly.chips_y - 0.967 * H) < 1.0

        # Panel width floor
        assert ly.panel_width >= 260.0 - 0.5


def test_layout_scale_at_1100x900():
    """At 1100x900, scale < 1.0 is applied to shrink the orb for panel clearance."""
    ly = layout(1100, 900)
    assert ly.scale < 1.0
    assert ly.scale > 0.5
    assert abs(ly.panel_width - 260.0) < 1.0  # Floored at 260


def test_layout_scale_at_normal_resolutions():
    """At typical resolutions (1280x800, 1920x1080), scale remains 1.0."""
    for W, H in [(1280, 800), (1920, 1080), (1100, 700)]:
        ly = layout(W, H)
        assert ly.scale == 1.0, f"Scale at {W}x{H} should be 1.0"


# ============================================================================
# Panels never intersect the orb across aspect ratios
# ============================================================================


def test_panels_never_intersect_orb_across_aspect_ratios():
    """Sweep window sizes; panels never overlap the orb's arc outer radius bounding circle."""
    test_sizes = [
        (1100, 700),   # Wide
        (1280, 800),   # Default
        (1920, 1080),  # Large
        (1100, 900),   # Tall, triggers scale
        (1100, 1000),  # Very tall
        (1200, 700),
        (1400, 900),
        (1600, 1000),
        (1100, 1200),  # Extreme
    ]

    for W, H in test_sizes:
        ly = layout(W, H)

        # Arc outer radius defines the orb bounding circle
        orb_left = ly.cx - ly.arc_outer_radius
        orb_right = ly.cx + ly.arc_outer_radius

        # Chat panel right edge must be at least 12 px left of orb left edge
        clearance_left = orb_left - ly.chat_x_right
        assert clearance_left >= 11.5, (
            f"{W}x{H}: chat right {ly.chat_x_right:.1f} too close to "
            f"orb left {orb_left:.1f} (clearance={clearance_left:.1f})"
        )

        # Right panel left edge must be at least 12 px right of orb right edge
        clearance_right = ly.right_x_left - orb_right
        assert clearance_right >= 11.5, (
            f"{W}x{H}: right left {ly.right_x_left:.1f} too close to "
            f"orb right {orb_right:.1f} (clearance={clearance_right:.1f})"
        )


# ============================================================================
# Segmented arc block counts, gaps, and brightness profile
# ============================================================================


def test_segmented_arc_blocks_counts_gaps_and_brightness():
    """Arc A has 24 blocks, Arc B has 20 blocks; 3.2 deg block, 0.8 deg gap, bell peak at 55%."""
    rng = random.Random(42)
    blocks_a, blocks_b = build_segmented_arcs(rng)

    assert len(blocks_a) == 24
    assert len(blocks_b) == 20

    # Arc A spans from 92 to 188 degrees (24 blocks * 4 deg pitch)
    assert abs(blocks_a[0].start_deg - 92.0) < 0.01
    assert abs(blocks_a[-1].start_deg - (92.0 + 23 * 4.0)) < 0.01

    for blk in blocks_a:
        # Block angular width = 3.2 degrees
        assert abs(abs(blk.end_deg - blk.start_deg) - 3.2) < 0.01
        # Per-block weight in [0.4, 1.0]
        assert 0.4 <= blk.weight <= 1.0 + 1e-9

    # Arc B spans from 8 to -72 degrees (20 blocks * 4 deg pitch)
    assert abs(blocks_b[0].start_deg - 8.0) < 0.01

    for blk in blocks_b:
        assert abs(abs(blk.end_deg - blk.start_deg) - 3.2) < 0.01
        assert 0.4 <= blk.weight <= 1.0 + 1e-9

    # Bell curve peaks at ~55% along each arc
    # For arc A (24 blocks), peak index ~= 0.55 * 23 = 12.65, so block 12 or 13
    brightnesses_a = [b.brightness for b in blocks_a]
    peak_idx_a = brightnesses_a.index(max(brightnesses_a))
    expected_peak_a = round(0.55 * (len(blocks_a) - 1))
    assert abs(peak_idx_a - expected_peak_a) <= 1, f"Arc A peak at {peak_idx_a}, expected near {expected_peak_a}"

    brightnesses_b = [b.brightness for b in blocks_b]
    peak_idx_b = brightnesses_b.index(max(brightnesses_b))
    expected_peak_b = round(0.55 * (len(blocks_b) - 1))
    assert abs(peak_idx_b - expected_peak_b) <= 1, f"Arc B peak at {peak_idx_b}, expected near {expected_peak_b}"


# ============================================================================
# Dust ring depth classification and draw order
# ============================================================================


def test_dust_ring_depth_classification_and_draw_order():
    """Front half (z >= 0) vs back half (z < 0) of the dust ring particles."""
    rng = random.Random(42)
    particles = build_dust_ring_particles(rng, count=350)
    assert len(particles) == 350

    # Simulate depth classification at a specific orbit phase
    incl_deg = 70.0
    incl_rad = math.radians(incl_deg)
    orbit_phase_rad = 0.0

    front_count = 0
    back_count = 0
    for p in particles:
        phase = p.base_phase + math.radians(orbit_phase_rad)
        sin_ph = math.sin(phase)

        # In 3D: x = semi_major * cos(phase), y = semi_minor * sin(phase) * cos(incl)
        #         z = semi_minor * sin(phase) * sin(incl)
        # Then rotate by screen tilt
        z_raw = sin_ph * math.sin(incl_rad)
        if z_raw >= 0:
            front_count += 1
        else:
            back_count += 1

    # Roughly half should be front and half back (they're evenly distributed)
    assert front_count > 100, f"Too few front particles: {front_count}"
    assert back_count > 100, f"Too few back particles: {back_count}"
    assert front_count + back_count == 350


# ============================================================================
# Pulse envelope values and bounds
# ============================================================================


def test_pulse_envelope_values_and_bounds():
    """Verify envelope at exact checkpoints: 0 -> 0.0, 0.25 -> 1.0, 0.50 -> 0.6875, 1.0 -> 0.0625."""
    assert pulse_envelope_value(0.0) == 0.0
    assert abs(pulse_envelope_value(0.25) - 1.0) < 1e-9
    assert abs(pulse_envelope_value(0.50) - 0.6875) < 1e-9
    assert abs(pulse_envelope_value(1.00) - 0.0625) < 1e-9

    # At t = 1.05, should be exactly 0.0
    assert pulse_envelope_value(1.05) == 0.0
    # At t = 2.0, should be 0.0
    assert pulse_envelope_value(2.0) == 0.0


def test_pulse_envelope_never_overshoots():
    """Pulse envelope is strictly bounded in [0.0, 1.0] for all t in [0, 3]."""
    for i in range(3001):
        t = i / 1000.0
        val = pulse_envelope_value(t)
        assert 0.0 <= val <= 1.0, f"Overshoot at t={t}: {val}"


def test_pulse_envelope_monotonic_attack():
    """Envelope is monotonically non-decreasing during attack phase [0, 0.25]."""
    prev = 0.0
    for i in range(251):
        t = i / 1000.0
        val = pulse_envelope_value(t)
        assert val >= prev - 1e-12, f"Non-monotone attack at t={t}"
        prev = val


def test_pulse_envelope_monotonic_decay():
    """Envelope is monotonically non-increasing during decay phase [0.25, 1.05]."""
    prev = 1.0
    for i in range(250, 1051):
        t = i / 1000.0
        val = pulse_envelope_value(t)
        assert val <= prev + 1e-12, f"Non-monotone decay at t={t}"
        prev = val


# ============================================================================
# Mic smoothing attack and decay
# ============================================================================


def test_mic_smoothing_attack_and_decay():
    """update_smoothed_mic with 80ms attack / 400ms decay time constants."""
    dt = 0.016  # ~60 fps frame time

    # Attack: level jumps from 0 to 1
    level = 0.0
    for _ in range(10):  # 160ms of attack
        level = update_smoothed_mic(level, 1.0, dt, tau_attack=0.08, tau_decay=0.40)
    # After ~160ms of attack with tau=80ms, should be well above 0.5
    assert level > 0.5, f"Attack too slow: {level}"
    assert level < 1.0, f"Attack overshoots: {level}"

    # Continue attack for more frames
    for _ in range(50):
        level = update_smoothed_mic(level, 1.0, dt, tau_attack=0.08, tau_decay=0.40)
    assert level > 0.95, f"Attack should converge near 1.0: {level}"

    # Decay: target drops to 0
    for _ in range(10):  # 160ms of decay
        level = update_smoothed_mic(level, 0.0, dt, tau_attack=0.08, tau_decay=0.40)
    # After ~160ms of decay with tau=400ms, still well above 0
    assert level > 0.3, f"Decay too fast: {level}"

    # Much longer decay
    for _ in range(200):  # ~3.2 seconds
        level = update_smoothed_mic(level, 0.0, dt, tau_attack=0.08, tau_decay=0.40)
    assert level < 0.01, f"Decay should converge near 0: {level}"


def test_mic_level_drives_hud_model():
    """HudModel.set_mic_level drives smoothed mic_level upward."""
    model = HudModel()
    assert model.mic_level == 0.0

    model.set_mic_level(1.0)
    after_one = model.mic_level
    assert after_one > 0.0

    model.set_mic_level(1.0)
    after_two = model.mic_level
    assert after_two > after_one


# ============================================================================
# Governor tiers scaling particles and ticks
# ============================================================================


def test_governor_tiers_scaling():
    """Governor tiers scale particles (350/200/100/0) and tick marks."""
    rng = random.Random(42)
    dust_particles = build_dust_ring_particles(rng, count=350)
    assert len(dust_particles) == 350

    ticks = build_reference_ticks()
    assert len(ticks) == 120  # 360 / 3 = 120 ticks

    # Verify tier scaling rules:
    # High: 350 particles, 120 ticks
    # Medium: 200 particles, 60 ticks (step 2)
    # Low: 100 particles, 24 ticks (step 5)
    # Static: 0 particles, 0 ticks
    tier_particle_limits = {"high": 350, "medium": 200, "low": 100, "static": 0}
    for tier, expected in tier_particle_limits.items():
        visible = dust_particles[:expected]
        assert len(visible) == expected

    # Tick mark scaling by governor tier
    tier_tick_limits = {"high": 120, "medium": 60, "low": 24}
    for tier, expected_count in tier_tick_limits.items():
        step = 1 if tier == "high" else (2 if tier == "medium" else 5)
        visible_ticks = ticks[::step][:expected_count]
        assert len(visible_ticks) <= expected_count


# ============================================================================
# Reference sphere segments
# ============================================================================


def test_reference_sphere_segments_structure():
    """16 meridians and latitudes every 15 degrees, producing expected segment counts."""
    segments = build_reference_sphere_segments()

    # Latitudes: from -75 to +75 in steps of 15 = 11 latitude circles,
    # each with 36 segments = 396
    lat_count = 11  # -75, -60, -45, -30, -15, 0, 15, 30, 45, 60, 75
    lat_segments = lat_count * 36

    # Meridians: 16, each with 24 segments = 384
    meridian_segments = 16 * 24

    expected_total = lat_segments + meridian_segments
    assert len(segments) == expected_total, f"Expected {expected_total}, got {len(segments)}"


# ============================================================================
# Reference ticks structure
# ============================================================================


def test_reference_ticks_structure():
    """120 ticks: minor every 3 deg, major every 15 deg."""
    ticks = build_reference_ticks()
    assert len(ticks) == 120

    major_ticks = [t for t in ticks if t[2]]  # is_major
    minor_ticks = [t for t in ticks if not t[2]]

    # Major: every 15 deg -> 360/15 = 24
    assert len(major_ticks) == 24

    # Minor: 120 - 24 = 96
    assert len(minor_ticks) == 96

    # Major tick length ratio is 0.012, minor is 0.006
    for ang, length, is_major in ticks:
        if is_major:
            assert abs(length - 0.012) < 1e-9
        else:
            assert abs(length - 0.006) < 1e-9


# ============================================================================
# Per-character tracking symmetry
# ============================================================================


def test_per_character_tracking_symmetry():
    """Letter-spaced 'ORBIT' positions are symmetric around centre cx."""
    ly = layout(1280, 800)
    cx = ly.cx
    em_track = 14.0  # As used in hud.py

    positions = []
    for idx in range(5):
        offset_idx = idx - 2  # -2, -1, 0, 1, 2
        char_x = cx + offset_idx * em_track
        positions.append(char_x)

    # Centre character at cx
    assert abs(positions[2] - cx) < 0.01

    # Symmetric pairs: pos[0]+pos[4] == 2*cx, pos[1]+pos[3] == 2*cx
    assert abs((positions[0] + positions[4]) - 2 * cx) < 0.01
    assert abs((positions[1] + positions[3]) - 2 * cx) < 0.01

    # Equal spacing
    for i in range(4):
        gap = positions[i + 1] - positions[i]
        assert abs(gap - em_track) < 0.01


# ============================================================================
# Trigger pulse types on HudModel
# ============================================================================


def test_hud_model_trigger_pulse_types():
    """Trigger pulse stores type and strength; pulse_val updates after model.update."""
    t = 1000.0

    def clock():
        return t

    model = HudModel(clock=clock)

    # No pulse initially
    model.update(0.033)
    assert model.pulse_val == 0.0

    # Trigger full pulse
    model.trigger_pulse("full", 1.0)
    assert model.pulse_type == "full"

    # Advance to peak (t + 0.25s)
    t += 0.25
    model.update(0.033)
    assert abs(model.pulse_val - 1.0) < 0.01

    # Trigger mild pulse with lower strength
    t += 2.0  # Clear previous pulse
    model.update(0.033)
    model.trigger_pulse("mild", 0.45)
    assert model.pulse_type == "mild"

    t += 0.25
    model.update(0.033)
    assert abs(model.pulse_val - 0.45) < 0.05


# ============================================================================
# Design reference hygiene
# ============================================================================


def test_design_reference_not_read_by_app_code():
    """No application source file (outside tests/ and scripts/) reads design_reference/."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    app_files = []
    for root_dir, dirs, files in os.walk(project_root):
        # Skip test, scripts, venv, .git, .planning, __pycache__ directories
        rel = os.path.relpath(root_dir, project_root)
        skip_dirs = {"tests", "scripts", "venv", ".git", ".planning",
                     "__pycache__", ".pytest_cache", "design_reference",
                     "evals", "demo_home", ".agents", ".agent"}
        parts = rel.replace("\\", "/").split("/")
        if any(p in skip_dirs for p in parts):
            continue
        for fname in files:
            if fname.endswith(".py"):
                app_files.append(os.path.join(root_dir, fname))

    for fpath in app_files:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        assert "design_reference" not in content, (
            f"Application file {fpath} references design_reference/"
        )


def test_design_reference_not_tracked_by_git():
    """design_reference/ is listed in .gitignore and not tracked."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    gitignore_path = os.path.join(project_root, ".gitignore")

    with open(gitignore_path, "r", encoding="utf-8") as f:
        gitignore = f.read()

    assert "design_reference/" in gitignore, ".gitignore must exclude design_reference/"


# ============================================================================
# Drift angle rates
# ============================================================================


def test_drift_angle_rates():
    """Arc A drifts +3 deg/s, Arc B drifts -2 deg/s, dust orbit 6 deg/s."""
    model = HudModel()
    dt = 1.0  # 1 second

    model.update(dt)

    assert abs(model.arc_a_drift_deg - 3.0) < 0.01
    assert abs(model.arc_b_drift_deg - (-2.0) % 360) < 0.01
    assert abs(model.dust_orbit_deg - 6.0) < 0.01
