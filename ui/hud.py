"""High-tech HUD canvas for ORBIT desktop assistant.

Implements:
- Tkinter Canvas visualization of HudModel with pre-allocated item pools
- Frame-rate bounded animation loop with delta-time calculation
- Exception-safe reschedule in finally block and after_cancel teardown
- Automatic pause when unmapped/minimized (<Unmap> / <Map>)
- Debounced resize re-layout (100 ms)
- Motion modes: full (30 FPS default), reduced (~8 FPS, no particles), off (static)
- Centered state status label and branded title banner
- Quality governor integration
"""

from __future__ import annotations

import math
import time
import tkinter as tk
from typing import Any, Optional

from core.version import VERSION
from ui.hud_core import HudModel, HudState, depth_color, project, rotate
from ui.theme import Palette, get_palette


class HudCanvas(tk.Canvas):
    """Sci-fi interactive HUD visualization canvas for the ORBIT desktop agent."""

    def __init__(
        self,
        parent: tk.Widget,
        model: HudModel,
        palette: Optional[Palette] = None,
        fps: int = 30,
        motion: str = "full",
        **kwargs: Any,
    ) -> None:
        self.palette: Palette = palette or get_palette("cyan")
        self.model = model
        self.fps = max(10, min(60, int(fps)))
        self.motion = motion.lower().strip() if motion else "full"
        if self.motion not in ("full", "reduced", "off"):
            self.motion = "full"

        # Apply canvas styling
        kwargs.setdefault("bg", self.palette.bg)
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("bd", 0)
        super().__init__(parent, **kwargs)

        # Animation timing and control
        self._destroyed = False
        self._paused = False
        self._after_id: Optional[str] = None
        self._debounce_id: Optional[str] = None
        self._last_time = time.monotonic()
        self._last_state: Optional[HudState] = None

        # Boot animation (up to 2.0 seconds in full motion)
        self._boot_done = self.motion != "full"
        self._boot_start_time = time.monotonic() if not self._boot_done else 0.0

        # Geometry scaling cache
        self._width = max(200, int(kwargs.get("width", 360)))
        self._height = max(180, int(kwargs.get("height", 240)))

        # Pre-allocated Item Pools
        self._pool_created = False
        self._title_id: Optional[int] = None
        self._title_line_id: Optional[int] = None
        self._state_label_id: Optional[int] = None
        self._boot_text_id: Optional[int] = None
        self._outer_circles: list[int] = []
        self._tick_items: list[int] = []
        self._radial_arc_items: list[int] = []
        self._sphere_line_items: list[int] = []
        self._core_glow_items: list[int] = []
        self._particle_items: list[int] = []

        # Previous state coordinate cache to minimize canvas updates
        self._last_coords_cache: dict[int, tuple[float, ...]] = {}

        # Wire canvas events
        self.bind("<Configure>", self._on_configure)
        self.bind("<Map>", self._on_map)
        self.bind("<Unmap>", self._on_unmap)
        self.bind("<Destroy>", self._on_destroy)

        self._setup_item_pool()

        # Start animation loop if not off
        if self.motion != "off":
            self._schedule_tick(10)
        else:
            self._draw_frame(dt=0.0)

    # --- Window & Lifecycle Handlers ---

    def _on_destroy(self, event: Optional[tk.Event] = None) -> None:
        """Cancel pending animation loops upon widget destruction."""
        self._destroyed = True
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        if self._debounce_id is not None:
            try:
                self.after_cancel(self._debounce_id)
            except Exception:
                pass
            self._debounce_id = None

    def _on_unmap(self, event: Optional[tk.Event] = None) -> None:
        """Pause rendering when window is minimized or unmapped."""
        self._paused = True

    def _on_map(self, event: Optional[tk.Event] = None) -> None:
        """Resume rendering when window is restored and mapped."""
        self._paused = False
        self._last_time = time.monotonic()
        if self.motion != "off" and self._after_id is None:
            self._schedule_tick(10)

    def _on_configure(self, event: tk.Event) -> None:
        """Handle canvas resize with 100ms debounce."""
        if event.width > 20 and event.height > 20:
            self._width = event.width
            self._height = event.height
            if self._debounce_id is not None:
                try:
                    self.after_cancel(self._debounce_id)
                except Exception:
                    pass
            self._debounce_id = self.after(100, self._apply_resize)

    def _apply_resize(self) -> None:
        self._debounce_id = None
        if not self._destroyed:
            self._draw_frame(dt=0.0)

    # --- Item Pool Setup ---

    def _setup_item_pool(self) -> None:
        """Instantiate all visual Canvas elements once. Subsequent frames reuse items."""
        if self._pool_created:
            return
        self._pool_created = True

        # 1. Concentric circles & tick marks
        for _ in range(2):
            cid = self.create_oval(0, 0, 0, 0, outline=self.palette.panel_edge, width=1)
            self._outer_circles.append(cid)

        for _ in self.model.geometry.tick_marks:
            tid = self.create_line(0, 0, 0, 0, fill=self.palette.accent_dim, width=1)
            self._tick_items.append(tid)

        # 2. Radial arcs
        for _ in self.model.geometry.radial_arcs:
            rid = self.create_line(0, 0, 0, 0, fill=self.palette.accent_dim, width=1)
            self._radial_arc_items.append(rid)

        # 3. Sphere segments
        for _ in self.model.geometry.sphere_segments:
            sid = self.create_line(0, 0, 0, 0, fill=self.palette.accent_dim, width=1)
            self._sphere_line_items.append(sid)

        # 4. Core glow (5 concentric circles)
        for _ in range(5):
            gid = self.create_oval(0, 0, 0, 0, outline=self.palette.accent_dim, width=1)
            self._core_glow_items.append(gid)

        # 5. Particles (up to 400 for high tier)
        for _ in range(400):
            pid = self.create_oval(0, 0, 0, 0, fill=self.palette.accent, outline="", state="hidden")
            self._particle_items.append(pid)

        # 6. Title and state text
        self._title_id = self.create_text(
            12, 12,
            text=f"ORBIT  v{VERSION}",
            anchor="nw",
            fill=self.palette.text_dim,
            font=("Segoe UI", 8, "bold"),
        )
        self._title_line_id = self.create_line(0, 0, 0, 0, fill=self.palette.panel_edge, width=1)

        self._state_label_id = self.create_text(
            0, 0,
            text="STANDBY",
            anchor="center",
            fill=self.palette.text_dim,
            font=("Consolas", 9, "bold"),
        )

        self._boot_text_id = self.create_text(
            0, 0,
            text="ORBIT ONLINE",
            anchor="center",
            fill=self.palette.accent,
            font=("Segoe UI", 12, "bold"),
            state="normal" if not self._boot_done else "hidden",
        )

    # --- Animation Tick Loop ---

    def _schedule_tick(self, delay_ms: int) -> None:
        if self._destroyed:
            return
        try:
            self._after_id = self.after(delay_ms, self._tick)
        except Exception:
            self._after_id = None

    def _tick(self) -> None:
        """Execute one rendering step and ALWAYS reschedule in finally block."""
        self._after_id = None
        if self._destroyed:
            return

        target_fps = 8 if self.motion == "reduced" else self.fps
        interval_ms = max(16, int(1000.0 / target_fps))

        start_time = time.monotonic()
        try:
            now = start_time
            dt = max(0.001, min(0.1, now - self._last_time))
            self._last_time = now

            if not self._paused:
                self.model.update(dt)
                self._draw_frame(dt)
        finally:
            frame_duration = time.monotonic() - start_time
            self.model.governor.record_frame(frame_duration)

            if not self._destroyed and self.motion != "off":
                next_delay = max(5, interval_ms - int(frame_duration * 1000))
                self._schedule_tick(next_delay)

    # --- Frame Drawing & Projection ---

    def _update_coords(self, item_id: int, *coords: float) -> None:
        """Helper to invoke coords only when values differ from cached values."""
        rounded = tuple(round(c, 1) for c in coords)
        if self._last_coords_cache.get(item_id) != rounded:
            self.coords(item_id, *coords)
            self._last_coords_cache[item_id] = rounded

    def _draw_frame(self, dt: float) -> None:
        """Render projected geometry, rings, particles, and state labels."""
        w = max(100, self._width)
        h = max(100, self._height)
        cx = w / 2.0
        cy = h / 2.0 - 10.0
        sphere_r = max(24.0, min(w, h) * 0.22)

        # 1. Title bar & border line
        if self._title_line_id is not None:
            self._update_coords(self._title_line_id, 0, 24, w, 24)

        # 2. Concentric boundary circles
        if len(self._outer_circles) >= 2:
            r1 = sphere_r * 1.95
            r2 = sphere_r * 1.98
            self._update_coords(self._outer_circles[0], cx - r1, cy - r1, cx + r1, cy + r1)
            self._update_coords(self._outer_circles[1], cx - r2, cy - r2, cx + r2, cy + r2)

        # 3. Outer boundary tick marks
        for (p1, p2), item_id in zip(self.model.geometry.tick_marks, self._tick_items):
            x1 = cx + p1[0] * sphere_r
            y1 = cy + p1[1] * sphere_r
            x2 = cx + p2[0] * sphere_r
            y2 = cy + p2[1] * sphere_r
            self._update_coords(item_id, x1, y1, x2, y2)

        # 4. Radial arcs
        st = self.model.state
        arc_color = self.palette.warn if st == HudState.CONFIRMING else (
            self.palette.danger if st == HudState.ERROR else self.palette.accent_dim
        )
        for (p1, p2), item_id in zip(self.model.geometry.radial_arcs, self._radial_arc_items):
            x1 = cx + p1[0] * sphere_r
            y1 = cy + p1[1] * sphere_r
            x2 = cx + p2[0] * sphere_r
            y2 = cy + p2[1] * sphere_r
            self._update_coords(item_id, x1, y1, x2, y2)
            self.itemconfigure(item_id, fill=arc_color)

        # 5. Wireframe sphere rotation & projection
        rot_pts: list[tuple[float, float, float]] = []
        for p1, p2 in self.model.geometry.sphere_segments:
            rot_pts.extend([p1, p2])

        rotated_pts = rotate(rot_pts, self.model.sphere_ax, self.model.sphere_ay, self.model.sphere_az)
        projected = project(rotated_pts, cx, cy, sphere_r, focal=320.0)

        tier = self.model.tier
        is_low = tier in ("low", "static")

        for idx, item_id in enumerate(self._sphere_line_items):
            if is_low and (idx % 3 != 0):
                self.itemconfigure(item_id, state="hidden")
                continue
            self.itemconfigure(item_id, state="normal")

            p1 = projected[idx * 2]
            p2 = projected[idx * 2 + 1]
            self._update_coords(item_id, p1[0], p1[1], p2[0], p2[1])

            avg_z = (p1[2] + p2[2]) / 2.0
            col = depth_color(avg_z, self.palette)
            self.itemconfigure(item_id, fill=col)

        # 6. Core glow layers
        pulse = self.model.core_pulse
        core_color = self.palette.warn if st == HudState.CONFIRMING else (
            self.palette.danger if st == HudState.ERROR else self.palette.accent
        )
        for i, item_id in enumerate(self._core_glow_items):
            if is_low:
                self.itemconfigure(item_id, state="hidden")
                continue
            r_glow = sphere_r * (0.12 + 0.08 * i) * (1.0 + 0.25 * pulse)
            self.itemconfigure(item_id, state="normal", outline=core_color)
            self._update_coords(item_id, cx - r_glow, cy - r_glow, cx + r_glow, cy + r_glow)

        # 7. Orbit particles
        show_particles = self.motion == "full" and tier in ("high", "medium")
        max_p = 400 if tier == "high" else 200

        for i, item_id in enumerate(self._particle_items):
            if not show_particles or i >= max_p:
                self.itemconfigure(item_id, state="hidden")
                continue

            p_data = self.model.geometry.particles[i]
            phase = p_data["phase"]
            r_val = p_data["radius"]
            tilt = p_data["tilt"]

            # Position in tilted ring plane
            p_flat = [(r_val * math.cos(phase), r_val * math.sin(phase), 0.0)]
            p_tilted = rotate(p_flat, tilt[0], tilt[1], tilt[2] + self.model.rings_spin)[0]
            proj_p = project([p_tilted], cx, cy, sphere_r, focal=320.0)[0]

            psz = p_data["size"]
            self.itemconfigure(item_id, state="normal", fill=core_color)
            self._update_coords(item_id, proj_p[0] - psz, proj_p[1] - psz, proj_p[0] + psz, proj_p[1] + psz)

        # 8. State status label
        state_labels = {
            HudState.IDLE: ("STANDBY", self.palette.text_dim),
            HudState.LISTENING: ("LISTENING", self.palette.accent),
            HudState.THINKING: ("PROCESSING", self.palette.accent),
            HudState.SPEAKING: ("SPEAKING", self.palette.ok),
            HudState.CONFIRMING: ("AWAITING APPROVAL", self.palette.warn),
            HudState.ERROR: ("FAULT", self.palette.danger),
        }
        text_str, text_col = state_labels.get(st, ("STANDBY", self.palette.text_dim))
        label_y = cy + sphere_r * 1.55
        if self._state_label_id is not None:
            self._update_coords(self._state_label_id, cx, label_y)
            self.itemconfigure(self._state_label_id, text=text_str, fill=text_col)

        # 9. Boot animation text
        if not self._boot_done and self._boot_text_id is not None:
            now_boot = time.monotonic()
            if now_boot - self._boot_start_time > 1.8:
                self._boot_done = True
                self.itemconfigure(self._boot_text_id, state="hidden")
            else:
                self._update_coords(self._boot_text_id, cx, cy)
                self.itemconfigure(self._boot_text_id, state="normal")

    def set_palette(self, palette: Palette) -> None:
        """Update active color palette and redraw immediately."""
        self.palette = palette
        self.configure(bg=palette.bg)
        self._last_coords_cache.clear()
        self._draw_frame(dt=0.0)

