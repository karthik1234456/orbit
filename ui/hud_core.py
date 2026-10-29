"""Pure Python mathematical model, 3D projection, and state machine for ORBIT HUD.

Strictly isolated from GUI frameworks: NO tkinter or UI dependencies.
Implements:
- HudState priority hierarchy (confirming > error > speaking > listening > thinking > idle)
- 3D wireframe sphere and orbit ring geometry generated from deterministic seed
- 3D rotation, perspective projection, depth-based color interpolation
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
        # Rotate around X
        y1 = y * cx - z * sx
        z1 = y * sx + z * cx

        # Rotate around Y
        x2 = x * cy + z1 * sy
        z2 = -x * sy + z1 * cy

        # Rotate around Z
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

        # Perspective factor: camera looking along +Z towards origin
        dist = focal + world_z
        if dist <= 1.0:
            dist = 1.0
        scale = focal / dist

        sx = cx + world_x * scale
        sy = cy + world_y * scale
        projected.append((sx, sy, z))
    return projected


def _hex_to_rgb(hex_code: str) -> tuple[int, int, int]:
    """Parse #rrggbb into RGB integer tuple."""
    clean = hex_code.lstrip("#")
    if len(clean) == 3:
        clean = "".join(c * 2 for c in clean)
    if len(clean) != 6:
        return (0, 229, 255)
    return (int(clean[0:2], 16), int(clean[2:4], 16), int(clean[4:6], 16))


def depth_color(
    depth: float,
    palette: Any,
    min_z: float = -1.0,
    max_z: float = 1.0,
) -> str:
    """Interpolate color between palette.accent (front) and palette.accent_dim/bg (back)."""
    # Normalize depth to [0.0 (far), 1.0 (near)]
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
class HudGeometry:
    """Pre-computed geometric elements for the sci-fi HUD visual core."""

    # List of segments: [((x1, y1, z1), (x2, y2, z2)), ...]
    sphere_segments: list[tuple[tuple[float, float, float], tuple[float, float, float]]]
    # 3 orbit rings: each is a list of points
    orbit_ring_points: list[list[tuple[float, float, float]]]
    # Orbit ring particles: list of base particle phases and offsets
    particles: list[dict[str, Any]]
    # Radial tick arc segments: [((x1, y1, z1), (x2, y2, z2)), ...]
    radial_arcs: list[tuple[tuple[float, float, float], tuple[float, float, float]]]
    # Concentric boundary tick marks: [((x1, y1), (x2, y2)), ...]
    tick_marks: list[tuple[tuple[float, float], tuple[float, float]]]


