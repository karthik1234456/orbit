"""Pure Python mathematical model, 3D projection, and state machine for ORBIT HUD.

Strictly isolated from GUI frameworks: NO tkinter or UI dependencies.
Implements:
- HudLayout dataclass and single-source layout(W, H) reference geometry
- HudState priority hierarchy (confirming > error > speaking > listening > thinking > idle)
- 3D wireframe sphere (16 meridians, latitude every 15 deg, 18 deg axial tilt)
- Segmented arcs (Arc A: 92-188 deg, Arc B: +8 to -72 deg, bell curve brightness)
- Tilted dust ring with 350 particles, central gold ellipse, and front/back depth sorting
- Concentric double ring with minor and major tick marks
- Core glow concentric filled circles (stepping #FADEA4 to #B29C71)
- Pulse envelope with attack 0.25 s and decay 0.80 s, and audio/task drivers
- Frame-rate independent dt updates
- Adaptive quality governor with 25ms threshold and 30-second hysteresis
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import math
import random
import time
from typing import Any, Callable, Optional


class HudState(str, Enum):
    """Observable states of the HUD interface with strict priority resolution."""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    CONFIRMING = "confirming"
    ERROR = "error"


@dataclass(frozen=True)
class HudLayout:
    """Complete geometric layout specification for the ORBIT HUD window.

    All coordinates and dimensions are derived from window width W and height H
    in layout(W, H), with zero magic numbers elsewhere.
    """

    W: float
    H: float
    cx: float
    cy: float
    scale: float
    sphere_radius: float
    core_radii: list[float]
    ring_inner_radius: float
    ring_outer_radius: float
    minor_tick_len: float
    major_tick_len: float
    arc_inner_radius: float
    arc_outer_radius: float
    dust_semi_major: float
    dust_semi_minor: float
    title_y: float
    title_rule_y: float
    title_rule_w: float
    state_label_y: float
    dock_y: float
    dock_xs: list[float]
    dock_side_radius: float
    dock_mic_radius: float
    panel_y_top: float
    panel_y_bottom: float
    panel_width: float
    chat_x_left: float
    chat_x_right: float
    chat_y_expanded_top: float
    right_x_left: float
    right_x_right: float
    chips_y: float
    orb_box_size: float = 0.0
    dock_width: float = 0.0
    dock_height: float = 0.0
    dock_margin: float = 0.0
    scaling: float = 1.0


def clamp_zoom_factor(W: float, H: float, zoom: float, scale: float = 1.0) -> float:
    """Clamp orb zoom factor between 0.8x and 1.25x so panels never overlap the orb."""
    cx = 0.50 * float(W)
    chat_x_left = 0.03 * float(W)
    unscaled_arc_outer = 0.310 * float(H)
    current_arc_outer = unscaled_arc_outer * max(0.1, float(scale))
    clearance = cx - 12.0 - chat_x_left - 260.0
    if current_arc_outer > 0:
        max_zoom = clearance / current_arc_outer
    else:
        max_zoom = 1.25
    upper = min(1.25, max(0.8, max_zoom))
    return max(0.8, min(upper, float(zoom)))


def layout(W: float, H: float, scaling: float = 1.0) -> HudLayout:
    """Calculate single-source-of-truth HUD geometry from window dimensions.

    Reference geometry:
    - Orb centre cx = 0.50 W, cy = 0.43 H
    - OrbLayer box size: square of 0.74 H * scale
    - Sphere radius 0.21 H * scale
    - Panel widths = min(0.262 W, cx - arc_outer - 12 px) with floor 260 px
    - If floor cannot be met, shrink whole orb by scale factor so panels NEVER overlap
    - Dock canvas sized from radii, margins, and Tk scaling factor so bounding boxes never clip
    """
    W = float(W)
    H = float(H)
    scaling = max(0.5, min(4.0, float(scaling)))
    cx = 0.50 * W
    cy = 0.43 * H

    chat_x_left = 0.03 * W
    right_x_right = 0.97 * W

    # Clearance required: cx - arc_outer - 12 px margin - chat_x_left >= 260.0
    unscaled_arc_outer = 0.310 * H
    max_clear_radius = (cx - 12.0) - chat_x_left - 260.0
    if unscaled_arc_outer > max_clear_radius and unscaled_arc_outer > 0:
        scale = max(0.1, max_clear_radius / unscaled_arc_outer)
        panel_width = 260.0
    else:
        scale = 1.0
        max_allowed_w = (cx - unscaled_arc_outer - 12.0) - chat_x_left
        panel_width = max(260.0, min(0.262 * W, max_allowed_w))

    chat_x_right = chat_x_left + panel_width
    right_x_left = right_x_right - panel_width

    orb_box_size = 0.74 * H * scale

    # Sphere & Core
    sphere_radius = 0.21 * H * scale
    core_radii = [r * sphere_radius for r in (0.12, 0.20, 0.30, 0.42, 0.55)]

    # Double ring & ticks
    ring_inner_radius = 0.262 * H * scale
    ring_outer_radius = 0.271 * H * scale
    minor_tick_len = 0.006 * H * scale
    major_tick_len = 0.012 * H * scale

    # Segmented arcs (band 0.285 H to 0.310 H)
    arc_inner_radius = 0.285 * H * scale
    arc_outer_radius = 0.310 * H * scale

    # Dust ring
    dust_semi_major = 0.31 * H * scale
    dust_semi_minor = 0.10 * H * scale

    # Typography
    title_y = 0.045 * H
    title_rule_y = 0.068 * H
    title_rule_w = 0.24 * W
    state_label_y = 0.76 * H

    # Dock (scaled for DPI/Tk scaling)
    dock_y = 0.88 * H
    dock_xs = [0.357 * W, 0.422 * W, 0.500 * W, 0.578 * W, 0.643 * W]
    dock_side_radius = 0.038 * H * scaling
    dock_mic_radius = 0.060 * H * scaling
    dock_margin = 10.0 * scaling
    dock_height = float(math.ceil(2.0 * (dock_mic_radius + dock_margin)))
    min_dock_x = dock_xs[0]
    max_dock_x = dock_xs[-1]
    dock_width = float(math.ceil((max_dock_x - min_dock_x) + 2.0 * (dock_side_radius + dock_margin)))

    # Panels vertical
    panel_y_top = 0.665 * H
    panel_y_bottom = 0.92 * H
    chat_y_expanded_top = 0.30 * H
    chips_y = 0.967 * H

    return HudLayout(
        W=W,
        H=H,
        cx=cx,
        cy=cy,
        scale=scale,
        sphere_radius=sphere_radius,
        core_radii=core_radii,
        ring_inner_radius=ring_inner_radius,
        ring_outer_radius=ring_outer_radius,
        minor_tick_len=minor_tick_len,
        major_tick_len=major_tick_len,
        arc_inner_radius=arc_inner_radius,
        arc_outer_radius=arc_outer_radius,
        dust_semi_major=dust_semi_major,
        dust_semi_minor=dust_semi_minor,
        title_y=title_y,
        title_rule_y=title_rule_y,
        title_rule_w=title_rule_w,
        state_label_y=state_label_y,
        dock_y=dock_y,
        dock_xs=dock_xs,
        dock_side_radius=dock_side_radius,
        dock_mic_radius=dock_mic_radius,
        panel_y_top=panel_y_top,
        panel_y_bottom=panel_y_bottom,
        panel_width=panel_width,
        chat_x_left=chat_x_left,
        chat_x_right=chat_x_right,
        chat_y_expanded_top=chat_y_expanded_top,
        right_x_left=right_x_left,
        right_x_right=right_x_right,
        chips_y=chips_y,
        orb_box_size=orb_box_size,
        dock_width=dock_width,
        dock_height=dock_height,
        dock_margin=dock_margin,
        scaling=scaling,
    )


def pulse_envelope_value(t: float, attack: float = 0.25, decay: float = 0.80) -> float:
    """Evaluate normalized pulse envelope at time t >= 0.

    Peak is at t = attack with value 1.0. Decays to 0.0 at t = attack + decay.
    Values are bounded strictly in [0.0, 1.0] with zero overshoot.
    """
    if t <= 0.0:
        return 0.0
    if t <= attack:
        return t / attack
    if t <= attack + decay:
        return 1.0 - (t - attack) / decay
    return 0.0


def update_smoothed_mic(
    current: float,
    target: float,
    dt: float,
    tau_attack: float = 0.08,
    tau_decay: float = 0.40,
) -> float:
    """Update smoothed microphone level with distinct attack (80ms) and decay (400ms) time constants."""
    dt = max(0.0, float(dt))
    target = max(0.0, min(1.0, float(target)))
    tau = tau_attack if target > current else tau_decay
    if tau <= 0.0:
        return target
    alpha = 1.0 - math.exp(-dt / tau)
    return current + alpha * (target - current)


def _hex_to_rgb(hex_code: str) -> tuple[int, int, int]:
    """Parse #rrggbb into RGB integer tuple."""
    clean = hex_code.lstrip("#")
    if len(clean) == 3:
        clean = "".join(c * 2 for c in clean)
    if len(clean) != 6:
        return (0, 229, 255)
    return (int(clean[0:2], 16), int(clean[2:4], 16), int(clean[4:6], 16))


