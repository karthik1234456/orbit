"""High-tech HUD canvas for ORBIT desktop assistant.

Implements:
- Tkinter Canvas visualization of reference gold/cyan HUD
- Single-source layout(W, H) geometry derivation
- Orthographic sphere projection (all points strictly within r_s + 0.5 px)
- 54-polyline sphere wireframe (at most 2 polylines per latitude/meridian: front and back)
- 5-layer concentric core drawn largest-first (stepping from #B29C71 to #FADEA4 at center)
- Core tiers never collapse to one disc (low/static uses 3 layers)
- Segmented arc quads with create_polygon in band 0.285 H to 0.310 H with bell brightness
- Motion modes: full (30 FPS), reduced (8 FPS, half particles, slower motion), off (1 static frame)
- Title equal advance cells ("cell = widest glyph + tracking") with symmetric placement
- Item caching and touched-item counters (max 450 items touched on tier high)
- Realism system (full | lite | off):
  - Deterministic background bitmap (radial vignette, haze, floor grid, reflection, grain)
  - Pre-rendered bloom halo sprites and translucent glass sphere body via PPM PhotoImages
  - 3 depth layers of parallax dust specks shifted toward cursor (0.01, 0.02, 0.04 W)
  - Clean vector fallback on PhotoImage error
"""

from __future__ import annotations

import math
import threading
import time
import tkinter as tk
from typing import Any, Optional

from config import settings
from ui.hud_bitmap import (
    build_background_bitmap,
    build_bloom_sprites,
    build_sphere_body_sprite,
)
from ui.hud_core import (
    HudModel,
    HudState,
    adjust_color_brightness,
    clamp_zoom_factor,
    compute_arc_quad_coords,
    compute_equal_advance_cells,
    layout,
    project_orthographic,
    rotate,
    split_curve_front_back,
)
from ui.radial_menu import RadialAction, RadialMenu
from ui.theme import Palette, get_heading_font, get_palette


from dataclasses import dataclass

@dataclass
class HudNotice:
    """Transient notification displayed under the HUD title rule."""
    text: str
    created_at: float
    lifetime: float = 3.0