def build_geometry(rng: random.Random) -> HudGeometry:
    """Generate wireframe sphere, orbit rings, radial arcs, and particles from seed."""
    sphere_segments: list[tuple[tuple[float, float, float], tuple[float, float, float]]] = []

    # 1. Wireframe sphere: 12 latitude circles, 24 longitude lines
    num_lats = 12
    num_lons = 24
    steps_per_circle = 36

    # Latitudes
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

    # Longitudes
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

    # 2. 3 Tilted Orbit Rings
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

        # Allocate 150 particles per ring (pool for up to 450 max)
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

    # 3. 2 Rings of segmented radial arcs
    radial_arcs: list[tuple[tuple[float, float, float], tuple[float, float, float]]] = []
    # Inner segmented arc (radius 1.15 to 1.22, 32 ticks)
    for k in range(32):
        if k % 4 == 0:
            continue  # Segment gap
        ang = (2 * math.pi * k) / 32
        p1 = (1.15 * math.cos(ang), 1.15 * math.sin(ang), 0.0)
        p2 = (1.22 * math.cos(ang), 1.22 * math.sin(ang), 0.0)
        radial_arcs.append((p1, p2))

    # Outer segmented arc (radius 1.75 to 1.85, 48 ticks)
    for k in range(48):
        if k % 6 == 0:
            continue
        ang = (2 * math.pi * k) / 48
        p1 = (1.75 * math.cos(ang), 1.75 * math.sin(ang), 0.0)
        p2 = (1.85 * math.cos(ang), 1.85 * math.sin(ang), 0.0)
        radial_arcs.append((p1, p2))

    # 4. Concentric boundary tick marks (2D circle ticks)
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
        self._current_tier_idx = 0  # 0: high, 1: medium, 2: low, 3: static
        self._frame_times: deque[float] = deque(maxlen=20)
        self._last_downgrade_time: float = -999.0
        self._downgrades_count: int = 0

    @property
    def tier(self) -> str:
        return self.TIERS[self._current_tier_idx]

    @property
    def average_frame_time(self) -> float:
        """Average frame time over the last 20 frames in seconds."""
        if not self._frame_times:
            return 0.0
        return sum(self._frame_times) / len(self._frame_times)

    @property
    def downgrades_count(self) -> int:
        return self._downgrades_count

    def record_frame(self, duration_s: float) -> None:
        """Record duration of a rendered frame and evaluate downgrade criteria."""
        self._frame_times.append(max(0.0, float(duration_s)))
        if len(self._frame_times) < 20:
            return

        avg = self.average_frame_time
        now = self.clock()

        # Threshold: 25ms average frame time (> 0.025s) triggers downgrade
        if avg > 0.025 and self._current_tier_idx < len(self.TIERS) - 1:
            # 30-second cooldown hysteresis
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
        self.rings_spin: float = 0.0
        self.core_pulse: float = 0.0
        self.elapsed: float = 0.0

        # Geometry & Governor
        self.geometry = build_geometry(self.rng)
        self.governor = QualityGovernor(self.clock)

    # --- Inputs ---

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)

    def set_listening(self, listening: bool) -> None:
        self._listening = bool(listening)

    def set_speaking(self, speaking: bool) -> None:
        self._speaking = bool(speaking)

    def set_confirming(self, confirming: bool) -> None:
        self._confirming = bool(confirming)

    def set_mic_level(self, level: float) -> None:
        lvl = max(0.0, min(1.0, float(level)))
        self._raw_mic_level = lvl
        # Exponential smoothing (alpha = 0.3)
        self._smooth_mic_level += 0.3 * (lvl - self._smooth_mic_level)

    def flash_error(self) -> None:
        self._error_time = self.clock()

    # --- Properties & Derived State ---

    @property
    def mic_level(self) -> float:
        return self._smooth_mic_level

    @property
    def tier(self) -> str:
        return self.governor.tier

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

    # --- Physics & Delta-Time Motion ---

    def update(self, dt: float) -> None:
        """Advance angles, pulses, and particle phases proportionally to dt."""
        dt = max(0.0, float(dt))
        self.elapsed += dt

        st = self.state

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
            # Slow calm hold
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

        self.sphere_ax = (self.sphere_ax + ax_speed * dt) % (2 * math.pi)
        self.sphere_ay = (self.sphere_ay + ay_speed * dt) % (2 * math.pi)
        self.rings_spin = (self.rings_spin + ring_speed * dt) % (2 * math.pi)

        # Core pulse calculation
        if st == HudState.SPEAKING:
            # Fixed 3 Hz rhythm
            self.core_pulse = 0.5 + 0.5 * math.sin(2 * math.pi * 3.0 * self.elapsed)
        elif st == HudState.LISTENING:
            # Pulse with smoothed mic level
            self.core_pulse = max(0.1, min(1.0, self._smooth_mic_level * 1.2))
        elif st == HudState.CONFIRMING:
            self.core_pulse = 0.3
        elif st == HudState.ERROR:
            # Red warning pulse
            decay = max(0.0, 1.0 - ((self.clock() - (self._error_time or 0.0)) / 1.5))
            self.core_pulse = decay
        else:  # IDLE / THINKING
            # Gentle 0.5 Hz breathing cycle
            self.core_pulse = 0.35 + 0.25 * math.sin(2 * math.pi * 0.5 * self.elapsed)

        # Particle phase advancement
        tier = self.governor.tier
        max_particles = 400 if tier == "high" else (200 if tier == "medium" else (100 if tier == "low" else 0))
        for i, p in enumerate(self.geometry.particles[:max_particles]):
            p_speed = p["speed"]
            if st == HudState.THINKING:
                # Particles swirl faster inward
                p["phase"] = (p["phase"] + p_speed * ring_speed * 1.5 * dt) % (2 * math.pi)
                p["radius"] = p["base_radius"] * (0.85 + 0.15 * math.cos(p["phase"]))
            else:
                p["phase"] = (p["phase"] + p_speed * ring_speed * dt) % (2 * math.pi)
                p["radius"] = p["base_radius"]