def interpolate_color(hex1: str, hex2: str, t: float) -> str:
    """Linearly interpolate between hex1 (t=0) and hex2 (t=1)."""
    t = max(0.0, min(1.0, float(t)))
    r1, g1, b1 = _hex_to_rgb(hex1)
    r2, g2, b2 = _hex_to_rgb(hex2)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def adjust_color_brightness(hex_code: str, factor: float) -> str:
    """Step brightness of a color by factor (e.g. 1.2 for +20%), returning #rrggbb."""
    r, g, b = _hex_to_rgb(hex_code)
    r = min(255, max(0, int(r * factor)))
    g = min(255, max(0, int(g * factor)))
    b = min(255, max(0, int(b * factor)))
    return f"#{r:02x}{g:02x}{b:02x}"


def rotate(
    points: list[tuple[float, float, float]],
    ax: float,
    ay: float,
    az: float,
) -> list[tuple[float, float, float]]:
    """Rotate 3D points around X, Y, and Z axes preserving vector lengths.

    R_x, R_y, R_z applied in order. Vector Euclidean norm is strictly invariant.
    """
    cx, sx = math.cos(ax), math.sin(ax)
    cy, sy = math.cos(ay), math.sin(ay)
    cz, sz = math.cos(az), math.sin(az)

    rotated: list[tuple[float, float, float]] = []
    for x, y, z in points:
        y1 = y * cx - z * sx
        z1 = y * sx + z * cx

        x2 = x * cy + z1 * sy
        z2 = -x * sy + z1 * cy

        x3 = x2 * cz - y1 * sz
        y3 = x2 * sz + y1 * cz
        z3 = z2

        rotated.append((x3, y3, z3))
    return rotated


def project(
    points: list[tuple[float, float, float]],
    cx: float,
    cy: float,
    radius: float,
    focal: float = 300.0,
) -> list[tuple[float, float, float]]:
    """Project normalized 3D coordinates onto 2D screen space with perspective.

    Returns (screen_x, screen_y, depth_z). Larger depth_z is closer to viewer.
    """
    projected: list[tuple[float, float, float]] = []
    for x, y, z in points:
        world_x = x * radius
        world_y = y * radius
        world_z = z * radius

        dist = focal + world_z
        if dist <= 1.0:
            dist = 1.0
        scale = focal / dist

        sx = cx + world_x * scale
        sy = cy + world_y * scale
        projected.append((sx, sy, z))
    return projected