class HudCanvas(tk.Canvas):
    """Sci-fi interactive HUD visualization canvas for the ORBIT desktop agent."""

    def __init__(
        self,
        parent: tk.Widget,
        model: HudModel,
        palette: Optional[Palette] = None,
        fps: int = 30,
        motion: str = "full",
        mode_label: str = "LOCAL qwen3:8b",
        doctor_label: str = "ALL SYSTEMS NOMINAL",
        realism: Optional[str] = None,
        interactive: Optional[bool] = None,
        is_dialog_open: Optional[Any] = None,
        is_input_focused: Optional[Any] = None,
        on_mic: Optional[Any] = None,
        on_radial_menu: Optional[Any] = None,
        on_shortcuts: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        self.palette: Palette = palette or get_palette("cyan")
        self.model = model
        self.fps = max(10, min(60, int(fps)))
        self.motion = motion.lower().strip() if motion else "full"
        if self.motion not in ("full", "reduced", "off"):
            self.motion = "full"

        self.mode_label = mode_label
        self.doctor_label = doctor_label
        self.chat_expanded = False

        # Realism setting (full | lite | off)
        if realism is not None:
            self.realism = str(realism).lower().strip()
        else:
            self.realism = str(getattr(settings, "UI_REALISM", "full")).lower().strip()
        if self.realism not in ("full", "lite", "off"):
            self.realism = "full"

        kwargs.setdefault("bg", self.palette.bg)
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("bd", 0)
        super().__init__(parent, **kwargs)

        # Animation timing
        self._destroyed = False
        self._paused = False
        self._after_id: Optional[str] = None
        self._debounce_id: Optional[str] = None
        self._realism_debounce_id: Optional[str] = None
        self._last_time = time.monotonic()
        self._last_frame_duration = 0.0

        # Optimization & Telemetry counters
        self.items_touched_last_frame: int = 0
        self._items_touched_this_frame: int = 0
        self._items_touched_history: list[int] = []
        self.items_touched_average: float = 0.0

        # Coordinate & Config Caches to eliminate redundant Tk calls
        self._last_coords_cache: dict[int, tuple[float, ...]] = {}
        self._last_config_cache: dict[int, dict[str, Any]] = {}

        # Boot animation
        self._boot_done = self.motion != "full"
        self._boot_start_time = time.monotonic() if not self._boot_done else 0.0

        # Dimensions & Cursor tracking
        self._width = max(200, int(kwargs.get("width", 1280)))
        self._height = max(180, int(kwargs.get("height", 800)))
        self._cursor_x: float = self._width / 2.0
        self._cursor_y: float = self._height / 2.0

        # Realism Bitmaps & Sprites state
        self._bg_photo: Optional[tk.PhotoImage] = None
        self._bloom_photos: list[tk.PhotoImage] = []
        self._sphere_body_photo: Optional[tk.PhotoImage] = None
        self._last_bloom_level: int = -1
        self.realism_fallback_reason: str = ""
        self.realism_build_time_ms: float = 0.0
        self.realism_sprite_count: int = 0
        self._realism_worker_busy: bool = False

        # Interaction and Telemetry Setup
        if interactive is not None:
            self.interactive = bool(interactive)
        else:
            self.interactive = bool(getattr(settings, "UI_INTERACTIVE", 1))

        self.is_dialog_open = is_dialog_open
        self.is_input_focused = is_input_focused
        self.on_mic = on_mic
        self.on_radial_menu = on_radial_menu
        self.on_shortcuts = on_shortcuts
        self._is_blocked_callback: Optional[Any] = None

        self.drag_count: int = 0
        self.zoom_factor: float = 1.0
        self.radial_menu_opens: int = 0
        self.notices_count: int = 0
        self._notices: list[HudNotice] = []
        self._is_dragging: bool = False
        self._has_dragged: bool = False
        self._drag_start_x: float = 0.0
        self._drag_start_y: float = 0.0
        self._drag_last_x: float = 0.0
        self._drag_last_y: float = 0.0
        self._drag_last_time: float = time.monotonic()
        self._drag_distance: float = 0.0

        self._focus_ring_id: Optional[int] = None
        self._arc_tooltip_bg_id: Optional[int] = None
        self._arc_tooltip_id: Optional[int] = None
        self._notice_items: list[int] = []

        self._radial_menu = RadialMenu(self, palette=self.palette)
        self._telemetry_timer_id: Optional[str] = None

        # Item Pools
        self._pool_created = False
        self._bg_image_id: Optional[int] = None
        self._vignette_items: list[int] = []
        self._parallax_deep_items: list[int] = []
        self._parallax_mid_items: list[int] = []
        self._parallax_near_items: list[int] = []
        self._panel_cards: list[int] = []

        # Typography items
        self._title_chars: list[int] = []
        self._title_rule_id: Optional[int] = None
        self._state_label_id: Optional[int] = None
        self._boot_text_id: Optional[int] = None

        # Bottom chips
        self._chips_line_id: Optional[int] = None
        self._chip_left_bg: Optional[int] = None
        self._chip_left_text: Optional[int] = None
        self._chip_right_bg: Optional[int] = None
        self._chip_right_text: Optional[int] = None

        # Double ring & ticks
        self._double_rings: list[int] = []
        self._tick_items: list[int] = []

        # Segmented arcs (Arc A: 24 quads, Arc B: 20 quads)
        self._arc_a_items: list[int] = []
        self._arc_b_items: list[int] = []

        # Dust ring & Ellipse (back and front)
        self._dust_back_items: list[int] = []
        self._dust_back_ellipse_id: Optional[int] = None
        self._bloom_image_id: Optional[int] = None
        self._sphere_body_image_id: Optional[int] = None

        # Core concentric circles (5 layers, largest first)
        self._core_circle_items: list[int] = []
        self._limb_glow_items: list[int] = []
        self._limb_ring_id: Optional[int] = None

        # Wireframe sphere lines (54 polylines: 27 front, 27 back)
        self._sphere_front_items: list[int] = []
        self._sphere_back_items: list[int] = []
        self._sphere_line_items: list[int] = []

        self._dust_front_items: list[int] = []
        self._dust_front_ellipse_id: Optional[int] = None

        # Legacy aliases for backward compatibility tests
        self._outer_circles: list[int] = []
        self._core_glow_items: list[int] = []
        self._particle_items: list[int] = []
        self._radial_arc_items: list[int] = []

        # Wire events & focus
        self.configure(takefocus=True)
        self.bind("<Configure>", self._on_configure)
        self.bind("<Map>", self._on_map)
        self.bind("<Unmap>", self._on_unmap)
        self.bind("<Destroy>", self._on_destroy)
        self.bind("<Motion>", self._on_motion)

        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Button-1>", self._on_mouse_down)
        self.bind("<B1-Motion>", self._on_mouse_drag)
        self.bind("<ButtonRelease-1>", self._on_mouse_up)
        self.bind("<Double-Button-1>", self._on_double_click)
        self.bind("<MouseWheel>", self._on_mouse_wheel)
        self.bind("<Button-4>", lambda e: self._on_mouse_wheel(e, delta=120))
        self.bind("<Button-5>", lambda e: self._on_mouse_wheel(e, delta=-120))
        self.bind("<Button-3>", self._on_right_click)

        # Keyboard controls when canvas is focused
        self.bind("<Up>", self._on_key_up)
        self.bind("<Down>", self._on_key_down)
        self.bind("<Left>", self._on_key_left)
        self.bind("<Right>", self._on_key_right)
        self.bind("<plus>", self._on_key_zoom_in)
        self.bind("<equal>", self._on_key_zoom_in)
        self.bind("<minus>", self._on_key_zoom_out)
        self.bind("<underscore>", self._on_key_zoom_out)
        self.bind("<Key-0>", self._on_key_reset)
        self.bind("<space>", self._on_key_space)
        self.bind("<Control-space>", self._on_key_ctrl_space)
        self.bind("<Key-m>", self._on_key_m)
        self.bind("<Key-M>", self._on_key_m)
        self.bind("<question>", self._on_key_help)
        self.bind("<Escape>", self._on_key_escape)
        self.bind("<Tab>", self._on_key_tab)
        self.bind("<Shift-Tab>", self._on_key_shift_tab)
        self.bind("<Return>", self._on_key_return)
        self.bind("<KP_Enter>", self._on_key_return)

        self._setup_item_pool()
        self._schedule_realism_build()
        self._poll_telemetry()

        if self.motion != "off":
            self._schedule_tick(10)
        else:
            self._draw_frame(dt=0.0)

    # --- Event Handlers & Lifecycle ---

    @property
    def _scale(self) -> float:
        try:
            tk_scale = float(self.tk.call("tk", "scaling"))
            if tk_scale < 0.5 or tk_scale > 4.0:
                return 1.0
            return tk_scale
        except Exception:
            return 1.0

    @property
    def realism_fallback(self) -> bool:
        return bool(self.realism_fallback_reason)

    @property
    def bitmap_build_ms(self) -> float:
        return self.realism_build_time_ms

    def _on_destroy(self, event: Optional[tk.Event] = None) -> None:
        self._destroyed = True
        if self._telemetry_timer_id is not None:
            try:
                self.after_cancel(self._telemetry_timer_id)
            except Exception:
                pass
            self._telemetry_timer_id = None
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
        if self._realism_debounce_id is not None:
            try:
                self.after_cancel(self._realism_debounce_id)
            except Exception:
                pass
            self._realism_debounce_id = None

    def _on_unmap(self, event: Optional[tk.Event] = None) -> None:
        self._paused = True

    def _on_map(self, event: Optional[tk.Event] = None) -> None:
        if self._paused:
            self._paused = False
            self._last_time = time.monotonic()
            if self.motion != "off" and self._after_id is None:
                self._schedule_tick(10)

    def _on_motion(self, event: tk.Event) -> None:
        self._cursor_x = float(event.x)
        self._cursor_y = float(event.y)
        if self._is_dialog_open() or not self.interactive:
            return

        if self._radial_menu.is_open:
            self._radial_menu.handle_mouse_move(event.x, event.y)
            return

        # Target look-at within +/- 6 deg (0.1047 rad)
        if self.motion == "full":
            ly = layout(self._width, self._height, scaling=self._scale)
            dx = float(event.x) - ly.cx
            dy = float(event.y) - ly.cy
            max_r = max(100.0, ly.sphere_radius * 2.0)
            max_ang = math.radians(6.0)
            self.model.target_lookat_y = max(-max_ang, min(max_ang, (dx / max_r) * max_ang))
            self.model.target_lookat_x = max(-max_ang, min(max_ang, (dy / max_r) * max_ang))
        else:
            self.model.target_lookat_x = 0.0
            self.model.target_lookat_y = 0.0

        # Check arc quad hover for telemetry tooltips
        ly = layout(self._width, self._height, scaling=self._scale)
        dist = math.hypot(event.x - ly.cx, event.y - ly.cy)
        r_inner = ly.arc_inner_radius
        r_outer = r_inner + 0.055 * ly.H * ly.scale
        if r_inner <= dist <= r_outer:
            ang_deg = (math.degrees(math.atan2(event.y - ly.cy, event.x - ly.cx)) + 360.0) % 360.0
            tt_text = ""
            if 180 <= ang_deg <= 360:
                tt_text = self.model.cpu_tooltip or ("CPU: active" if self.model.cpu_load is not None else "")
            else:
                tt_text = self.model.mem_tooltip or ("Memory: active" if self.model.mem_load is not None else "")

            if tt_text and self._arc_tooltip_id is not None and self._arc_tooltip_bg_id is not None:
                pad = 4
                self._configure_item(self._arc_tooltip_id, text=tt_text, state="normal")
                self._update_coords(self._arc_tooltip_id, event.x + 12, event.y - 10)
                try:
                    bbox = self.bbox(self._arc_tooltip_id)
                    if bbox:
                        self._configure_item(self._arc_tooltip_bg_id, state="normal")
                        self._update_coords(self._arc_tooltip_bg_id, bbox[0]-pad, bbox[1]-pad, bbox[2]+pad, bbox[3]+pad)
                except Exception:
                    pass
            else:
                if self._arc_tooltip_id is not None:
                    self._configure_item(self._arc_tooltip_id, state="hidden")
                if self._arc_tooltip_bg_id is not None:
                    self._configure_item(self._arc_tooltip_bg_id, state="hidden")
        else:
            if self._arc_tooltip_id is not None:
                self._configure_item(self._arc_tooltip_id, state="hidden")
            if self._arc_tooltip_bg_id is not None:
                self._configure_item(self._arc_tooltip_bg_id, state="hidden")

    def _on_configure(self, event: tk.Event) -> None:
        if event.width <= 1 or event.height <= 1:
            return
        if event.width == self._width and event.height == self._height:
            return
        self._width = event.width
        self._height = event.height
        if self._debounce_id is not None:
            try:
                self.after_cancel(self._debounce_id)
            except Exception:
                pass
        self._debounce_id = self.after(50, self._apply_resize)
        self._schedule_realism_build()

    def _apply_resize(self) -> None:
        self._debounce_id = None
        self._last_coords_cache.clear()
        self._draw_frame(dt=0.0)

    # --- Item Pooling & Canvas Setup ---

    def _setup_item_pool(self) -> None:
        """Instantiate all visual elements once. Subsequent frames reuse and update in-place."""
        if self._pool_created:
            return
        self._pool_created = True

        # 1. Background image (bitmap PhotoImage at bottom-most Z)
        self._bg_image_id = self.create_image(0, 0, anchor="nw", state="hidden")

        # 2. Vignette background fallback layers
        vignette_colors = ["#08080D", "#0A0A10", "#0C0C12", "#0E0F13"]
        for col in vignette_colors:
            vid = self.create_rectangle(0, 0, 0, 0, fill=col, outline="", state="hidden")
            self._vignette_items.append(vid)

        # 3. Parallax dust specks: deep layer (30 items)
        for _ in range(30):
            pid = self.create_oval(0, 0, 0, 0, fill="#6E5C3E", outline="", state="hidden")
            self._parallax_deep_items.append(pid)

        # 4. Parallax dust specks: mid layer (20 items)
        for _ in range(20):
            pid = self.create_oval(0, 0, 0, 0, fill="#B29C71", outline="", state="hidden")
            self._parallax_mid_items.append(pid)

        # 5. Panel card outlines (smooth polygons for chat and right panels)
        card_chat = self.create_polygon(0, 0, 0, 0, 0, 0, 0, 0, outline="#D6AE47", width=2, fill="#181510", smooth=True)
        card_right = self.create_polygon(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, outline="#7A6640", width=2, fill="#181510", smooth=True)
        self._panel_cards = [card_chat, card_right]

        # 6. Typography: "ORBIT" 5 individual letter-spaced characters in equal advance cells
        font_face = get_heading_font(self, size=15)[0]
        orbit_font = (font_face, 15, "bold")
        for char in "ORBIT":
            tid = self.create_text(0, 0, text=char, anchor="center", fill=self.palette.text, font=orbit_font)
            self._title_chars.append(tid)

        self._title_rule_id = self.create_line(0, 0, 0, 0, fill=self.palette.panel_edge, width=1)
        self._state_label_id = self.create_text(
            0, 0,
            text="STANDBY",
            anchor="center",
            fill="#B29C71",
            font=("Segoe UI", 9, "bold"),
        )

        self._boot_text_id = self.create_text(
            0, 0,
            text="ORBIT ONLINE",
            anchor="center",
            fill=self.palette.accent,
            font=("Segoe UI", 13, "bold"),
            state="normal" if not self._boot_done else "hidden",
        )

        # 7. Bottom chips and baseline
        self._chips_line_id = self.create_line(0, 0, 0, 0, fill=self.palette.panel_edge, width=1)
        self._chip_left_bg = self.create_rectangle(0, 0, 0, 0, fill="#F4DDAA", outline="")
        self._chip_left_text = self.create_text(0, 0, text=self.mode_label, fill="#1A1812", font=("Segoe UI", 8, "bold"))
        self._chip_right_bg = self.create_rectangle(0, 0, 0, 0, fill="#836F47", outline="")
        self._chip_right_text = self.create_text(0, 0, text=self.doctor_label, fill="#1A1812", font=("Segoe UI", 8, "bold"))

        # 8. Double concentric rings (at 0.262H and 0.271H)
        for _ in range(2):
            cid = self.create_oval(0, 0, 0, 0, outline=self.palette.panel_edge, width=1)
            self._double_rings.append(cid)
            self._outer_circles.append(cid)

        # 9. Concentric boundary tick marks (120 ticks)
        for _ in range(120):
            tid = self.create_line(0, 0, 0, 0, fill=self.palette.accent_dim, width=1)
            self._tick_items.append(tid)

        # 10. Segmented arcs: Arc A (24 quads), Arc B (20 quads) drawn with create_polygon
        for _ in range(24):
            aid = self.create_polygon(0, 0, 0, 0, 0, 0, 0, 0, fill="#F2D8A1", outline="", state="hidden")
            self._arc_a_items.append(aid)
        for _ in range(20):
            bid = self.create_polygon(0, 0, 0, 0, 0, 0, 0, 0, fill="#7E6C48", outline="", state="hidden")
            self._arc_b_items.append(bid)
        self._radial_arc_items = self._arc_a_items + self._arc_b_items

        # 11. Dust ring back half (particles & ellipse arc behind sphere)
        for _ in range(350):
            pid = self.create_oval(0, 0, 0, 0, fill=self.palette.accent, outline="", state="hidden")
            self._dust_back_items.append(pid)
            self._particle_items.append(pid)
        self._dust_back_ellipse_id = self.create_line(0, 0, 0, 0, fill="#7A6640", width=1, state="hidden")

        # 12. Bloom Halo Sprite & Sphere Body Glass Sprite
        self._bloom_image_id = self.create_image(0, 0, anchor="nw", state="hidden")
        self._sphere_body_image_id = self.create_image(0, 0, anchor="nw", state="hidden")

        # 13. Core Concentric Filled Circles: 5 layers created LARGEST-FIRST
        # Index 0: 0.55 (#B29C71), Index 1: 0.42 (#C8AD7B), Index 2: 0.30 (#DDBF85),
        # Index 3: 0.20 (#F0D296), Index 4: 0.12 (#FADEA4 center disc on top)
        core_shades = ["#B29C71", "#C8AD7B", "#DDBF85", "#F0D296", "#FADEA4"]
        for shade in core_shades:
            cid = self.create_oval(0, 0, 0, 0, fill=shade, outline="", state="hidden")
            self._core_circle_items.append(cid)
            self._core_glow_items.append(cid)

        # 14. Sphere Limb: 3 stepped glow circles + 1 bright ring (2 px)
        for _ in range(3):
            gid = self.create_oval(0, 0, 0, 0, outline=self.palette.accent_dim, width=2, state="hidden")
            self._limb_glow_items.append(gid)
        self._limb_ring_id = self.create_oval(0, 0, 0, 0, outline="#FADEA4", width=2, state="hidden")

        # 15. Wireframe sphere: 54 polylines (27 back dim, 27 front bright)
        for _ in range(27):
            sid = self.create_line(0, 0, 0, 0, fill="#4A3E28", width=1, state="hidden")
            self._sphere_back_items.append(sid)
            self._sphere_line_items.append(sid)
        for _ in range(27):
            sid = self.create_line(0, 0, 0, 0, fill="#E6CC9A", width=1, state="hidden")
            self._sphere_front_items.append(sid)
            self._sphere_line_items.append(sid)

        # 16. Dust ring front half (particles & ellipse arc in front of sphere)
        for _ in range(350):
            pid = self.create_oval(0, 0, 0, 0, fill=self.palette.accent, outline="", state="hidden")
            self._dust_front_items.append(pid)
        self._dust_front_ellipse_id = self.create_line(0, 0, 0, 0, fill="#D6AE47", width=2, state="hidden")

        # 17. Parallax dust specks: near layer (10 items on top)
        for _ in range(10):
            pid = self.create_oval(0, 0, 0, 0, fill="#F2D8A1", outline="", state="hidden")
            self._parallax_near_items.append(pid)

        # 18. Focus Ring, Tooltip, and HUD Notices
        self._focus_ring_id = self.create_rectangle(
            2, 2, self._width - 2, self._height - 2,
            outline=self.palette.accent,
            width=1,
            dash=(3, 3),
            state="hidden",
        )
        self._arc_tooltip_bg_id = self.create_rectangle(0, 0, 0, 0, fill=self.palette.panel, outline=self.palette.accent, state="hidden")
        self._arc_tooltip_id = self.create_text(0, 0, text="", fill=self.palette.text, font=("Segoe UI", 9, "bold"), state="hidden")
        self._notice_items = [
            self.create_text(0, 0, text="", fill=self.palette.accent, font=("Segoe UI", 9, "bold"), state="hidden"),
            self.create_text(0, 0, text="", fill=self.palette.accent, font=("Segoe UI", 9, "bold"), state="hidden"),
        ]

    # --- Interactive Features & Event Handlers ---

    def _is_dialog_open(self) -> bool:
        if self.is_dialog_open is not None:
            try:
                return bool(self.is_dialog_open())
            except Exception:
                pass
        return False

    def show_notice(self, text: str, lifetime: float = 3.0) -> None:
        """Post a short-lived transient HUD notice under the title rule."""
        self.notices_count += 1
        clean_text = str(text).strip()
        self._notices.append(HudNotice(clean_text, time.monotonic(), float(lifetime)))
        if len(self._notices) > 2:
            self._notices = self._notices[-2:]

    def open_radial_menu(self, actions: list[RadialAction]) -> bool:
        """Open the radial quick action menu centered on the orb."""
        if self._is_dialog_open() or not self.interactive:
            return False
        self.radial_menu_opens += 1
        ly = layout(self._width, self._height, scaling=self._scale)
        orb_r = ly.sphere_radius * self.model.zoom_factor
        return self._radial_menu.open(
            cx=ly.cx,
            cy=ly.cy,
            radius=orb_r * 1.30,
            actions=actions,
        )

    def _poll_telemetry(self) -> None:
        if self._destroyed:
            return
        try:
            from ui.telemetry import sample_system_telemetry
            telem = sample_system_telemetry()
            if telem.has_data:
                self.model.cpu_load = telem.cpu_percent / 100.0 if telem.cpu_percent is not None else None
                self.model.mem_load = telem.mem_percent / 100.0 if telem.mem_percent is not None else None
                self.model.cpu_tooltip = telem.format_cpu_tooltip()
                self.model.mem_tooltip = telem.format_memory_tooltip()
            else:
                self.model.cpu_load = None
                self.model.mem_load = None
                self.model.cpu_tooltip = ""
                self.model.mem_tooltip = ""
        except Exception:
            self.model.cpu_load = None
            self.model.mem_load = None
            self.model.cpu_tooltip = ""
            self.model.mem_tooltip = ""
        self._telemetry_timer_id = self.after(1000, self._poll_telemetry)

    def _on_focus_in(self, event: tk.Event) -> None:
        if self._focus_ring_id is not None:
            self.itemconfigure(self._focus_ring_id, state="normal")

    def _on_focus_out(self, event: tk.Event) -> None:
        if self._focus_ring_id is not None:
            self.itemconfigure(self._focus_ring_id, state="hidden")

    def _on_mouse_down(self, event: tk.Event) -> None:
        if self._is_dialog_open() or not self.interactive:
            return

        # If radial menu is open, handle click
        if self._radial_menu.is_open:
            if self._radial_menu.handle_mouse_click(event.x, event.y):
                return

        ly = layout(self._width, self._height, scaling=self._scale)
        orb_r = ly.sphere_radius * self.model.zoom_factor
        dist = math.hypot(event.x - ly.cx, event.y - ly.cy)

        if dist <= orb_r:
            self.focus_set()
            self._is_dragging = True
            self._has_dragged = False
            self._drag_start_x = float(event.x)
            self._drag_start_y = float(event.y)
            self._drag_last_x = float(event.x)
            self._drag_last_y = float(event.y)
            self._drag_last_time = time.monotonic()
            self._drag_distance = 0.0

    def _on_mouse_drag(self, event: tk.Event) -> None:
        if not self._is_dragging:
            return

        dx = float(event.x) - self._drag_last_x
        dy = float(event.y) - self._drag_last_y
        dist = math.hypot(dx, dy)
        self._drag_distance += dist

        now = time.monotonic()
        dt_drag = max(0.001, now - self._drag_last_time)

        if self._drag_distance >= 4.0:
            if not self._has_dragged:
                self._has_dragged = True
                self.drag_count += 1

            self.model.sphere_ay = (self.model.sphere_ay + dx * 0.008) % (2 * math.pi)
            self.model.sphere_ax = (self.model.sphere_ax + dy * 0.008) % (2 * math.pi)

            if self.motion == "full":
                self.model.ang_vel_y = (dx * 0.008) / dt_drag
                self.model.ang_vel_x = (dy * 0.008) / dt_drag

        self._drag_last_x = float(event.x)
        self._drag_last_y = float(event.y)
        self._drag_last_time = now

    def _on_mouse_up(self, event: tk.Event) -> None:
        if not self._is_dragging:
            return
        self._is_dragging = False

        if self._drag_distance < 4.0:
            # Click on orb toggles microphone through same handler
            ly = layout(self._width, self._height, scaling=self._scale)
            orb_r = ly.sphere_radius * self.model.zoom_factor
            dist = math.hypot(event.x - ly.cx, event.y - ly.cy)
            if dist <= orb_r:
                self._trigger_mic_action()
        else:
            if self.motion in ("reduced", "off"):
                self.model.ang_vel_x = 0.0
                self.model.ang_vel_y = 0.0

    def _trigger_mic_action(self) -> None:
        if self.on_mic is not None:
            blocked = False
            if hasattr(self, "_is_blocked_callback") and self._is_blocked_callback:
                try:
                    blocked = bool(self._is_blocked_callback())
                except Exception:
                    blocked = False
            if blocked:
                self.show_notice("Microphone busy")
            else:
                self.on_mic()

    def _on_double_click(self, event: tk.Event) -> None:
        if self._is_dialog_open() or not self.interactive:
            return
        ly = layout(self._width, self._height, scaling=self._scale)
        orb_r = ly.sphere_radius * self.model.zoom_factor
        dist = math.hypot(event.x - ly.cx, event.y - ly.cy)
        if dist <= orb_r:
            self.model.reset_view()
            self.zoom_factor = 1.0

    def _on_mouse_wheel(self, event: tk.Event, delta: Optional[int] = None) -> None:
        if self._is_dialog_open() or not self.interactive:
            return
        d = delta if delta is not None else getattr(event, "delta", 0)
        step = 1.05 if d > 0 else 0.95
        ly = layout(self._width, self._height, scaling=self._scale)
        new_zoom = clamp_zoom_factor(self._width, self._height, self.model.zoom_factor * step, scale=ly.scale)
        self.model.zoom_factor = new_zoom
        self.zoom_factor = new_zoom

    def _on_right_click(self, event: tk.Event) -> None:
        if self._is_dialog_open() or not self.interactive:
            return
        ly = layout(self._width, self._height, scaling=self._scale)
        orb_r = ly.sphere_radius * self.model.zoom_factor
        dist = math.hypot(event.x - ly.cx, event.y - ly.cy)
        if dist <= orb_r:
            if self.on_radial_menu is not None:
                self.on_radial_menu(ly.cx, ly.cy)

    def _on_key_up(self, event: tk.Event) -> None:
        if self._is_dialog_open() or not self.interactive:
            return
        if self._radial_menu.is_open:
            self._radial_menu.select_prev()
            return
        self.model.sphere_ax = (self.model.sphere_ax - math.radians(15.0)) % (2 * math.pi)

    def _on_key_down(self, event: tk.Event) -> None:
        if self._is_dialog_open() or not self.interactive:
            return
        if self._radial_menu.is_open:
            self._radial_menu.select_next()
            return
        self.model.sphere_ax = (self.model.sphere_ax + math.radians(15.0)) % (2 * math.pi)

    def _on_key_left(self, event: tk.Event) -> None:
        if self._is_dialog_open() or not self.interactive:
            return
        if self._radial_menu.is_open:
            self._radial_menu.select_prev()
            return
        self.model.sphere_ay = (self.model.sphere_ay - math.radians(15.0)) % (2 * math.pi)

    def _on_key_right(self, event: tk.Event) -> None:
        if self._is_dialog_open() or not self.interactive:
            return
        if self._radial_menu.is_open:
            self._radial_menu.select_next()
            return
        self.model.sphere_ay = (self.model.sphere_ay + math.radians(15.0)) % (2 * math.pi)

    def _on_key_zoom_in(self, event: tk.Event) -> None:
        if self._is_dialog_open() or not self.interactive:
            return
        ly = layout(self._width, self._height, scaling=self._scale)
        new_zoom = clamp_zoom_factor(self._width, self._height, self.model.zoom_factor + 0.1, scale=ly.scale)
        self.model.zoom_factor = new_zoom
        self.zoom_factor = new_zoom

    def _on_key_zoom_out(self, event: tk.Event) -> None:
        if self._is_dialog_open() or not self.interactive:
            return
        ly = layout(self._width, self._height, scaling=self._scale)
        new_zoom = clamp_zoom_factor(self._width, self._height, self.model.zoom_factor - 0.1, scale=ly.scale)
        self.model.zoom_factor = new_zoom
        self.zoom_factor = new_zoom

    def _on_key_reset(self, event: tk.Event) -> None:
        if self._is_dialog_open() or not self.interactive:
            return
        self.model.reset_view()
        self.zoom_factor = 1.0

    def _on_key_space(self, event: tk.Event) -> None:
        if self._is_dialog_open() or not self.interactive:
            return
        if self._radial_menu.is_open:
            self._radial_menu.execute_selected()
            return
        self._trigger_mic_action()

    def _on_key_ctrl_space(self, event: tk.Event) -> None:
        if self._is_dialog_open() or not self.interactive:
            return
        self._trigger_mic_action()

    def _on_key_m(self, event: tk.Event) -> None:
        if self._is_dialog_open() or not self.interactive:
            return
        ly = layout(self._width, self._height, scaling=self._scale)
        if self.on_radial_menu is not None:
            self.on_radial_menu(ly.cx, ly.cy)

    def _on_key_help(self, event: tk.Event) -> None:
        if self._is_dialog_open() or not self.interactive:
            return
        if self.on_shortcuts is not None:
            self.on_shortcuts()

    def _on_key_escape(self, event: tk.Event) -> None:
        if self._radial_menu.is_open:
            self._radial_menu.close()

    def _on_key_tab(self, event: tk.Event) -> Optional[str]:
        if self._radial_menu.is_open:
            self._radial_menu.select_next()
            return "break"
        return None

    def _on_key_shift_tab(self, event: tk.Event) -> Optional[str]:
        if self._radial_menu.is_open:
            self._radial_menu.select_prev()
            return "break"
        return None

    def _on_key_return(self, event: tk.Event) -> Optional[str]:
        if self._radial_menu.is_open:
            self._radial_menu.execute_selected()
            return "break"
        return None

    # --- Realism Background & Sprite Worker ---

    def _schedule_realism_build(self) -> None:
        """Schedule background worker to pre-render bitmaps/sprites with 200ms debounce."""
        if self.realism == "off":
            return
        if self._realism_debounce_id is not None:
            try:
                self.after_cancel(self._realism_debounce_id)
            except Exception:
                pass
        self._realism_debounce_id = self.after(200, self._start_realism_worker)

    def _start_realism_worker(self) -> None:
        self._realism_debounce_id = None
        if self._realism_worker_busy or self._destroyed:
            return
        self._realism_worker_busy = True
        w = self._width
        h = self._height
        pal = self.palette
        realism_mode = self.realism
        tier = self.model.tier

        def _worker() -> None:
            t0 = time.perf_counter()
            try:
                bg_arr, bg_ppm = build_background_bitmap(w, h, pal)
                ly = layout(w, h)
                blooms_data: list[bytes] = []
                body_ppm_data: Optional[bytes] = None
                sprite_box: tuple[int, int, int] = (0, 0, 0)

                if realism_mode == "full":
                    num_levels = 8 if tier == "high" else (4 if tier == "medium" else (2 if tier == "low" else 1))
                    blooms = build_bloom_sprites(ly.sphere_radius, ly.cx, ly.cy, bg_arr, pal, num_levels=num_levels)
                    blooms_data = [ppm for _, ppm in blooms]
                    _, body_ppm_data = build_sphere_body_sprite(ly.sphere_radius, ly.cx, ly.cy, bg_arr, pal)
                    S = blooms[0][0].shape[0]
                    half_S = S // 2
                    sprite_box = (int(round(ly.cx)) - half_S, int(round(ly.cy)) - half_S, S)

                elapsed_ms = (time.perf_counter() - t0) * 1000.0

                def _deliver() -> None:
                    if self._destroyed:
                        return
                    self._realism_worker_busy = False
                    self.realism_build_time_ms = elapsed_ms
                    try:
                        self._apply_realism_assets(bg_ppm, blooms_data, body_ppm_data, sprite_box)
                    except Exception as exc:
                        self.realism_fallback_reason = f"PhotoImage failed: {exc}"
                        if self._bg_image_id is not None:
                            self.itemconfigure(self._bg_image_id, state="hidden")
                        if self._bloom_image_id is not None:
                            self.itemconfigure(self._bloom_image_id, state="hidden")
                        if self._sphere_body_image_id is not None:
                            self.itemconfigure(self._sphere_body_image_id, state="hidden")

                try:
                    self.after(0, _deliver)
                except Exception:
                    self._realism_worker_busy = False

            except Exception as exc:
                self._realism_worker_busy = False
                self.realism_fallback_reason = f"Worker failed: {exc}"

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def _apply_realism_assets(
        self,
        bg_ppm: bytes,
        blooms_data: list[bytes],
        body_ppm_data: Optional[bytes],
        sprite_box: tuple[int, int, int],
    ) -> None:
        """Instantiate PhotoImages on the UI thread."""
        self._bg_photo = tk.PhotoImage(data=bg_ppm, format="PPM")
        if self._bg_image_id is not None:
            self.itemconfigure(self._bg_image_id, image=self._bg_photo, state="normal")
            self.coords(self._bg_image_id, 0, 0)

        if blooms_data:
            self._bloom_photos = [tk.PhotoImage(data=b, format="PPM") for b in blooms_data]
            self.realism_sprite_count = len(self._bloom_photos)
            if self._bloom_image_id is not None and self._bloom_photos:
                bx, by, _ = sprite_box
                self.coords(self._bloom_image_id, bx, by)
                self.itemconfigure(self._bloom_image_id, image=self._bloom_photos[0], state="normal")
                self._last_bloom_level = 0

        if body_ppm_data is not None:
            self._sphere_body_photo = tk.PhotoImage(data=body_ppm_data, format="PPM")
            if self._sphere_body_image_id is not None:
                bx, by, _ = sprite_box
                self.coords(self._sphere_body_image_id, bx, by)
                self.itemconfigure(self._sphere_body_image_id, image=self._sphere_body_photo, state="normal")

        self.realism_fallback_reason = ""
        self._draw_frame(dt=0.0)

    # --- Animation Tick & Rendering ---

    def _schedule_tick(self, delay_ms: int) -> None:
        if self._destroyed:
            return
        self._after_id = self.after(max(1, delay_ms), self._tick)

    def _tick(self) -> None:
        self._after_id = None
        if self._destroyed or self._paused:
            return

        now = time.monotonic()
        dt = now - self._last_time
        self._last_time = now

        # Enforce target FPS (reduced motion = 8 FPS, else configured self.fps)
        target_fps = 8 if self.motion == "reduced" else self.fps
        frame_interval = 1.0 / target_fps
        t_start = time.perf_counter()

        self.model.update(dt, motion_mode=self.motion)
        self._draw_frame(dt)

        t_end = time.perf_counter()
        self._last_frame_duration = t_end - t_start
        self.model.governor.record_frame(self._last_frame_duration)

        if not self._destroyed and self.motion != "off":
            delay = int(max(1, (frame_interval - self._last_frame_duration) * 1000))
            self._schedule_tick(delay)

    def _update_coords(self, item_id: int, *coords: float) -> bool:
        """Call coords only when rounded integer/0.5px positions actually change."""
        rounded = tuple(round(c, 1) for c in coords)
        if self._last_coords_cache.get(item_id) == rounded:
            return False
        self._last_coords_cache[item_id] = rounded
        self.coords(item_id, *coords)
        self._items_touched_this_frame += 1
        return True

    def _configure_item(self, item_id: int, **kwargs: Any) -> bool:
        """Call itemconfigure only when attributes change."""
        cache = self._last_config_cache.setdefault(item_id, {})
        changed = False
        for k, v in kwargs.items():
            if cache.get(k) != v:
                changed = True
                cache[k] = v
        if changed:
            self.itemconfigure(item_id, **kwargs)
            self._items_touched_this_frame += 1
            return True
        return False

    def _draw_frame(self, dt: float) -> None:
        """Render all scene layers with item pooling and zero per-frame churn."""
        self._items_touched_this_frame = 0
        w = max(200, self._width)
        h = max(180, self._height)

        # Tk scaling for dock & clearance derivation
        try:
            tk_scale = float(self.tk.call("tk", "scaling"))
            if tk_scale < 0.5 or tk_scale > 4.0:
                tk_scale = 1.0
        except Exception:
            tk_scale = 1.0

        ly = layout(w, h, scaling=tk_scale)
        cx = ly.cx
        cy = ly.cy
        sphere_r = ly.sphere_radius * self.model.zoom_factor

        st = self.model.state
        tier = self.model.tier
        is_low = tier in ("low", "static")
        is_static = tier == "static" or self.motion == "off"

        pulse_val = self.model.pulse_val
        pulse_boost = 1.0 + 0.20 * pulse_val

        # 1. Background image / Fallback Vignette
        if self._bg_photo is not None and self.realism != "off" and not self.realism_fallback_reason:
            if self._bg_image_id is not None:
                self._configure_item(self._bg_image_id, state="normal")
            for vid in self._vignette_items:
                self._configure_item(vid, state="hidden")
        else:
            if self._bg_image_id is not None:
                self._configure_item(self._bg_image_id, state="hidden")
            if self._vignette_items:
                for idx, vid in enumerate(self._vignette_items):
                    inset = idx * min(w, h) * 0.08
                    self._update_coords(vid, inset, inset, w - inset, h - inset)
                    self._configure_item(vid, state="normal")

        # 2. Panel Outline Cards
        if len(self._panel_cards) >= 2:
            r_corner = 12.0
            c_x1, c_x2 = ly.chat_x_left, ly.chat_x_right
            c_y1 = ly.chat_y_expanded_top if self.chat_expanded else ly.panel_y_top
            c_y2 = ly.panel_y_bottom
            chat_poly = [
                c_x1 + r_corner, c_y1, c_x2 - r_corner, c_y1,
                c_x2, c_y1 + r_corner, c_x2, c_y2 - r_corner,
                c_x2 - r_corner, c_y2, c_x1 + r_corner, c_y2,
                c_x1, c_y2 - r_corner, c_x1, c_y1 + r_corner,
            ]
            self._update_coords(self._panel_cards[0], *chat_poly)

            r_x1, r_x2 = ly.right_x_left, ly.right_x_right
            r_y1, r_y2 = ly.panel_y_top, ly.panel_y_bottom
            right_poly = [
                r_x1 + r_corner, r_y1, r_x2 - r_corner, r_y1,
                r_x2, r_y1 + r_corner, r_x2, r_y2 - r_corner,
                r_x2 - r_corner, r_y2, r_x1 + r_corner, r_y2,
                r_x1, r_y2 - r_corner, r_x1, r_y1 + r_corner,
            ]
            self._update_coords(self._panel_cards[1], *right_poly)

        # 3. Typography: Equal advance cells for "ORBIT"
        if self._title_chars:
            try:
                font_obj = (get_heading_font(self, size=15)[0], 15, "bold")
                f_meas = tk.font.Font(font=font_obj).measure
                widest_g = max(f_meas(c) for c in "ORBIT")
            except Exception:
                widest_g = 16.0
            tracking = 14.0
            title_xs = compute_equal_advance_cells("ORBIT", cx, widest_g, tracking)
            for idx, cid in enumerate(self._title_chars):
                if idx < len(title_xs):
                    self._update_coords(cid, title_xs[idx], ly.title_y)

        if self._title_rule_id is not None:
            half_w = ly.title_rule_w / 2.0
            self._update_coords(self._title_rule_id, cx - half_w, ly.title_rule_y, cx + half_w, ly.title_rule_y)

        # Update transient HUD notices under title rule
        now_notice = time.monotonic()
        self._notices = [n for n in self._notices if (now_notice - n.created_at) < n.lifetime]
        for idx, nid in enumerate(self._notice_items):
            if idx < len(self._notices):
                n = self._notices[idx]
                rem = max(0.0, min(1.0, 1.0 - (now_notice - n.created_at) / n.lifetime))
                fade_col = self.palette.accent if rem > 0.4 else self.palette.accent_dim
                ny = ly.title_rule_y + 14.0 + (idx * 16.0)
                self._update_coords(nid, cx, ny)
                self._configure_item(nid, text=n.text, fill=fade_col, state="normal")
            else:
                self._configure_item(nid, state="hidden")

        if self._focus_ring_id is not None:
            self._update_coords(self._focus_ring_id, 2, 2, w - 2, h - 2)

        # 4. State label under orb
        if self._state_label_id is not None:
            state_labels = {
                HudState.IDLE: ("STANDBY", "#B29C71"),
                HudState.LISTENING: ("LISTENING", self.palette.accent),
                HudState.THINKING: ("PROCESSING", self.palette.accent),
                HudState.SPEAKING: ("SPEAKING", self.palette.ok),
                HudState.CONFIRMING: ("AWAITING APPROVAL", self.palette.warn),
                HudState.ERROR: ("FAULT", self.palette.danger),
            }
            s_text, s_col = state_labels.get(st, ("STANDBY", "#B29C71"))
            self._update_coords(self._state_label_id, cx, ly.state_label_y)
            self._configure_item(self._state_label_id, text=s_text, fill=s_col)

        # 5. Bottom chips line and pills
        if self._chips_line_id is not None:
            self._update_coords(self._chips_line_id, 0, ly.chips_y, w, ly.chips_y)

        if self._chip_left_bg is not None and self._chip_left_text is not None:
            chip_w, chip_h = 130.0, 18.0
            x_left_pill = cx - chip_w - 10.0
            self._update_coords(self._chip_left_bg, x_left_pill, ly.chips_y - chip_h / 2.0, x_left_pill + chip_w, ly.chips_y + chip_h / 2.0)
            self._update_coords(self._chip_left_text, x_left_pill + chip_w / 2.0, ly.chips_y)

        if self._chip_right_bg is not None and self._chip_right_text is not None:
            chip_w, chip_h = 150.0, 18.0
            x_right_pill = cx + 10.0
            self._update_coords(self._chip_right_bg, x_right_pill, ly.chips_y - chip_h / 2.0, x_right_pill + chip_w, ly.chips_y + chip_h / 2.0)
            self._update_coords(self._chip_right_text, x_right_pill + chip_w / 2.0, ly.chips_y)

        # 6. Double Concentric Rings (0.262H and 0.271H)
        if len(self._double_rings) >= 2:
            r_in = ly.ring_inner_radius
            r_out = ly.ring_outer_radius
            self._update_coords(self._double_rings[0], cx - r_in, cy - r_in, cx + r_in, cy + r_in)
            self._update_coords(self._double_rings[1], cx - r_out, cy - r_out, cx + r_out, cy + r_out)

        # 7. Concentric Ticks (minor 0.006H, major 0.012H) - ALWAYS drawn, even in off/static
        max_ticks = 120 if tier == "high" else (60 if tier == "medium" else 24)
        tick_step = 1 if tier == "high" else (2 if tier == "medium" else 5)
        for idx, item_id in enumerate(self._tick_items):
            if idx >= max_ticks:
                self._configure_item(item_id, state="hidden")
                continue

            ref_idx = idx * tick_step
            if ref_idx >= len(self.model.ref_ticks):
                self._configure_item(item_id, state="hidden")
                continue

            ang, t_len_ratio, is_maj = self.model.ref_ticks[ref_idx]
            r1 = ly.ring_outer_radius
            r2 = r1 + t_len_ratio * ly.H * ly.scale
            x1 = cx + r1 * math.cos(ang)
            y1 = cy + r1 * math.sin(ang)
            x2 = cx + r2 * math.cos(ang)
            y2 = cy + r2 * math.sin(ang)

            col = "#D6AE47" if is_maj else self.palette.accent_dim
            self._configure_item(item_id, state="normal", fill=col)
            self._update_coords(item_id, x1, y1, x2, y2)

        # 8. Segmented Arcs (Quads with create_polygon)
        drift_a = math.radians(self.model.arc_a_drift_deg)
        drift_b = math.radians(self.model.arc_b_drift_deg)
        mic_boost = self.model.mic_level if st == HudState.LISTENING else 0.0
        gain = 0.03 * ly.H * ly.scale
        base_thickness = 0.025 * ly.H * ly.scale

        # Arc A Quads (24 blocks, CPU Load telemetry if available)
        total_a = len(self.model.blocks_a)
        lit_a = int(round(self.model.cpu_load * total_a)) if self.model.cpu_load is not None else total_a

        for idx, item_id in enumerate(self._arc_a_items):
            if idx >= total_a:
                self._configure_item(item_id, state="hidden")
                continue
            blk = self.model.blocks_a[idx]
            start_d = blk.start_deg + math.degrees(drift_a)
            end_d = blk.end_deg + math.degrees(drift_a)

            extra_len = (pulse_val + mic_boost) * gain * blk.weight
            r1 = ly.arc_inner_radius
            r2 = ly.arc_inner_radius + base_thickness + extra_len

            quad_coords = compute_arc_quad_coords(cx, cy, r1, r2, start_d, end_d)
            is_lit = (idx < lit_a) or (self.model.cpu_load is None)
            base_col = blk.base_color if is_lit else "#383125"
            col = adjust_color_brightness(base_col, pulse_boost)
            self._configure_item(item_id, state="normal", fill=col)
            self._update_coords(item_id, *quad_coords)

        # Arc B Quads (20 blocks, Memory Load telemetry if available)
        total_b = len(self.model.blocks_b)
        lit_b = int(round(self.model.mem_load * total_b)) if self.model.mem_load is not None else total_b

        for idx, item_id in enumerate(self._arc_b_items):
            if idx >= total_b:
                self._configure_item(item_id, state="hidden")
                continue
            blk = self.model.blocks_b[idx]
            start_d = blk.start_deg + math.degrees(drift_b)
            end_d = blk.end_deg + math.degrees(drift_b)

            extra_len = (pulse_val + mic_boost) * gain * blk.weight
            r1 = ly.arc_inner_radius
            r2 = ly.arc_inner_radius + base_thickness + extra_len

            quad_coords = compute_arc_quad_coords(cx, cy, r1, r2, start_d, end_d)
            is_lit = (idx < lit_b) or (self.model.mem_load is None)
            base_col = blk.base_color if is_lit else "#383125"
            col = adjust_color_brightness(base_col, pulse_boost)
            self._configure_item(item_id, state="normal", fill=col)
            self._update_coords(item_id, *quad_coords)

        # 9. Dust Ring Particles & Central Ellipse
        tilt_rad = math.radians(-22.0)
        cos_tilt = math.cos(tilt_rad)
        sin_tilt = math.sin(tilt_rad)

        target_circle_r = 0.36 * ly.H * ly.scale
        dust_a = ly.dust_semi_major + (target_circle_r - ly.dust_semi_major) * pulse_val
        dust_b = ly.dust_semi_minor + (target_circle_r - ly.dust_semi_minor) * pulse_val
        dust_orbit_rad = math.radians(self.model.dust_orbit_deg)

        # Governor & Motion particle scaling
        base_particles = 350 if tier == "high" else (200 if tier == "medium" else 100)
        if self.motion == "reduced":
            max_particles = max(1, base_particles // 2)
        elif self.motion == "off":
            max_particles = 60  # Faint static dust ring
        else:
            max_particles = base_particles

        # Draw Dust Back Items (z < 0)
        for idx, pid in enumerate(self._dust_back_items):
            if idx >= max_particles:
                self._configure_item(pid, state="hidden")
                continue
            pt = self.model.dust_particles[idx]
            phi = pt.base_phase + dust_orbit_rad
            if math.sin(phi) <= 0.0:
                self._configure_item(pid, state="hidden")
                continue

            r_jit = pt.radial_jitter * ly.H * ly.scale
            xe = (dust_a + r_jit) * math.cos(phi)
            ye = (dust_b + r_jit * (dust_b / dust_a)) * math.sin(phi)

            xs = cx + xe * cos_tilt - ye * sin_tilt
            ys = cy + xe * sin_tilt + ye * cos_tilt

            # Occlude if inside sphere
            if math.hypot(xs - cx, ys - cy) < sphere_r * 0.98:
                self._configure_item(pid, state="hidden")
                continue

            pr = pt.size
            shade = adjust_color_brightness("#7A6640" if self.motion == "off" else "#9E824C", pulse_boost)
            self._configure_item(pid, state="normal", fill=shade)
            self._update_coords(pid, round(xs - pr), round(ys - pr), round(xs + pr), round(ys + pr))

        # 10. Bloom Sprite & Glass Sphere Body Sprite
        if self.realism == "full" and not self.realism_fallback_reason:
            if self._bloom_photos and self._bloom_image_id is not None:
                lvl = int(round(pulse_val * (len(self._bloom_photos) - 1)))
                lvl = max(0, min(len(self._bloom_photos) - 1, lvl))
                if lvl != self._last_bloom_level:
                    self._last_bloom_level = lvl
                    self.itemconfigure(self._bloom_image_id, image=self._bloom_photos[lvl], state="normal")
                    self._items_touched_this_frame += 1
            if self._sphere_body_photo is not None and self._sphere_body_image_id is not None:
                self._configure_item(self._sphere_body_image_id, state="normal")

        # 11. Core Concentric Filled Circles: LARGEST-FIRST
        # Index 0: 0.55 (#B29C71), Index 1: 0.42 (#C8AD7B), Index 2: 0.30 (#DDBF85),
        # Index 3: 0.20 (#F0D296), Index 4: 0.12 (#FADEA4)
        core_shades = ["#B29C71", "#C8AD7B", "#DDBF85", "#F0D296", "#FADEA4"]
        core_fracs = [0.55, 0.42, 0.30, 0.20, 0.12]

        for idx, (cid, frac) in enumerate(zip(self._core_circle_items, core_fracs)):
            # On low tier / static: never collapse to one disc; use 3 layers (index 0, 2, 4)
            if is_low and idx in (1, 3):
                self._configure_item(cid, state="hidden")
                continue

            cr = frac * sphere_r * (1.0 + 0.10 * self.model.core_pulse)
            shade = adjust_color_brightness(core_shades[idx], pulse_boost)
            self._configure_item(cid, state="normal", fill=shade)
            self._update_coords(cid, cx - cr, cy - cr, cx + cr, cy + cr)

        # 12. Sphere Limb: Stepped Glow Circles & Bright Ring
        glow_steps = [1.02, 1.05, 1.08]
        glow_colors = ["#9E824C", "#7A6640", "#54442B"]
        for idx, gid in enumerate(self._limb_glow_items):
            glow_r = sphere_r * glow_steps[idx]
            g_col = adjust_color_brightness(glow_colors[idx], pulse_boost)
            self._configure_item(gid, state="normal", outline=g_col)
            self._update_coords(gid, cx - glow_r, cy - glow_r, cx + glow_r, cy + glow_r)

        if self._limb_ring_id is not None:
            limb_col = adjust_color_brightness("#FADEA4", pulse_boost)
            self._configure_item(self._limb_ring_id, state="normal", outline=limb_col)
            self._update_coords(self._limb_ring_id, cx - sphere_r, cy - sphere_r, cx + sphere_r, cy + sphere_r)

        # 13. Orthographic Sphere Wireframe Polylines (27 curves: 11 lats, 16 meridians)
        tilt_x = math.radians(18.0) + self.model.lookat_x
        sphere_yaw = (self.model.sphere_ay + self.model.lookat_y)
        wire_curves = self.model.ref_lat_curves + self.model.ref_meridian_curves
        dim_line_col = adjust_color_brightness("#4A3E28", pulse_boost)
        bright_line_col = adjust_color_brightness("#E6CC9A", pulse_boost)

        for c_idx, curve_pts in enumerate(wire_curves):
            # Rotate in 3D
            rot_pts = rotate(curve_pts, tilt_x, sphere_yaw, 0.0)
            proj_pts = project_orthographic(rot_pts, cx, cy, sphere_r)
            f_coords, b_coords = split_curve_front_back(proj_pts)

            # Back polyline
            b_id = self._sphere_back_items[c_idx]
            if len(b_coords) >= 4:
                self._configure_item(b_id, state="normal", fill=dim_line_col)
                self._update_coords(b_id, *b_coords)
            else:
                self._configure_item(b_id, state="hidden")

            # Front polyline
            f_id = self._sphere_front_items[c_idx]
            if len(f_coords) >= 4:
                self._configure_item(f_id, state="normal", fill=bright_line_col)
                self._update_coords(f_id, *f_coords)
            else:
                self._configure_item(f_id, state="hidden")

        # 14. Dust Ring Front Items (z >= 0) & Front Ellipse Arc
        for idx, pid in enumerate(self._dust_front_items):
            if idx >= max_particles:
                self._configure_item(pid, state="hidden")
                continue
            pt = self.model.dust_particles[idx]
            phi = pt.base_phase + dust_orbit_rad
            if math.sin(phi) > 0.0:
                self._configure_item(pid, state="hidden")
                continue

            r_jit = pt.radial_jitter * ly.H * ly.scale
            xe = (dust_a + r_jit) * math.cos(phi)
            ye = (dust_b + r_jit * (dust_b / dust_a)) * math.sin(phi)

            xs = cx + xe * cos_tilt - ye * sin_tilt
            ys = cy + xe * sin_tilt + ye * cos_tilt

            pr = pt.size
            shade = adjust_color_brightness("#FADEA4" if self.motion == "off" else self.palette.accent, pulse_boost)
            self._configure_item(pid, state="normal", fill=shade)
            self._update_coords(pid, round(xs - pr), round(ys - pr), round(xs + pr), round(ys + pr))

        # 15. Parallax Dust Specks (3 depth layers with cursor tracking)
        if self.realism == "full":
            cursor_dx = (self._cursor_x - cx) if self.motion == "full" else 0.0
            cursor_dy = (self._cursor_y - cy) if self.motion == "full" else 0.0
            shift_rates = [0.01, 0.02, 0.04]
            layer_pools = [
                (self._parallax_deep_items, 0, (30 if tier == "high" else (15 if tier == "medium" else 8))),
                (self._parallax_mid_items, 1, (20 if tier == "high" else (10 if tier == "medium" else 5))),
                (self._parallax_near_items, 2, (10 if tier == "high" else (5 if tier == "medium" else 2))),
            ]

            p_idx = 0
            for item_list, l_num, active_cnt in layer_pools:
                shift_px = shift_rates[l_num] * w
                for i_idx, item_id in enumerate(item_list):
                    if i_idx >= active_cnt or is_static:
                        self._configure_item(item_id, state="hidden")
                        continue
                    part = self.model.parallax_particles[p_idx % len(self.model.parallax_particles)]
                    p_idx += 1

                    px = (part.norm_x * w + (cursor_dx / (0.5 * w)) * shift_px) % w
                    py = (part.norm_y * h + (cursor_dy / (0.5 * h)) * shift_px) % h
                    ps = part.size
                    self._configure_item(item_id, state="normal", fill=part.shade)
                    self._update_coords(item_id, round(px - ps), round(py - ps), round(px + ps), round(py + ps))

        # 16. Boot Animation Text
        if not self._boot_done and self._boot_text_id is not None:
            now_boot = time.monotonic()
            if now_boot - self._boot_start_time > 1.8:
                self._boot_done = True
                self._configure_item(self._boot_text_id, state="hidden")
            else:
                self._update_coords(self._boot_text_id, cx, cy)
                self._configure_item(self._boot_text_id, state="normal")

        # Telemetry update
        self.items_touched_last_frame = self._items_touched_this_frame
        self._items_touched_history.append(self._items_touched_this_frame)
        if len(self._items_touched_history) > 20:
            self._items_touched_history.pop(0)
        self.items_touched_average = sum(self._items_touched_history) / max(1, len(self._items_touched_history))

    def set_palette(self, palette: Palette) -> None:
        self.palette = palette
        self.configure(bg=palette.bg)
        self._last_coords_cache.clear()
        self._last_config_cache.clear()
        self._schedule_realism_build()
        self._draw_frame(dt=0.0)

    def set_labels(self, mode_label: Optional[str] = None, doctor_label: Optional[str] = None) -> None:
        if mode_label is not None:
            self.mode_label = mode_label
            if self._chip_left_text is not None:
                self.itemconfigure(self._chip_left_text, text=self.mode_label)
        if doctor_label is not None:
            self.doctor_label = doctor_label
            if self._chip_right_text is not None:
                self.itemconfigure(self._chip_right_text, text=self.doctor_label)

    def set_chat_expanded(self, expanded: bool) -> None:
        self.chat_expanded = bool(expanded)
        self._last_coords_cache.clear()
        self._draw_frame(dt=0.0)