def project_orthographic(
    points: list[tuple[float, float, float]],
    cx: float,
    cy: float,
    radius: float,
) -> list[tuple[float, float, float]]:
    """Project 3D unit-sphere points orthographically onto screen plane.

    Positions lie on a true circle of radius r:
    x_proj = cx + x * radius
    y_proj = cy + y * radius
    z_val = z (depth retained for line brightness/dimming)

    Guaranteed: sqrt((x_proj - cx)^2 + (y_proj - cy)^2) <= radius + 0.5 px for all unit sphere points.
    """
    projected: list[tuple[float, float, float]] = []
    for x, y, z in points:
        projected.append((cx + x * radius, cy + y * radius, z))
    return projected


def quantize_depth_bucket(z: float, num_buckets: int = 6) -> int:
    """Quantize depth z in [-1.0, 1.0] into discrete buckets in range [0, num_buckets - 1]."""
    norm = (float(z) + 1.0) / 2.0
    bucket = int(norm * num_buckets)
    return max(0, min(num_buckets - 1, bucket))


def depth_color(
    depth: float,
    palette: Any,
    min_z: float = -1.0,
    max_z: float = 1.0,
) -> str:
    """Interpolate color between palette.accent (front) and palette.accent_dim/bg (back)."""
    if max_z <= min_z:
        t = 0.5
    else:
        t = (depth - min_z) / (max_z - min_z)
    t = max(0.0, min(1.0, t))

    accent = getattr(palette, "accent", "#00e5ff")
    dim = getattr(palette, "accent_dim", getattr(palette, "panel_edge", "#00838f"))

    r1, g1, b1 = _hex_to_rgb(dim)
    r2, g2, b2 = _hex_to_rgb(accent)

    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


@dataclass
class ArcBlock:
    start_deg: float
    end_deg: float
    weight: float
    base_color: str
    brightness: float


def build_segmented_arcs(
    rng: random.Random,
    color_dim: str = "#7E6C48",
    color_bright: str = "#F2D8A1",
) -> tuple[list[ArcBlock], list[ArcBlock]]:
    """Build Arc A (92 to 188 deg, 24 blocks) and Arc B (8 to -72 deg, 20 blocks).

    Blocks are 3.2 deg with 0.8 deg gaps (pitch 4.0 deg).
    Brightness follows bell curve peaking at 55% along each arc.
    Each block receives a fixed random weight in [0.4, 1.0].
    """
    blocks_a: list[ArcBlock] = []
    n_a = 24
    for i in range(n_a):
        deg_start = 92.0 + i * 4.0
        deg_end = deg_start + 3.2
        u = i / (n_a - 1) if n_a > 1 else 0.5
        b_val = math.exp(-0.5 * ((u - 0.55) / 0.22) ** 2)
        b_val = max(0.0, min(1.0, b_val))
        col = interpolate_color(color_dim, color_bright, b_val)
        w = rng.uniform(0.4, 1.0)
        blocks_a.append(ArcBlock(deg_start, deg_end, w, col, b_val))

    blocks_b: list[ArcBlock] = []
    n_b = 20
    for i in range(n_b):
        deg_start = 8.0 - i * 4.0
        deg_end = deg_start - 3.2
        u = i / (n_b - 1) if n_b > 1 else 0.5
        b_val = math.exp(-0.5 * ((u - 0.55) / 0.22) ** 2)
        b_val = max(0.0, min(1.0, b_val))
        col = interpolate_color(color_dim, color_bright, b_val)
        w = rng.uniform(0.4, 1.0)
        blocks_b.append(ArcBlock(deg_start, deg_end, w, col, b_val))

    return blocks_a, blocks_b


class DustRingSoA:
    """Struct-of-arrays representation of dust ring particles for vectorized projection and depth sorting.

    Features (A4, B4):
    - 600 particles on tier high, 300 medium, 150 low
    - Continuous elliptical band + second faint ring at half density
    - 1 px particle size
    - Vectorized rotation and projection with numpy when importable; pure Python fallback within 0.01 px
    """

    def __init__(self, rng: random.Random, count: int = 600):
        self.count = max(10, int(count))
        self.phases: list[float] = []
        self.jitters: list[float] = []
        self.speeds: list[float] = []
        self.ring_types: list[int] = []  # 0: primary, 1: secondary faint

        n_prim = int(round(self.count * (2.0 / 3.0)))
        n_sec = self.count - n_prim

        for i in range(n_prim):
            phase = (2.0 * math.pi * i) / max(1, n_prim) + rng.uniform(-0.015, 0.015)
            self.phases.append(phase)
            self.jitters.append(rng.gauss(0.0, 0.018))
            self.speeds.append(math.radians(6.0) * rng.uniform(0.9, 1.1))
            self.ring_types.append(0)

        for i in range(n_sec):
            phase = (2.0 * math.pi * i) / max(1, n_sec) + rng.uniform(-0.02, 0.02)
            self.phases.append(phase)
            self.jitters.append(rng.gauss(0.0, 0.022))
            self.speeds.append(math.radians(5.0) * rng.uniform(0.9, 1.1))
            self.ring_types.append(1)

        try:
            import numpy as np

            self._np_phases = np.array(self.phases, dtype=np.float64)
            self._np_jitters = np.array(self.jitters, dtype=np.float64)
            self._np_speeds = np.array(self.speeds, dtype=np.float64)
            self._np_ring_types = np.array(self.ring_types, dtype=np.int32)
            self._has_numpy = True
        except ImportError:
            self._has_numpy = False

    def update(self, dt: float) -> None:
        dt = max(0.0, float(dt))
        two_pi = 2.0 * math.pi
        if self._has_numpy:
            self._np_phases = (self._np_phases + self._np_speeds * dt) % two_pi
            self.phases = self._np_phases.tolist()
        else:
            for i in range(self.count):
                self.phases[i] = (self.phases[i] + self.speeds[i] * dt) % two_pi

    def project_python(
        self,
        cx: float,
        cy: float,
        semi_major_1: float,
        semi_minor_1: float,
        semi_major_2: float,
        semi_minor_2: float,
        tilt_rad: float = 0.0,
    ) -> list[tuple[float, float, float, int]]:
        """Pure Python fallback projection. Returns list of (x, y, z, ring_type)."""
        cos_tilt = math.cos(tilt_rad)
        sin_tilt = math.sin(tilt_rad)
        results: list[tuple[float, float, float, int]] = []
        for i in range(self.count):
            rtype = self.ring_types[i]
            s_maj = semi_major_1 if rtype == 0 else semi_major_2
            s_min = semi_minor_1 if rtype == 0 else semi_minor_2
            r = s_maj * (1.0 + self.jitters[i])
            phase = self.phases[i]
            x_loc = r * math.cos(phase)
            y_loc = (r * (s_min / s_maj)) * math.sin(phase)
            x_rot = x_loc * cos_tilt - y_loc * sin_tilt
            y_rot = x_loc * sin_tilt + y_loc * cos_tilt
            z = math.sin(phase)
            results.append((cx + x_rot, cy + y_rot, z, rtype))
        return results

    def project_numpy(
        self,
        cx: float,
        cy: float,
        semi_major_1: float,
        semi_minor_1: float,
        semi_major_2: float,
        semi_minor_2: float,
        tilt_rad: float = 0.0,
    ) -> list[tuple[float, float, float, int]]:
        """Vectorized numpy projection matching project_python within 0.01 px."""
        if not self._has_numpy:
            return self.project_python(cx, cy, semi_major_1, semi_minor_1, semi_major_2, semi_minor_2, tilt_rad)
        import numpy as np

        mask0 = self._np_ring_types == 0
        s_maj = np.where(mask0, semi_major_1, semi_major_2)
        s_min = np.where(mask0, semi_minor_1, semi_minor_2)
        r = s_maj * (1.0 + self._np_jitters)
        cos_p = np.cos(self._np_phases)
        sin_p = np.sin(self._np_phases)
        x_loc = r * cos_p
        y_loc = (r * (s_min / s_maj)) * sin_p
        cos_tilt = math.cos(tilt_rad)
        sin_tilt = math.sin(tilt_rad)
        x_rot = x_loc * cos_tilt - y_loc * sin_tilt
        y_rot = x_loc * sin_tilt + y_loc * cos_tilt
        x_out = cx + x_rot
        y_out = cy + y_rot
        z_out = sin_p
        out = []
        for i in range(self.count):
            out.append((float(x_out[i]), float(y_out[i]), float(z_out[i]), int(self._np_ring_types[i])))
        return out


@dataclass
class StaticStar:
    norm_x: float  # [0.0, 1.0] in window space
    norm_y: float  # [0.0, 1.0]
    brightness: float  # in [0.10, 0.35] (strictly <= 0.35)
    size: float  # strictly 1.0 (never > 2.0)


def build_static_stars(rng: random.Random, count: int = 40) -> list[StaticStar]:
    """Generate at most 40 static stars with size 1 px and brightness <= 35% accent (B5)."""
    count = min(40, max(0, int(count)))
    stars: list[StaticStar] = []
    for _ in range(count):
        stars.append(
            StaticStar(
                norm_x=rng.uniform(0.02, 0.98),
                norm_y=rng.uniform(0.02, 0.95),
                brightness=rng.uniform(0.12, 0.35),
                size=1.0,
            )
        )
    return stars


@dataclass
class DustParticle:
    base_phase: float
    radial_jitter: float
    speed_deg: float
    size: float


def build_dust_ring_particles(rng: random.Random, count: int = 350) -> list[DustParticle]:
    """Generate dust particles orbiting at 6 deg/s with gaussian radial jitter."""
    particles: list[DustParticle] = []
    for i in range(count):
        base_phase = (2.0 * math.pi * i) / count + rng.uniform(-0.01, 0.01)
        jitter = rng.gauss(0.0, 0.015)
        particles.append(
            DustParticle(
                base_phase=base_phase,
                radial_jitter=jitter,
                speed_deg=6.0,
                size=rng.choice([1.0, 1.5, 2.0]),
            )
        )
    return particles


def build_reference_sphere_segments() -> list[tuple[tuple[float, float, float], tuple[float, float, float]]]:
    """Generate wireframe sphere segments with 16 meridians and latitudes every 15 degrees.

    Axial tilt of 18 degrees towards the viewer is applied during projection.
    """
    segments: list[tuple[tuple[float, float, float], tuple[float, float, float]]] = []

    # Latitudes every 15 degrees (-75 to +75)
    steps_circle = 36
    for lat_deg in range(-75, 90, 15):
        phi = math.radians(lat_deg)
        r_lat = math.cos(phi)
        z_lat = math.sin(phi)
        for s in range(steps_circle):
            t1 = (2.0 * math.pi * s) / steps_circle
            t2 = (2.0 * math.pi * (s + 1)) / steps_circle
            p1 = (r_lat * math.cos(t1), z_lat, r_lat * math.sin(t1))
            p2 = (r_lat * math.cos(t2), z_lat, r_lat * math.sin(t2))
            segments.append((p1, p2))

    # 16 Meridians
    num_meridians = 16
    steps_meridian = 24
    for j in range(num_meridians):
        theta = (2.0 * math.pi * j) / num_meridians
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        for s in range(steps_meridian):
            phi1 = -math.pi / 2.0 + (math.pi * s) / steps_meridian
            phi2 = -math.pi / 2.0 + (math.pi * (s + 1)) / steps_meridian
            p1 = (math.cos(phi1) * cos_t, math.sin(phi1), math.cos(phi1) * sin_t)
            p2 = (math.cos(phi2) * cos_t, math.sin(phi2), math.cos(phi2) * sin_t)
            segments.append((p1, p2))

    return segments


def build_reference_ticks() -> list[tuple[float, float, bool]]:
    """Generate ticks every 3 degrees (minor length 0.006 H) and every 15 degrees (major length 0.012 H).

    Returns list of (angle_rad, tick_length_ratio, is_major).
    """
    ticks: list[tuple[float, float, bool]] = []
    for deg in range(0, 360, 3):
        ang = math.radians(deg)
        is_major = (deg % 15 == 0)
        tick_len = 0.012 if is_major else 0.006
        ticks.append((ang, tick_len, is_major))
    return ticks


def build_reference_sphere_curves(
    lat_step_deg: int = 15,
    pts_per_lat: int = 36,
    num_meridians: int = 16,
    pts_per_meridian: int = 24,
) -> tuple[list[list[tuple[float, float, float]]], list[list[tuple[float, float, float]]]]:
    """Generate 11 latitude closed curves and 16 meridian curves on the unit sphere.

    Total 27 curves, which split into at most 54 front/back polylines.
    """
    lat_curves: list[list[tuple[float, float, float]]] = []
    for lat_deg in range(-75, 90, lat_step_deg):
        phi = math.radians(lat_deg)
        r_lat = math.cos(phi)
        z_lat = math.sin(phi)
        pts: list[tuple[float, float, float]] = []
        for s in range(pts_per_lat + 1):
            theta = 2.0 * math.pi * (s % pts_per_lat) / pts_per_lat
            pts.append((r_lat * math.cos(theta), z_lat, r_lat * math.sin(theta)))
        lat_curves.append(pts)

    meridian_curves: list[list[tuple[float, float, float]]] = []
    for j in range(num_meridians):
        theta = 2.0 * math.pi * j / num_meridians
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        pts = []
        for s in range(pts_per_meridian + 1):
            phi = -math.pi / 2.0 + (math.pi * s) / pts_per_meridian
            pts.append((math.cos(phi) * cos_t, math.sin(phi), math.cos(phi) * sin_t))
        meridian_curves.append(pts)

    return lat_curves, meridian_curves


def split_curve_front_back(
    screen_pts: list[tuple[float, float, float]],
) -> tuple[list[float], list[float]]:
    """Split a 3D projected curve into at most one front polyline (z >= 0) and one back polyline (z < 0).

    Returns (front_coords, back_coords) as flat [x1, y1, x2, y2, ...] lists.
    Transitions across z=0 interpolate the horizon crossing point so curves connect smoothly.
    """
    front: list[float] = []
    back: list[float] = []
    n = len(screen_pts)
    for i in range(n - 1):
        x1, y1, z1 = screen_pts[i]
        x2, y2, z2 = screen_pts[i + 1]

        if z1 >= 0.0 and z2 >= 0.0:
            if not front:
                front.extend([x1, y1])
            front.extend([x2, y2])
        elif z1 < 0.0 and z2 < 0.0:
            if not back:
                back.extend([x1, y1])
            back.extend([x2, y2])
        else:
            denom = z2 - z1
            T = -z1 / denom if abs(denom) > 1e-9 else 0.5
            xz = x1 + T * (x2 - x1)
            yz = y1 + T * (y2 - y1)
            if z1 >= 0.0:
                if not front:
                    front.extend([x1, y1])
                front.extend([xz, yz])
                back.extend([xz, yz, x2, y2])
            else:
                if not back:
                    back.extend([x1, y1])
                back.extend([xz, yz])
                front.extend([xz, yz, x2, y2])

    return front, back


def compute_arc_quad_coords(
    cx: float,
    cy: float,
    r_inner: float,
    r_outer: float,
    start_deg: float,
    end_deg: float,
) -> list[float]:
    """Compute 4 quad vertices (8 coordinates) for an arc block polygon in screen space.

    Vertices order: inner start, outer start, outer end, inner end.
    Screen-up is positive (cy - r * sin(theta)).
    """
    th1 = math.radians(start_deg)
    th2 = math.radians(end_deg)

    p1_x = cx + r_inner * math.cos(th1)
    p1_y = cy - r_inner * math.sin(th1)

    p2_x = cx + r_outer * math.cos(th1)
    p2_y = cy - r_outer * math.sin(th1)

    p3_x = cx + r_outer * math.cos(th2)
    p3_y = cy - r_outer * math.sin(th2)

    p4_x = cx + r_inner * math.cos(th2)
    p4_y = cy - r_inner * math.sin(th2)

    return [p1_x, p1_y, p2_x, p2_y, p3_x, p3_y, p4_x, p4_y]


def compute_equal_advance_cells(
    text: str,
    cx: float,
    widest_glyph: float,
    tracking: float,
) -> list[float]:
    """Compute centered X coordinates for each character in equal advance cells.

    cell = widest_glyph + tracking
    Characters are placed at cx + (i - (N - 1) / 2) * cell.
    Symmetric about cx for any string.
    """
    n = len(text)
    if n == 0:
        return []
    cell = max(1.0, float(widest_glyph) + float(tracking))
    half_offset = (n - 1) / 2.0
    return [cx + (i - half_offset) * cell for i in range(n)]


@dataclass
class ParallaxParticle:
    norm_x: float  # [0.0, 1.0]
    norm_y: float  # [0.0, 1.0]
    speed_x: float  # normalized drift per second
    speed_y: float
    layer: int  # 0: deep (30), 1: mid (20), 2: near (10)
    size: float
    shade: str


def build_parallax_particles(rng: random.Random) -> list[ParallaxParticle]:
    """Generate 60 parallax dust particles (30 layer 0, 20 layer 1, 10 layer 2)."""
    particles: list[ParallaxParticle] = []
    layer_counts = [(0, 30, 0.003, 1.0, "#6E5C3E"), (1, 20, 0.006, 1.5, "#B29C71"), (2, 10, 0.012, 2.0, "#F2D8A1")]
    for layer, count, base_speed, size, shade in layer_counts:
        for _ in range(count):
            norm_x = rng.uniform(0.0, 1.0)
            norm_y = rng.uniform(0.0, 1.0)
            sx = base_speed * rng.uniform(0.7, 1.3)
            sy = base_speed * rng.uniform(-0.3, 0.3)
            particles.append(
                ParallaxParticle(
                    norm_x=norm_x,
                    norm_y=norm_y,
                    speed_x=sx,
                    speed_y=sy,
                    layer=layer,
                    size=size,
                    shade=shade,
                )
            )
    return particles


@dataclass
class HudGeometry:
    """Pre-computed geometric elements for the sci-fi HUD visual core."""

    sphere_segments: list[tuple[tuple[float, float, float], tuple[float, float, float]]]
    orbit_ring_points: list[list[tuple[float, float, float]]]
    particles: list[dict[str, Any]]
    radial_arcs: list[tuple[tuple[float, float, float], tuple[float, float, float]]]
    tick_marks: list[tuple[tuple[float, float], tuple[float, float]]]


def build_geometry(rng: random.Random) -> HudGeometry:
    """Generate wireframe sphere, orbit rings, radial arcs, and particles from seed."""
    sphere_segments: list[tuple[tuple[float, float, float], tuple[float, float, float]]] = []

    num_lats = 12
    num_lons = 24
    steps_per_circle = 36

    for i in range(1, num_lats + 1):
        phi = (math.pi * i) / (num_lats + 1)
        r_lat = math.sin(phi)
        z_lat = math.cos(phi)
        for s in range(steps_per_circle):
            theta1 = (2 * math.pi * s) / steps_per_circle
            theta2 = (2 * math.pi * (s + 1)) / steps_per_circle
            p1 = (r_lat * math.cos(theta1), r_lat * math.sin(theta1), z_lat)
            p2 = (r_lat * math.cos(theta2), r_lat * math.sin(theta2), z_lat)
            sphere_segments.append((p1, p2))

    for j in range(num_lons):
        theta = (2 * math.pi * j) / num_lons
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        for s in range(steps_per_circle // 2):
            phi1 = (math.pi * s) / (steps_per_circle // 2)
            phi2 = (math.pi * (s + 1)) / (steps_per_circle // 2)
            p1 = (math.sin(phi1) * cos_t, math.sin(phi1) * sin_t, math.cos(phi1))
            p2 = (math.sin(phi2) * cos_t, math.sin(phi2) * sin_t, math.cos(phi2))
            sphere_segments.append((p1, p2))

    ring_tilts = [
        (0.55, 0.20, 0.10, 1.25),
        (-0.40, 0.65, -0.30, 1.45),
        (0.25, -0.50, 0.70, 1.65),
    ]
    orbit_ring_points: list[list[tuple[float, float, float]]] = []
    particles: list[dict[str, Any]] = []

    for ring_idx, (tilt_x, tilt_y, tilt_z, r_mult) in enumerate(ring_tilts):
        pts: list[tuple[float, float, float]] = []
        ring_steps = 48
        for s in range(ring_steps + 1):
            ang = (2 * math.pi * (s % ring_steps)) / ring_steps
            p_flat = [(r_mult * math.cos(ang), r_mult * math.sin(ang), 0.0)]
            p_tilted = rotate(p_flat, tilt_x, tilt_y, tilt_z)[0]
            pts.append(p_tilted)
        orbit_ring_points.append(pts)

        for p_idx in range(150):
            base_phase = (2 * math.pi * p_idx) / 150.0 + rng.uniform(-0.02, 0.02)
            speed = rng.uniform(0.7, 1.3)
            size = rng.choice([2.0, 2.5, 3.0, 3.5])
            particles.append({
                "ring": ring_idx,
                "tilt": (tilt_x, tilt_y, tilt_z),
                "radius": r_mult,
                "base_radius": r_mult,
                "phase": base_phase,
                "speed": speed,
                "size": size,
            })

    radial_arcs: list[tuple[tuple[float, float, float], tuple[float, float, float]]] = []
    for k in range(32):
        if k % 4 == 0:
            continue
        ang = (2 * math.pi * k) / 32
        p1 = (1.15 * math.cos(ang), 1.15 * math.sin(ang), 0.0)
        p2 = (1.22 * math.cos(ang), 1.22 * math.sin(ang), 0.0)
        radial_arcs.append((p1, p2))

    for k in range(48):
        if k % 6 == 0:
            continue
        ang = (2 * math.pi * k) / 48
        p1 = (1.75 * math.cos(ang), 1.75 * math.sin(ang), 0.0)
        p2 = (1.85 * math.cos(ang), 1.85 * math.sin(ang), 0.0)
        radial_arcs.append((p1, p2))

    tick_marks: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for k in range(60):
        ang = (2 * math.pi * k) / 60
        r_inner = 1.92 if k % 5 == 0 else 1.95
        r_outer = 1.98
        t1 = (r_inner * math.cos(ang), r_inner * math.sin(ang))
        t2 = (r_outer * math.cos(ang), r_outer * math.sin(ang))
        tick_marks.append((t1, t2))

    return HudGeometry(
        sphere_segments=sphere_segments,
        orbit_ring_points=orbit_ring_points,
        particles=particles,
        radial_arcs=radial_arcs,
        tick_marks=tick_marks,
    )


class QualityGovernor:
    """Adaptive frame-rate monitor that drops fidelity when rendering slows."""

    TIERS = ("high", "medium", "low", "static")

    def __init__(self, clock: Callable[[], float]):
        self.clock = clock
        self._current_tier_idx = 0
        self._frame_times: deque[float] = deque(maxlen=20)
        self._last_downgrade_time: float = -999.0
        self._downgrades_count: int = 0

    @property
    def tier(self) -> str:
        return self.TIERS[self._current_tier_idx]

    @tier.setter
    def tier(self, val: str) -> None:
        val_str = str(val).lower().strip()
        if val_str in self.TIERS:
            self._current_tier_idx = self.TIERS.index(val_str)

    @property
    def average_frame_time(self) -> float:
        if not self._frame_times:
            return 0.0
        return sum(self._frame_times) / len(self._frame_times)

    @property
    def downgrades_count(self) -> int:
        return self._downgrades_count

    def record_frame(self, duration_s: float) -> None:
        self._frame_times.append(max(0.0, float(duration_s)))
        if len(self._frame_times) < 20:
            return

        avg = self.average_frame_time
        now = self.clock()

        if avg > 0.025 and self._current_tier_idx < len(self.TIERS) - 1:
            if now - self._last_downgrade_time >= 30.0:
                self._current_tier_idx += 1
                self._last_downgrade_time = now
                self._downgrades_count += 1
                self._frame_times.clear()


class HudModel:
    """Stateful simulation of the sci-fi HUD visual core."""

    def __init__(
        self,
        clock: Optional[Callable[[], float]] = None,
        rng: Optional[random.Random] = None,
    ):
        self.clock = clock or time.time
        self.rng = rng or random.Random(42)

        # Simulation inputs
        self._busy: bool = False
        self._listening: bool = False
        self._speaking: bool = False
        self._confirming: bool = False
        self._error_time: Optional[float] = None
        self._raw_mic_level: float = 0.0
        self._smooth_mic_level: float = 0.0

        # Animation state variables
        self.sphere_ax: float = 0.0
        self.sphere_ay: float = 0.0
        self.sphere_az: float = 0.0
        self.zoom_factor: float = 1.0
        self.ang_vel_x: float = 0.0
        self.ang_vel_y: float = 0.0
        self.target_lookat_x: float = 0.0
        self.target_lookat_y: float = 0.0
        self.lookat_x: float = 0.0
        self.lookat_y: float = 0.0
        self.cpu_load: Optional[float] = None
        self.mem_load: Optional[float] = None
        self.cpu_tooltip: str = ""
        self.mem_tooltip: str = ""
        self.rings_spin: float = 0.0
        self.core_pulse: float = 0.0
        self.elapsed: float = 0.0

        # Pulse envelope state
        self._pulse_start: Optional[float] = None
        self._pulse_strength: float = 1.0
        self._pulse_type: str = "full"
        self.pulse_val: float = 0.0
        self._last_heartbeat: float = 0.0

        # Continuous drift angles
        self.arc_a_drift_deg: float = 0.0
        self.arc_b_drift_deg: float = 0.0
        self.dust_orbit_deg: float = 0.0

        # Geometry & Governor
        self.geometry = build_geometry(self.rng)
        self.governor = QualityGovernor(self.clock)

        # Reference geometry collections
        self.ref_sphere_segments = build_reference_sphere_segments()
        self.ref_lat_curves, self.ref_meridian_curves = build_reference_sphere_curves()
        self.blocks_a, self.blocks_b = build_segmented_arcs(self.rng)
        self.dust_particles = build_dust_ring_particles(self.rng, count=350)
        self.dust_ring_soa = DustRingSoA(self.rng, count=600)
        self.static_stars = build_static_stars(self.rng, count=40)
        self.ref_ticks = build_reference_ticks()
        self.parallax_particles = build_parallax_particles(self.rng)

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)

    def set_listening(self, listening: bool) -> None:
        self._listening = bool(listening)

    def set_speaking(self, speaking: bool) -> None:
        self._speaking = bool(speaking)

    def set_confirming(self, confirming: bool) -> None:
        self._confirming = bool(confirming)

    def set_handsfree_armed(self, armed: bool) -> None:
        self._handsfree_armed = bool(armed)

    @property
    def handsfree_armed(self) -> bool:
        return getattr(self, "_handsfree_armed", False)

    def set_mic_level(self, level: float) -> None:
        lvl = max(0.0, min(1.0, float(level)))
        self._raw_mic_level = lvl
        self._smooth_mic_level += 0.3 * (lvl - self._smooth_mic_level)

    def flash_error(self) -> None:
        self._error_time = self.clock()

    def trigger_pulse(self, pulse_type: str = "full", strength: float = 1.0) -> None:
        """Trigger visual pulse envelope (full, mild, soft, heartbeat)."""
        self._pulse_start = self.clock()
        self._pulse_type = pulse_type
        self._pulse_strength = max(0.0, min(1.0, float(strength)))

    @property
    def pulse_type(self) -> str:
        return self._pulse_type

    @property
    def mic_level(self) -> float:
        return self._smooth_mic_level

    @property
    def tier(self) -> str:
        return self.governor.tier

    @tier.setter
    def tier(self, val: str) -> None:
        self.governor.tier = val

    @property
    def average_frame_time(self) -> float:
        return self.governor.average_frame_time

    @property
    def downgrades_count(self) -> int:
        return self.governor.downgrades_count

    @property
    def state(self) -> HudState:
        """Derive active state following strict priority rules:
        1. confirming (highest)
        2. error (decays after 1.5 s)
        3. speaking
        4. listening
        5. thinking (busy)
        6. idle (default)
        """
        if self._confirming:
            return HudState.CONFIRMING

        if self._error_time is not None:
            if (self.clock() - self._error_time) < 1.5:
                return HudState.ERROR
            self._error_time = None

        if self._speaking:
            return HudState.SPEAKING

        if self._listening:
            return HudState.LISTENING

        if self._busy:
            return HudState.THINKING

        return HudState.IDLE

    def reset_view(self) -> None:
        """Reset sphere rotation, look-at target, inertia, and zoom factor."""
        self.sphere_ax = 0.0
        self.sphere_ay = 0.0
        self.sphere_az = 0.0
        self.zoom_factor = 1.0
        self.ang_vel_x = 0.0
        self.ang_vel_y = 0.0
        self.target_lookat_x = 0.0
        self.target_lookat_y = 0.0
        self.lookat_x = 0.0
        self.lookat_y = 0.0

    def update(self, dt: float, motion_mode: str = "full") -> None:
        """Advance angles, pulses, and particle phases proportionally to dt."""
        dt = max(0.0, float(dt))
        self.elapsed += dt

        st = self.state

        # Update smoothed microphone with 80ms attack / 400ms decay
        self._smooth_mic_level = update_smoothed_mic(
            self._smooth_mic_level,
            self._raw_mic_level,
            dt,
            tau_attack=0.08,
            tau_decay=0.40,
        )

        # Pulse envelope update
        if self._pulse_start is not None:
            t_pulse = self.clock() - self._pulse_start
            raw_env = pulse_envelope_value(t_pulse, attack=0.25, decay=0.80)
            self.pulse_val = raw_env * self._pulse_strength
            if t_pulse >= 1.05:
                self._pulse_start = None
                self.pulse_val = 0.0
        else:
            self.pulse_val = 0.0

        # Idle heartbeat every 8.0s when motion is full
        if motion_mode == "full" and st == HudState.IDLE:
            if self.elapsed - self._last_heartbeat >= 8.0:
                self.trigger_pulse("heartbeat", 0.25)
                self._last_heartbeat = self.elapsed

        # Sphere rotation speeds based on state
        if st == HudState.THINKING:
            ax_speed = 0.8
            ay_speed = 1.6
            ring_speed = 2.4
        elif st == HudState.SPEAKING:
            ax_speed = 0.35
            ay_speed = 0.70
            ring_speed = 1.10
        elif st == HudState.LISTENING:
            ax_speed = 0.25
            ay_speed = 0.50
            ring_speed = 0.80 + self._smooth_mic_level * 1.5
        elif st == HudState.CONFIRMING:
            ax_speed = 0.05
            ay_speed = 0.10
            ring_speed = 0.15
        elif st == HudState.ERROR:
            ax_speed = 0.10
            ay_speed = 0.20
            ring_speed = 0.30
        else:  # IDLE
            ax_speed = 0.20
            ay_speed = 0.40
            ring_speed = 0.60

        if motion_mode == "full":
            decay = math.exp(-3.0 * dt)
            self.ang_vel_x *= decay
            self.ang_vel_y *= decay
            self.sphere_ax = (self.sphere_ax + (ax_speed + self.ang_vel_x) * dt) % (2 * math.pi)
            self.sphere_ay = (self.sphere_ay + (ay_speed + self.ang_vel_y) * dt) % (2 * math.pi)
            # Look-at smoothing over 300 ms (tau = 0.3)
            alpha_look = 1.0 - math.exp(-dt / 0.3)
            self.lookat_x += (self.target_lookat_x - self.lookat_x) * alpha_look
            self.lookat_y += (self.target_lookat_y - self.lookat_y) * alpha_look
        else:
            self.ang_vel_x = 0.0
            self.ang_vel_y = 0.0
            self.target_lookat_x = 0.0
            self.target_lookat_y = 0.0
            self.lookat_x = 0.0
            self.lookat_y = 0.0
            if motion_mode == "reduced":
                self.sphere_ax = (self.sphere_ax + ax_speed * 0.3 * dt) % (2 * math.pi)
                self.sphere_ay = (self.sphere_ay + ay_speed * 0.3 * dt) % (2 * math.pi)
        self.rings_spin = (self.rings_spin + ring_speed * dt) % (2 * math.pi)

        # Drift angles: Arc A +3 deg/s, Arc B -2 deg/s, Dust orbit 6 deg/s
        self.arc_a_drift_deg = (self.arc_a_drift_deg + 3.0 * dt) % 360.0
        self.arc_b_drift_deg = (self.arc_b_drift_deg - 2.0 * dt) % 360.0
        self.dust_orbit_deg = (self.dust_orbit_deg + 6.0 * dt) % 360.0
        self.dust_ring_soa.update(dt)

        # Core pulse calculation
        if st == HudState.SPEAKING:
            self.core_pulse = 0.5 + 0.5 * math.sin(2 * math.pi * 3.0 * self.elapsed)
        elif st == HudState.LISTENING:
            self.core_pulse = max(0.1, min(1.0, self._smooth_mic_level * 1.2))
        elif st == HudState.CONFIRMING:
            self.core_pulse = 0.3
        elif st == HudState.ERROR:
            decay = max(0.0, 1.0 - ((self.clock() - (self._error_time or 0.0)) / 1.5))
            self.core_pulse = decay
        else:  # IDLE / THINKING
            self.core_pulse = 0.35 + 0.25 * math.sin(2 * math.pi * 0.5 * self.elapsed)

        # Update parallax particles with wraparound
        motion_speed = 0.0 if motion_mode == "off" else (0.4 if motion_mode == "reduced" else 1.0)
        for part in self.parallax_particles:
            part.norm_x = (part.norm_x + part.speed_x * dt * motion_speed) % 1.0
            part.norm_y = (part.norm_y + part.speed_y * dt * motion_speed) % 1.0

        # Particle phase advancement for legacy geometry
        tier = self.governor.tier
        max_particles = 400 if tier == "high" else (200 if tier == "medium" else (100 if tier == "low" else 0))
        for p in self.geometry.particles[:max_particles]:
            p_speed = p["speed"]
            if st == HudState.THINKING:
                p["phase"] = (p["phase"] + p_speed * ring_speed * 1.5 * dt) % (2 * math.pi)
                p["radius"] = p["base_radius"] * (0.85 + 0.15 * math.cos(p["phase"]))
            else:
                p["phase"] = (p["phase"] + p_speed * ring_speed * dt) % (2 * math.pi)
                p["radius"] = p["base_radius"]
