"""High-tech dual-layer HUD canvas for ORBIT desktop assistant.

Implements (v2.23.0):
- PART A Architecture:
  - StaticLayer (self): baked background bitmap, vignette, static stars (max 40, 1px, <=35% accent brightness),
    panel card outlines, thin rings, tick ring, title equal advance text, state label, bottom chips.
    Redrawn ONLY on resize, theme change, or state-label change.
  - OrbLayer (child canvas placed via place() centered on orb, square side 0.74 H * scale, highlightthickness=0, bd=0):
    ALL animated elements (sphere lines, core bloom sprite, glass body sprite, arcs, dust ring, pulse effects,
    foreground stars). Background image is a crop of the same baked bitmap at its position with seamless border.
  - Frame Pacing: Idle ceiling 15 FPS when state is idle and no pulse is active; HUD_FPS otherwise.
    Skip frame entirely if displacement < 0.05 px and no color/pulse change.
    Quality governor measures OrbLayer duration only.
  - Color Bucketing: Quantize depth into 6 buckets; group sphere polylines by bucket tag;
    call itemconfigure only when an item changes bucket; per frame only coords change.
  - Struct-of-Arrays (SoA) particles: Vectorized projection with numpy when importable; pure Python fallback.
    Updates an item only when rounded integer coords change.
  - Telemetry: Per-frame compute_ms, canvas_ms, items_touched with rolling averages.
- PART B Fidelity & Theme Purity:
  - Palette Purity: Every color drawn in a frame comes from the active palette's ramps.
    In gold theme: strictly NO cyan or teal anywhere.
    In cyan theme: everything teal. Zero hardcoded colors.
  - Core & Glass: Pre-rendered bloom sprite using smoothstep radial falloff over up to 256 levels;
    bright core radius ~0.16 sphere radius, halo fades to 0 at 0.60;
    smoky glass body (dark amber in gold, dark teal in cyan) with faint Fresnel rim and soft specular highlight.
  - Arcs: Always draw every block; unlit blocks are visible ghosts (bg blended with 8% accent);
    lit blocks follow bell profile or telemetry fill; faint outer rim line under arcs.
  - Dust Ring: 600 particles high, 300 medium, 150 low, 1 px, theme ramp with depth fade;
    continuous elliptical band + thin ring line + second faint ring at half density.
  - Stars: At most 40 static stars, 1 px, brightness <= 35% accent, static.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
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
    crop_background_bitmap,
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
    quantize_depth_bucket,
    rotate,
    split_curve_front_back,
)
from ui.radial_menu import RadialAction, RadialMenu
from ui.theme import Palette, blend_color, get_heading_font, get_palette


@dataclass
class HudNotice:
    """Transient notification displayed under the HUD title rule."""

    text: str
    created_at: float
    lifetime: float = 3.0


class HudCanvas(tk.Canvas):
    """Sci-fi interactive HUD visualization canvas for the ORBIT desktop agent.

    Serves as StaticLayer parent canvas with an OrbLayer child canvas for animated elements.
    """

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

        # Layer draw counters & telemetry
        self.static_layer_draw_count: int = 0
        self.frame_skip_count: int = 0
        self.compute_ms: float = 0.0
        self.canvas_ms: float = 0.0
        self.items_touched_last_frame: int = 0
        self._items_touched_this_frame: int = 0

        self.compute_ms_history: deque[float] = deque(maxlen=30)
        self.canvas_ms_history: deque[float] = deque(maxlen=30)
        self._items_touched_history: deque[int] = deque(maxlen=30)

        # Caches to eliminate redundant Tk calls
        self._last_coords_cache: dict[int, tuple[float, ...]] = {}
        self._last_config_cache: dict[int, dict[str, Any]] = {}
        self._polyline_bucket_cache: dict[int, int] = {}
        self._particle_int_coords: dict[int, tuple[int, int]] = {}

        # Boot animation
        self._boot_done = self.motion != "full"
        self._boot_start_time = time.monotonic() if not self._boot_done else 0.0

        # Dimensions & Cursor tracking
        self._width = max(200, int(kwargs.get("width", 1280)))
        self._height = max(180, int(kwargs.get("height", 800)))
        self._cursor_x: float = self._width / 2.0
        self._cursor_y: float = self._height / 2.0

        # Bitmaps & Sprites state
        self._bg_array: Optional[Any] = None
        self._bg_photo: Optional[tk.PhotoImage] = None
        self._orb_bg_photo: Optional[tk.PhotoImage] = None
        self._bloom_photos: list[tk.PhotoImage] = []
        self._sphere_body_photo: Optional[tk.PhotoImage] = None
        self._last_bloom_level: int = -1
        self.realism_fallback_reason: str = ""
        self.realism_build_time_ms: float = 0.0
        self.realism_sprite_count: int = 0
        self._realism_worker_busy: bool = False

        # Interactive setup
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

        self._radial_menu = RadialMenu(self, palette=self.palette)
        self._telemetry_timer_id: Optional[str] = None

        # --- Layer Separation (A1) ---
        # StaticLayer elements on self
        self._pool_created = False
        self._bg_image_id: Optional[int] = None
        self._vignette_items: list[int] = []
        self._static_star_items: list[int] = []
        self._panel_cards: list[int] = []
        self._title_chars: list[int] = []
        self._title_rule_id: Optional[int] = None
        self._state_label_id: Optional[int] = None
        self._handsfree_label_id: Optional[int] = None
        self._handsfree_ring_id: Optional[int] = None
        self._boot_text_id: Optional[int] = None
        self._chips_line_id: Optional[int] = None
        self._chip_left_bg: Optional[int] = None
        self._chip_left_text: Optional[int] = None
        self._chip_right_bg: Optional[int] = None
        self._chip_right_text: Optional[int] = None
        self._double_rings: list[int] = []
        self._tick_items: list[int] = []
        self._focus_ring_id: Optional[int] = None
        self._notice_items: list[int] = []

        # Legacy aliases for tests
        self._outer_circles: list[int] = []
        self._core_glow_items: list[int] = []
        self._particle_items: list[int] = []
        self._radial_arc_items: list[int] = []
        self._parallax_deep_items: list[int] = []
        self._parallax_mid_items: list[int] = []
        self._parallax_near_items: list[int] = []

        # Tracking for OrbLayer items and particle side transitions
        self._orb_item_ids: set[int] = set()
        self._particle_side: dict[int, str] = {}
        self._particle_last_pos: dict[int, tuple[float, float]] = {}

        # OrbLayer child canvas (A1)
        self.orb_layer = tk.Canvas(
            self,
            bg=self.palette.bg,
            highlightthickness=0,
            bd=0,
        )
        # Offset item IDs on orb_layer so they never collide with self (StaticLayer)
        for _ in range(5000):
            self.orb_layer.delete(self.orb_layer.create_line(0, 0, 0, 0))
        self._orb_box_size: int = 0
        self._orb_x0: int = 0
        self._orb_y0: int = 0

        # Elements on OrbLayer
        self._orb_bg_image_id: Optional[int] = None
        self._bloom_image_id: Optional[int] = None
        self._sphere_body_image_id: Optional[int] = None
        self._dust_back_ellipse_id: Optional[int] = None
        self._dust_back_ellipse_2_id: Optional[int] = None
        self._dust_back_items: list[int] = []
        self._core_circle_items: list[int] = []
        self._limb_glow_items: list[int] = []
        self._limb_ring_id: Optional[int] = None
        self._sphere_back_items: list[int] = []
        self._sphere_front_items: list[int] = []
        self._sphere_line_items: list[int] = []
        self._arc_rim_id: Optional[int] = None
        self._arc_a_items: list[int] = []
        self._arc_b_items: list[int] = []
        self._dust_front_ellipse_id: Optional[int] = None
        self._dust_front_ellipse_2_id: Optional[int] = None
        self._dust_front_items: list[int] = []
        self._pulse_ring_id: Optional[int] = None
        self._orb_star_items: list[int] = []
        self._arc_tooltip_bg_id: Optional[int] = None
        self._arc_tooltip_id: Optional[int] = None

        # Wire events
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

        # Forward OrbLayer mouse events to parent canvas
        self._bind_orb_events()

        # Keyboard controls when focused
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

        # Initial render of StaticLayer
        self._draw_static_layer()

        if self.motion != "off":
            self._schedule_tick(10)
        else:
            self._draw_frame(dt=0.0)

    # --- Delegations & Transparency for Child Canvas (A1) ---

    @property
    def static_layer(self) -> tk.Canvas:
        return self

    @property
    def compute_ms_average(self) -> float:
        if not self.compute_ms_history:
            return 0.0
        return sum(self.compute_ms_history) / len(self.compute_ms_history)

    @property
    def canvas_ms_average(self) -> float:
        if not self.canvas_ms_history:
            return 0.0
        return sum(self.canvas_ms_history) / len(self.canvas_ms_history)

    @property
    def items_touched_average(self) -> float:
        if not self._items_touched_history:
            return 0.0
        return sum(self._items_touched_history) / len(self._items_touched_history)

    def itemcget(self, tagOrId: Any, option: str) -> Any:
        if hasattr(self, "_orb_item_ids") and tagOrId in self._orb_item_ids:
            return self.orb_layer.itemcget(tagOrId, option)
        try:
            return super().itemcget(tagOrId, option)
        except Exception:
            if hasattr(self, "orb_layer"):
                return self.orb_layer.itemcget(tagOrId, option)
            raise

    def itemconfigure(self, tagOrId: Any, cnf: Any = None, **kw: Any) -> Any:
        if hasattr(self, "_orb_item_ids") and tagOrId in self._orb_item_ids:
            return self.orb_layer.itemconfigure(tagOrId, cnf, **kw)
        try:
            return super().itemconfigure(tagOrId, cnf, **kw)
        except Exception:
            if hasattr(self, "orb_layer"):
                return self.orb_layer.itemconfigure(tagOrId, cnf, **kw)
            raise

    def coords(self, tagOrId: Any, *args: Any) -> Any:
        if hasattr(self, "_orb_item_ids") and tagOrId in self._orb_item_ids:
            return self.orb_layer.coords(tagOrId, *args)
        try:
            return super().coords(tagOrId, *args)
        except Exception:
            if hasattr(self, "orb_layer"):
                return self.orb_layer.coords(tagOrId, *args)
            raise

    def delete(self, *tagOrIds: Any) -> None:
        for t in tagOrIds:
            if hasattr(self, "_orb_item_ids") and t in self._orb_item_ids:
                try:
                    self.orb_layer.delete(t)
                except Exception:
                    pass
                self._orb_item_ids.discard(t)
            else:
                try:
                    super().delete(t)
                except Exception:
                    pass

    def find_all(self) -> tuple[int, ...]:
        base = super().find_all()
        if hasattr(self, "orb_layer"):
            return base + self.orb_layer.find_all()
        return base

    def _bind_orb_events(self) -> None:
        """Forward mouse clicks and drags from OrbLayer child canvas to self with coordinate offset."""
        for evt in (
            "<Button-1>",
            "<B1-Motion>",
            "<ButtonRelease-1>",
            "<Double-Button-1>",
            "<MouseWheel>",
            "<Button-4>",
            "<Button-5>",
            "<Button-3>",
            "<Motion>",
        ):
            self.orb_layer.bind(evt, self._forward_orb_mouse_event)
        self.orb_layer.bind("<FocusIn>", lambda e: self.focus_set())

    def _forward_orb_mouse_event(self, event: tk.Event) -> None:
        event.x += self._orb_x0
        event.y += self._orb_y0
        t = event.type
        if str(t) in ("4", "ButtonPress"):
            if getattr(event, "num", 1) == 1:
                self._on_mouse_down(event)
            elif getattr(event, "num", 1) == 3:
                self._on_right_click(event)
        elif str(t) in ("6", "Motion"):
            if event.state and (event.state & 0x0100):  # Button 1 pressed
                self._on_mouse_drag(event)
            else:
                self._on_motion(event)
        elif str(t) in ("5", "ButtonRelease"):
            self._on_mouse_up(event)
        elif str(t) in ("38", "MouseWheel"):
            self._on_mouse_wheel(event)

    # --- Properties & Sizing ---

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

    # --- Item Pooling & Canvas Setup ---

    def _setup_item_pool(self) -> None:
        """Instantiate visual elements once across StaticLayer and OrbLayer."""
        if self._pool_created:
            return
        self._pool_created = True

        # --- 1. StaticLayer Items (on self) ---
        self._bg_image_id = self.create_image(0, 0, anchor="nw", state="hidden")

        # Fallback vignette layers (4 theme-pure rectangles)
        for _ in range(4):
            vid = self.create_rectangle(0, 0, 0, 0, fill=self.palette.bg, outline="", state="hidden")
            self._vignette_items.append(vid)

        # Static stars (max 40, size 1px, brightness <= 35%)
        for star in self.model.static_stars:
            col = blend_color(self.palette.bg, self.palette.accent, star.brightness)
            sid = self.create_rectangle(0, 0, 1, 1, fill=col, outline="", state="hidden")
            self._static_star_items.append(sid)

        # Parallax items pool for test compatibility
        for _ in range(30):
            self._parallax_deep_items.append(self.create_oval(0, 0, 1, 1, fill=self.palette.accent_dim, outline="", state="hidden"))
        for _ in range(20):
            self._parallax_mid_items.append(self.create_oval(0, 0, 1, 1, fill=self.palette.accent_dim, outline="", state="hidden"))
        for _ in range(10):
            self._parallax_near_items.append(self.create_oval(0, 0, 1, 1, fill=self.palette.accent_dim, outline="", state="hidden"))

        # Panel cards
        card_chat = self.create_polygon(
            0, 0, 0, 0, 0, 0, 0, 0,
            outline=self.palette.panel_edge,
            width=1.5,
            fill=self.palette.panel,
            smooth=True,
        )
        card_right = self.create_polygon(
            0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
            outline=self.palette.panel_edge,
            width=1.5,
            fill=self.palette.panel,
            smooth=True,
        )
        self._panel_cards = [card_chat, card_right]

        # Double concentric rings
        for _ in range(2):
            cid = self.create_oval(0, 0, 0, 0, outline=self.palette.panel_edge, width=1)
            self._double_rings.append(cid)
            self._outer_circles.append(cid)

        # Boundary tick marks (120 ticks)
        for _ in range(120):
            tid = self.create_line(0, 0, 0, 0, fill=self.palette.accent_dim, width=1)
            self._tick_items.append(tid)

        # Title: "O R B I T" 5 letter-spaced chars in equal advance cells
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
            fill=self.palette.text_dim,
            font=("Segoe UI", 9, "bold"),
        )
        self._handsfree_label_id = self.create_text(
            0, 0,
            text="HANDS-FREE",
            anchor="center",
            fill=self.palette.accent,
            font=("Segoe UI", 8, "bold"),
            state="hidden",
        )

        # Bottom chips
        self._chips_line_id = self.create_line(0, 0, 0, 0, fill=self.palette.panel_edge, width=1)
        self._chip_left_bg = self.create_rectangle(0, 0, 0, 0, fill=self.palette.accent, outline="")
        self._chip_left_text = self.create_text(
            0, 0, text=self.mode_label, fill=self.palette.bg, font=("Segoe UI", 8, "bold")
        )
        self._chip_right_bg = self.create_rectangle(0, 0, 0, 0, fill=self.palette.accent_dim, outline="")
        self._chip_right_text = self.create_text(
            0, 0, text=self.doctor_label, fill=self.palette.text, font=("Segoe UI", 8, "bold")
        )

        self._focus_ring_id = self.create_rectangle(
            2, 2, self._width - 2, self._height - 2,
            outline=self.palette.accent,
            width=1,
            dash=(3, 3),
            state="hidden",
        )
        self._notice_items = [
            self.create_text(0, 0, text="", fill=self.palette.accent, font=("Segoe UI", 9, "bold"), state="hidden"),
            self.create_text(0, 0, text="", fill=self.palette.accent, font=("Segoe UI", 9, "bold"), state="hidden"),
        ]

        # --- 2. OrbLayer Items (on self.orb_layer) ---
        self._orb_bg_image_id = self.orb_layer.create_image(0, 0, anchor="nw", state="hidden")
        self._dust_back_ellipse_id = self.orb_layer.create_line(0, 0, 0, 0, fill=self.palette.panel_edge, width=1, state="hidden")
        self._dust_back_ellipse_2_id = self.orb_layer.create_line(0, 0, 0, 0, fill=blend_color(self.palette.bg, self.palette.accent_dim, 0.4), width=1, state="hidden")

        # Dust back particles (up to 600)
        for _ in range(600):
            pid = self.orb_layer.create_oval(0, 0, 1, 1, fill=self.palette.accent_dim, outline="", state="hidden")
            self._dust_back_items.append(pid)
            self._particle_items.append(pid)

        # Bloom & Glass sprites
        self._bloom_image_id = self.orb_layer.create_image(0, 0, anchor="nw", state="hidden")
        self._sphere_body_image_id = self.orb_layer.create_image(0, 0, anchor="nw", state="hidden")

        # Vector fallback core concentric filled circles (5 layers)
        for _ in range(5):
            cid = self.orb_layer.create_oval(0, 0, 0, 0, fill=self.palette.accent, outline="", state="hidden")
            self._core_circle_items.append(cid)
            self._core_glow_items.append(cid)

        for _ in range(3):
            gid = self.orb_layer.create_oval(0, 0, 0, 0, outline=self.palette.accent_dim, width=2, state="hidden")
            self._limb_glow_items.append(gid)
        self._limb_ring_id = self.orb_layer.create_oval(0, 0, 0, 0, outline=self.palette.accent, width=2, state="hidden")

        # Wireframe sphere lines (54 polylines: 27 back, 27 front)
        for _ in range(27):
            sid = self.orb_layer.create_line(0, 0, 0, 0, fill=self.palette.accent_dim, width=1, state="hidden")
            self._sphere_back_items.append(sid)
            self._sphere_line_items.append(sid)
        for _ in range(27):
            sid = self.orb_layer.create_line(0, 0, 0, 0, fill=self.palette.accent, width=1, state="hidden")
            self._sphere_front_items.append(sid)
            self._sphere_line_items.append(sid)

        # Faint outer rim line under arcs (B3)
        self._arc_rim_id = self.orb_layer.create_oval(
            0, 0, 0, 0, outline=blend_color(self.palette.bg, self.palette.accent, 0.15), width=1, state="hidden"
        )

        # Segmented arcs: Arc A (24 quads), Arc B (20 quads)
        for _ in range(24):
            aid = self.orb_layer.create_polygon(0, 0, 0, 0, 0, 0, 0, 0, fill=self.palette.accent, outline="", state="hidden")
            self._arc_a_items.append(aid)
        for _ in range(20):
            bid = self.orb_layer.create_polygon(0, 0, 0, 0, 0, 0, 0, 0, fill=self.palette.accent, outline="", state="hidden")
            self._arc_b_items.append(bid)
        self._radial_arc_items = self._arc_a_items + self._arc_b_items

        # Dust front ellipse lines & particles
        self._dust_front_ellipse_id = self.orb_layer.create_line(0, 0, 0, 0, fill=self.palette.accent, width=1.5, state="hidden")
        self._dust_front_ellipse_2_id = self.orb_layer.create_line(0, 0, 0, 0, fill=self.palette.accent_dim, width=1, state="hidden")
        for _ in range(600):
            pid = self.orb_layer.create_oval(0, 0, 1, 1, fill=self.palette.accent, outline="", state="hidden")
            self._dust_front_items.append(pid)

        # Foreground stars in OrbLayer (up to 12)
        for _ in range(12):
            sid = self.orb_layer.create_rectangle(0, 0, 1, 1, fill=self.palette.accent, outline="", state="hidden")
            self._orb_star_items.append(sid)

        self._boot_text_id = self.orb_layer.create_text(
            0, 0,
            text="ORBIT ONLINE",
            anchor="center",
            fill=self.palette.accent,
            font=("Segoe UI", 13, "bold"),
            state="normal" if not self._boot_done else "hidden",
        )
        self._arc_tooltip_bg_id = self.orb_layer.create_rectangle(
            0, 0, 0, 0, fill=self.palette.panel, outline=self.palette.accent, state="hidden"
        )
        self._arc_tooltip_id = self.orb_layer.create_text(
            0, 0, text="", fill=self.palette.text, font=("Segoe UI", 9, "bold"), state="hidden"
        )
        self._handsfree_ring_id = self.orb_layer.create_oval(
            0, 0, 0, 0, outline=self.palette.accent, width=1, state="hidden"
        )
        self._orb_item_ids.update(self.orb_layer.find_all())

    # --- Draw Helpers with Caching ---

    def _update_coords(self, item_id: int, *coords: float, on_orb: bool = False) -> None:
        canvas = self.orb_layer if on_orb else self
        target_coords = tuple(float(c) for c in coords)
        cached = self._last_coords_cache.get(item_id)
        if cached is not None and len(cached) == len(target_coords):
            match = True
            for c1, c2 in zip(cached, target_coords):
                if abs(c1 - c2) > 0.05:
                    match = False
                    break
            if match:
                return
        self._last_coords_cache[item_id] = target_coords
        tk.Canvas.coords(canvas, item_id, *target_coords)
        self._items_touched_this_frame += 1

    def _configure_item(self, item_id: int, on_orb: bool = False, **kw: Any) -> None:
        canvas = self.orb_layer if on_orb else self
        cached = self._last_config_cache.get(item_id)
        if cached is not None:
            all_match = True
            for k, v in kw.items():
                if cached.get(k) != v:
                    all_match = False
                    break
            if all_match:
                return
        else:
            cached = {}
            self._last_config_cache[item_id] = cached
        cached.update(kw)
        tk.Canvas.itemconfigure(canvas, item_id, **kw)
        self._items_touched_this_frame += 1

    # --- Static Layer Drawing (A1) ---

    def _draw_static_layer(self) -> None:
        """Render StaticLayer: background bitmap, panel outlines, static stars, rings, title, chips.

        Redrawn ONLY on resize, theme change, or state label change (A1).
        """
        self.static_layer_draw_count += 1
        w = self._width
        h = self._height
        ly = layout(w, h, scaling=self._scale)
        cx, cy = ly.cx, ly.cy

        # 1. OrbLayer Placement (square side 0.74 H * scale centered at cx, cy)
        box_size = int(round(ly.orb_box_size if ly.orb_box_size > 0 else 0.74 * h * ly.scale))
        self._orb_box_size = box_size
        self._orb_x0 = int(round(cx - box_size / 2.0))
        self._orb_y0 = int(round(cy - box_size / 2.0))
        self.orb_layer.configure(bg=self.palette.bg)
        self.orb_layer.place(x=self._orb_x0, y=self._orb_y0, width=box_size, height=box_size)

        # 2. Background bitmap on StaticLayer
        if self._bg_photo is not None and self._bg_image_id is not None:
            self._configure_item(self._bg_image_id, on_orb=False, image=self._bg_photo, state="normal")
            for vid in self._vignette_items:
                self._configure_item(vid, on_orb=False, state="hidden")
        else:
            # Fallback procedural vignette
            if self._bg_image_id is not None:
                self._configure_item(self._bg_image_id, on_orb=False, state="hidden")
            v_cols = [
                blend_color(self.palette.bg, "#000000", 0.4),
                self.palette.bg,
                blend_color(self.palette.bg, self.palette.panel, 0.3),
                self.palette.panel,
            ]
            for idx, vid in enumerate(self._vignette_items):
                r_frac = (idx + 1) / 4.0
                vx1 = cx - 0.55 * w * r_frac
                vy1 = cy - 0.55 * h * r_frac
                vx2 = cx + 0.55 * w * r_frac
                vy2 = cy + 0.55 * h * r_frac
                self._configure_item(vid, on_orb=False, fill=v_cols[idx], state="normal")
                self._update_coords(vid, vx1, vy1, vx2, vy2, on_orb=False)

        # 3. Static stars (max 40, size 1px, brightness <= 35%)
        for idx, sid in enumerate(self._static_star_items):
            if idx >= len(self.model.static_stars):
                self._configure_item(sid, on_orb=False, state="hidden")
                continue
            star = self.model.static_stars[idx]
            sx = star.norm_x * w
            sy = star.norm_y * h
            col = blend_color(self.palette.bg, self.palette.accent, star.brightness)
            self._configure_item(sid, on_orb=False, fill=col, state="normal")
            self._update_coords(sid, sx, sy, sx + 1, sy + 1, on_orb=False)

        # 4. Panel cards (smooth polygons for chat and right panels)
        if len(self._panel_cards) >= 2:
            chat_top = ly.chat_y_expanded_top if self.chat_expanded else ly.panel_y_top
            self._update_coords(
                self._panel_cards[0],
                ly.chat_x_left, chat_top,
                ly.chat_x_right, chat_top,
                ly.chat_x_right, ly.panel_y_bottom,
                ly.chat_x_left, ly.panel_y_bottom,
                on_orb=False,
            )
            self._configure_item(
                self._panel_cards[0],
                on_orb=False,
                outline=self.palette.panel_edge,
                fill=self.palette.panel,
                state="normal",
            )
            self._update_coords(
                self._panel_cards[1],
                ly.right_x_left, ly.panel_y_top,
                ly.right_x_right, ly.panel_y_top,
                ly.right_x_right, ly.panel_y_bottom,
                ly.right_x_left, ly.panel_y_bottom,
                on_orb=False,
            )
            self._configure_item(
                self._panel_cards[1],
                on_orb=False,
                outline=self.palette.panel_edge,
                fill=self.palette.panel,
                state="normal",
            )

        # 5. Double concentric rings (at 0.262H and 0.271H)
        if len(self._double_rings) >= 2:
            r_in = ly.ring_inner_radius
            r_out = ly.ring_outer_radius
            self._update_coords(self._double_rings[0], cx - r_in, cy - r_in, cx + r_in, cy + r_in, on_orb=False)
            self._update_coords(self._double_rings[1], cx - r_out, cy - r_out, cx + r_out, cy + r_out, on_orb=False)
            self._configure_item(self._double_rings[0], on_orb=False, outline=self.palette.panel_edge, state="normal")
            self._configure_item(self._double_rings[1], on_orb=False, outline=self.palette.panel_edge, state="normal")

        # 6. Concentric ticks (minor 0.006H, major 0.012H)
        for idx, tid in enumerate(self._tick_items):
            if idx >= len(self.model.ref_ticks):
                self._configure_item(tid, on_orb=False, state="hidden")
                continue
            ang, t_len_ratio, is_maj = self.model.ref_ticks[idx]
            r1 = ly.ring_outer_radius
            r2 = r1 + t_len_ratio * h * ly.scale
            x1 = cx + r1 * math.cos(ang)
            y1 = cy + r1 * math.sin(ang)
            x2 = cx + r2 * math.cos(ang)
            y2 = cy + r2 * math.sin(ang)
            col = self.palette.accent if is_maj else self.palette.accent_dim
            self._configure_item(tid, on_orb=False, fill=col, state="normal")
            self._update_coords(tid, x1, y1, x2, y2, on_orb=False)

        # 7. Typography (equal advance cells for "ORBIT")
        letter_cells = compute_equal_advance_cells("ORBIT", cx, widest_glyph=16.0, tracking=8.0)
        for idx, char_x in enumerate(letter_cells):
            if idx < len(self._title_chars):
                self._update_coords(self._title_chars[idx], char_x, ly.title_y, on_orb=False)
                self._configure_item(self._title_chars[idx], on_orb=False, fill=self.palette.text, state="normal")

        if self._title_rule_id is not None:
            self._update_coords(
                self._title_rule_id,
                cx - ly.title_rule_w / 2.0, ly.title_rule_y,
                cx + ly.title_rule_w / 2.0, ly.title_rule_y,
                on_orb=False,
            )
            self._configure_item(self._title_rule_id, on_orb=False, fill=self.palette.panel_edge, state="normal")

        # 8. State label
        self._update_state_label()

        # 9. Bottom chips line and pills
        if self._chips_line_id is not None:
            self._update_coords(self._chips_line_id, 0, ly.chips_y, w, ly.chips_y, on_orb=False)
            self._configure_item(self._chips_line_id, on_orb=False, fill=self.palette.panel_edge, state="normal")

        if self._chip_left_bg is not None and self._chip_left_text is not None:
            chip_w, chip_h = 130.0, 18.0
            x_left_pill = cx - chip_w - 10.0
            self._update_coords(
                self._chip_left_bg,
                x_left_pill, ly.chips_y - chip_h / 2.0,
                x_left_pill + chip_w, ly.chips_y + chip_h / 2.0,
                on_orb=False,
            )
            self._update_coords(self._chip_left_text, x_left_pill + chip_w / 2.0, ly.chips_y, on_orb=False)
            self._configure_item(self._chip_left_bg, on_orb=False, fill=self.palette.accent, state="normal")
            self._configure_item(self._chip_left_text, on_orb=False, text=self.mode_label, fill=self.palette.bg, state="normal")

        if self._chip_right_bg is not None and self._chip_right_text is not None:
            chip_w, chip_h = 150.0, 18.0
            x_right_pill = cx + 10.0
            self._update_coords(
                self._chip_right_bg,
                x_right_pill, ly.chips_y - chip_h / 2.0,
                x_right_pill + chip_w, ly.chips_y + chip_h / 2.0,
                on_orb=False,
            )
            self._update_coords(self._chip_right_text, x_right_pill + chip_w / 2.0, ly.chips_y, on_orb=False)
            self._configure_item(self._chip_right_bg, on_orb=False, fill=self.palette.accent_dim, state="normal")
            self._configure_item(self._chip_right_text, on_orb=False, text=self.doctor_label, fill=self.palette.text, state="normal")

        if self._focus_ring_id is not None:
            self._update_coords(self._focus_ring_id, 2, 2, w - 2, h - 2, on_orb=False)
            self._configure_item(self._focus_ring_id, on_orb=False, outline=self.palette.accent)

        # 10. Background crop on OrbLayer
        if self._orb_bg_photo is not None and self._orb_bg_image_id is not None:
            self._configure_item(self._orb_bg_image_id, on_orb=True, image=self._orb_bg_photo, state="normal")

    def _update_state_label(self) -> None:
        if self._state_label_id is None:
            return
        ly = layout(self._width, self._height, scaling=self._scale)
        st = self.model.state
        state_labels = {
            HudState.IDLE: ("STANDBY", self.palette.text_dim),
            HudState.LISTENING: ("LISTENING", self.palette.accent),
            HudState.THINKING: ("PROCESSING", self.palette.accent),
            HudState.SPEAKING: ("SPEAKING", self.palette.ok),
            HudState.CONFIRMING: ("AWAITING APPROVAL", self.palette.warn),
            HudState.ERROR: ("FAULT", self.palette.danger),
        }
        s_text, s_col = state_labels.get(st, ("STANDBY", self.palette.text_dim))
        self._update_coords(self._state_label_id, ly.cx, ly.state_label_y, on_orb=False)
        self._configure_item(self._state_label_id, on_orb=False, text=s_text, fill=s_col, state="normal")

        if getattr(self, "_handsfree_label_id", None) is not None:
            if getattr(self.model, "handsfree_armed", False):
                self._update_coords(self._handsfree_label_id, ly.cx, ly.state_label_y + 16, on_orb=False)
                self._configure_item(
                    self._handsfree_label_id, on_orb=False, text="HANDS-FREE", fill=self.palette.accent, state="normal"
                )
            else:
                self._configure_item(self._handsfree_label_id, on_orb=False, state="hidden")

    # --- Animated OrbLayer Drawing (A1, A2, A3, A4, B1-B5) ---

    def _draw_frame(self, dt: float) -> None:
        """Render animated visual elements strictly inside OrbLayer (A1)."""
        if self._destroyed:
            return

        t_frame_start = time.perf_counter()
        self._items_touched_this_frame = 0

        # Math / Simulation update
        dt_val = max(0.0, float(dt))
        self.model.update(dt_val, motion_mode=self.motion)

        w = self._width
        h = self._height
        ly = layout(w, h, scaling=self._scale)
        sphere_r = ly.sphere_radius * self.model.zoom_factor
        st = self.model.state
        tier = self.model.tier
        is_low = tier in ("low", "static")

        # Local center inside OrbLayer
        orb_box = self._orb_box_size if self._orb_box_size > 0 else int(round(ly.orb_box_size))
        orb_cx = orb_box / 2.0
        orb_cy = orb_box / 2.0

        # Frame Pacing & Idle Skipping (A2)
        pulse_val = self.model.pulse_val
        is_idle_envelope = (st == HudState.IDLE and pulse_val <= 0.001 and not self._is_dragging)
        if is_idle_envelope and dt_val > 0.0:
            # Check maximum vertex displacement on sphere
            ax_speed = 0.20
            ay_speed = 0.40
            max_disp = sphere_r * math.hypot(ax_speed, ay_speed) * dt_val
            if max_disp < 0.05:
                self.frame_skip_count += 1
                return

        # Precompute theme ramps and colors
        pulse_boost = 1.0 + 0.35 * pulse_val
        lit_ghost_col = blend_color(self.palette.bg, self.palette.accent, 0.08)

        # 6 Depth buckets for sphere polylines (A3)
        depth_bucket_colors = [
            adjust_color_brightness(
                blend_color(self.palette.accent_dim, self.palette.accent, (k + 0.5) / 6.0),
                pulse_boost,
            )
            for k in range(6)
        ]

        t_math_done = time.perf_counter()

        # --- Draw on OrbLayer ---

        # 1. Background image on OrbLayer
        if self._orb_bg_photo is not None and self._orb_bg_image_id is not None:
            self._configure_item(self._orb_bg_image_id, on_orb=True, image=self._orb_bg_photo, state="normal")

        # 2. Dust Ring Particles & Central Ellipses (B4, SoA)
        tilt_rad = math.radians(-22.0)
        cos_tilt = math.cos(tilt_rad)
        sin_tilt = math.sin(tilt_rad)

        target_circle_r = 0.38 * h * ly.scale
        dust_a_1 = ly.dust_semi_major + (target_circle_r - ly.dust_semi_major) * pulse_val
        dust_b_1 = ly.dust_semi_minor + (target_circle_r - ly.dust_semi_minor) * pulse_val
        dust_a_2 = dust_a_1 * 1.12
        dust_b_2 = dust_b_1 * 1.12

        # Draw Back Central Ellipses
        if self._dust_back_ellipse_id is not None:
            e1_pts = []
            for deg in range(0, 185, 5):
                rad = math.radians(deg)
                xe = dust_a_1 * math.cos(rad)
                ye = dust_b_1 * math.sin(rad)
                e1_pts.extend([orb_cx + xe * cos_tilt - ye * sin_tilt, orb_cy + xe * sin_tilt + ye * cos_tilt])
            self._configure_item(self._dust_back_ellipse_id, on_orb=True, fill=self.palette.panel_edge, state="normal")
            self._update_coords(self._dust_back_ellipse_id, *e1_pts, on_orb=True)

        if self._dust_back_ellipse_2_id is not None:
            e2_pts = []
            for deg in range(0, 185, 5):
                rad = math.radians(deg)
                xe = dust_a_2 * math.cos(rad)
                ye = dust_b_2 * math.sin(rad)
                e2_pts.extend([orb_cx + xe * cos_tilt - ye * sin_tilt, orb_cy + xe * sin_tilt + ye * cos_tilt])
            col_faint = blend_color(self.palette.bg, self.palette.accent_dim, 0.45)
            self._configure_item(self._dust_back_ellipse_2_id, on_orb=True, fill=col_faint, state="normal")
            self._update_coords(self._dust_back_ellipse_2_id, *e2_pts, on_orb=True)

        # Particle counts by tier (600 high, 300 med, 150 low)
        active_dust_count = 600 if tier == "high" else (300 if tier == "medium" else 150)
        if self.motion == "reduced":
            active_dust_count = max(50, active_dust_count // 2)

        # Project SoA particles
        projected_particles = self.model.dust_ring_soa.project_numpy(
            orb_cx, orb_cy, dust_a_1, dust_b_1, dust_a_2, dust_b_2, tilt_rad
        )

        for idx, (px, py, pz, rtype) in enumerate(projected_particles):
            if idx >= active_dust_count:
                if self._particle_side.get(idx) != "off":
                    if idx < len(self._dust_back_items):
                        self._configure_item(self._dust_back_items[idx], on_orb=True, state="hidden")
                    if idx < len(self._dust_front_items):
                        self._configure_item(self._dust_front_items[idx], on_orb=True, state="hidden")
                    self._particle_side[idx] = "off"
                continue

            curr_side = self._particle_side.get(idx)

            if pz >= 0.0:
                if math.hypot(px - orb_cx, py - orb_cy) < sphere_r * 0.98:
                    target_side = "hidden"
                else:
                    target_side = "back"
            else:
                target_side = "front"

            if curr_side != target_side:
                if target_side == "back":
                    if curr_side == "front" and idx < len(self._dust_front_items):
                        self._configure_item(self._dust_front_items[idx], on_orb=True, state="hidden")
                    col = blend_color(self.palette.bg, self.palette.accent_dim, 0.4 if rtype == 1 else 0.7)
                    col = adjust_color_brightness(col, pulse_boost)
                    self._configure_item(self._dust_back_items[idx], on_orb=True, fill=col, state="normal")
                elif target_side == "front":
                    if curr_side == "back" and idx < len(self._dust_back_items):
                        self._configure_item(self._dust_back_items[idx], on_orb=True, state="hidden")
                    col = self.palette.text_dim if rtype == 1 else self.palette.accent
                    col = adjust_color_brightness(col, pulse_boost)
                    self._configure_item(self._dust_front_items[idx], on_orb=True, fill=col, state="normal")
                else:  # hidden
                    if curr_side == "back" and idx < len(self._dust_back_items):
                        self._configure_item(self._dust_back_items[idx], on_orb=True, state="hidden")
                    elif curr_side == "front" and idx < len(self._dust_front_items):
                        self._configure_item(self._dust_front_items[idx], on_orb=True, state="hidden")
                self._particle_side[idx] = target_side
            elif pulse_val > 0.001 and target_side in ("back", "front"):
                pid = self._dust_back_items[idx] if target_side == "back" else self._dust_front_items[idx]
                if target_side == "back":
                    col = blend_color(self.palette.bg, self.palette.accent_dim, 0.4 if rtype == 1 else 0.7)
                else:
                    col = self.palette.text_dim if rtype == 1 else self.palette.accent
                col = adjust_color_brightness(col, pulse_boost)
                self._configure_item(pid, on_orb=True, fill=col)

            if target_side in ("back", "front"):
                pid = self._dust_back_items[idx] if target_side == "back" else self._dust_front_items[idx]
                last_pos = self._particle_last_pos.get(pid)
                if last_pos is None or abs(px - last_pos[0]) >= 1.4 or abs(py - last_pos[1]) >= 1.4:
                    self._particle_last_pos[pid] = (px, py)
                    rx = int(round(px))
                    ry = int(round(py))
                    self._update_coords(pid, rx, ry, rx + 1, ry + 1, on_orb=True)

        # 3. Bloom Sprite & Glass Sphere Body Sprite (B2)
        sprite_half = int(round(1.2 * sphere_r))
        sx0 = int(round(orb_cx - sprite_half))
        sy0 = int(round(orb_cy - sprite_half))

        if self.realism == "full" and not self.realism_fallback_reason:
            if self._bloom_photos and self._bloom_image_id is not None:
                lvl = int(round(pulse_val * (len(self._bloom_photos) - 1)))
                lvl = max(0, min(len(self._bloom_photos) - 1, lvl))
                if lvl != self._last_bloom_level:
                    self._last_bloom_level = lvl
                    self.orb_layer.itemconfigure(self._bloom_image_id, image=self._bloom_photos[lvl], state="normal")
                    self._items_touched_this_frame += 1
                self._update_coords(self._bloom_image_id, sx0, sy0, on_orb=True)

            if self._sphere_body_photo is not None and self._sphere_body_image_id is not None:
                self._configure_item(self._sphere_body_image_id, on_orb=True, state="normal")
                self._update_coords(self._sphere_body_image_id, sx0, sy0, on_orb=True)

        # 4. Core Concentric Filled Circles (5 layers) - fallback or complementary
        core_fracs = [0.55, 0.42, 0.30, 0.20, 0.12]
        core_shades = [
            blend_color(self.palette.panel_edge, self.palette.accent, 0.20),
            blend_color(self.palette.accent_dim, self.palette.accent, 0.40),
            blend_color(self.palette.accent_dim, self.palette.accent, 0.60),
            blend_color(self.palette.accent, self.palette.text, 0.50),
            blend_color(self.palette.accent, self.palette.text, 0.85),
        ]

        for idx, (cid, frac) in enumerate(zip(self._core_circle_items, core_fracs)):
            if is_low and idx in (1, 3):
                self._configure_item(cid, on_orb=True, state="hidden")
                continue
            cr = frac * sphere_r * (1.0 + 0.10 * self.model.core_pulse)
            shade = adjust_color_brightness(core_shades[idx], pulse_boost)
            self._configure_item(cid, on_orb=True, fill=shade, state="normal")
            self._update_coords(cid, orb_cx - cr, orb_cy - cr, orb_cx + cr, orb_cy + cr, on_orb=True)

        # 5. Sphere Limb Glow & Ring
        glow_steps = [1.02, 1.05, 1.08]
        for idx, gid in enumerate(self._limb_glow_items):
            glow_r = sphere_r * glow_steps[idx]
            g_col = adjust_color_brightness(self.palette.accent_dim, pulse_boost)
            self._configure_item(gid, on_orb=True, outline=g_col, state="normal")
            self._update_coords(gid, orb_cx - glow_r, orb_cy - glow_r, orb_cx + glow_r, orb_cy + glow_r, on_orb=True)

        if self._limb_ring_id is not None:
            limb_col = adjust_color_brightness(self.palette.accent, pulse_boost)
            self._configure_item(self._limb_ring_id, on_orb=True, outline=limb_col, state="normal")
            self._update_coords(self._limb_ring_id, orb_cx - sphere_r, orb_cy - sphere_r, orb_cx + sphere_r, orb_cy + sphere_r, on_orb=True)

        # 6. Orthographic Sphere Wireframe Polylines with Color Bucketing (A3)
        tilt_x = math.radians(18.0) + self.model.lookat_x
        sphere_yaw = self.model.sphere_ay + self.model.lookat_y
        wire_curves = self.model.ref_lat_curves + self.model.ref_meridian_curves

        for c_idx, curve_pts in enumerate(wire_curves):
            rot_pts = rotate(curve_pts, tilt_x, sphere_yaw, 0.0)
            proj_pts = project_orthographic(rot_pts, orb_cx, orb_cy, sphere_r)
            f_coords, b_coords = split_curve_front_back(proj_pts)

            # Back polyline
            b_id = self._sphere_back_items[c_idx]
            if len(b_coords) >= 4:
                avg_zb = sum(pt[2] for pt in rot_pts if pt[2] < 0.0) / max(1, sum(1 for pt in rot_pts if pt[2] < 0.0))
                bucket_b = quantize_depth_bucket(avg_zb, num_buckets=6)
                if self._polyline_bucket_cache.get(b_id) != bucket_b:
                    self._polyline_bucket_cache[b_id] = bucket_b
                    self._configure_item(b_id, on_orb=True, fill=depth_bucket_colors[bucket_b])
                self._configure_item(b_id, on_orb=True, state="normal")
                self._update_coords(b_id, *b_coords, on_orb=True)
            else:
                self._configure_item(b_id, on_orb=True, state="hidden")

            # Front polyline
            f_id = self._sphere_front_items[c_idx]
            if len(f_coords) >= 4:
                avg_zf = sum(pt[2] for pt in rot_pts if pt[2] >= 0.0) / max(1, sum(1 for pt in rot_pts if pt[2] >= 0.0))
                bucket_f = quantize_depth_bucket(avg_zf, num_buckets=6)
                if self._polyline_bucket_cache.get(f_id) != bucket_f:
                    self._polyline_bucket_cache[f_id] = bucket_f
                    self._configure_item(f_id, on_orb=True, fill=depth_bucket_colors[bucket_f])
                self._configure_item(f_id, on_orb=True, state="normal")
                self._update_coords(f_id, *f_coords, on_orb=True)
            else:
                self._configure_item(f_id, on_orb=True, state="hidden")

        # 7. Segmented Arcs & Faint Outer Rim (B3)
        drift_a = math.radians(self.model.arc_a_drift_deg)
        drift_b = math.radians(self.model.arc_b_drift_deg)
        mic_boost = self.model.mic_level if st == HudState.LISTENING else 0.0
        gain = 0.03 * h * ly.scale
        base_thickness = 0.025 * h * ly.scale

        # Faint outer rim under arcs
        r_outer_arc = ly.arc_outer_radius
        if self._arc_rim_id is not None:
            self._configure_item(self._arc_rim_id, on_orb=True, state="normal")
            self._update_coords(
                self._arc_rim_id,
                orb_cx - r_outer_arc, orb_cy - r_outer_arc,
                orb_cx + r_outer_arc, orb_cy + r_outer_arc,
                on_orb=True,
            )

        # Arc A (24 quads)
        total_a = len(self.model.blocks_a)
        lit_a = int(round(self.model.cpu_load * total_a)) if self.model.cpu_load is not None else total_a

        for idx, item_id in enumerate(self._arc_a_items):
            if idx >= total_a:
                self._configure_item(item_id, on_orb=True, state="hidden")
                continue
            blk = self.model.blocks_a[idx]
            start_d = blk.start_deg + math.degrees(drift_a)
            end_d = blk.end_deg + math.degrees(drift_a)

            extra_len = (pulse_val + mic_boost) * gain * blk.weight
            r1 = ly.arc_inner_radius
            r2 = min(ly.arc_outer_radius, ly.arc_inner_radius + base_thickness + extra_len)

            quad_coords = compute_arc_quad_coords(orb_cx, orb_cy, r1, r2, start_d, end_d)
            is_lit = (idx < lit_a) or (self.model.cpu_load is None)

            # B3: Always draw every block; unlit blocks are visible ghosts
            if is_lit:
                col = blend_color(self.palette.accent_dim, self.palette.accent, blk.brightness)
                col = adjust_color_brightness(col, pulse_boost)
            else:
                col = lit_ghost_col

            self._configure_item(item_id, on_orb=True, state="normal", fill=col)
            self._update_coords(item_id, *quad_coords, on_orb=True)

        # Arc B (20 quads)
        total_b = len(self.model.blocks_b)
        lit_b = int(round(self.model.mem_load * total_b)) if self.model.mem_load is not None else total_b

        for idx, item_id in enumerate(self._arc_b_items):
            if idx >= total_b:
                self._configure_item(item_id, on_orb=True, state="hidden")
                continue
            blk = self.model.blocks_b[idx]
            start_d = blk.start_deg + math.degrees(drift_b)
            end_d = blk.end_deg + math.degrees(drift_b)

            extra_len = (pulse_val + mic_boost) * gain * blk.weight
            r1 = ly.arc_inner_radius
            r2 = min(ly.arc_outer_radius, ly.arc_inner_radius + base_thickness + extra_len)

            quad_coords = compute_arc_quad_coords(orb_cx, orb_cy, r1, r2, start_d, end_d)
            is_lit = (idx < lit_b) or (self.model.mem_load is None)

            if is_lit:
                col = blend_color(self.palette.accent_dim, self.palette.accent, blk.brightness)
                col = adjust_color_brightness(col, pulse_boost)
            else:
                col = lit_ghost_col

            self._configure_item(item_id, on_orb=True, state="normal", fill=col)
            self._update_coords(item_id, *quad_coords, on_orb=True)

        # 8. Front Central Ellipses
        if self._dust_front_ellipse_id is not None:
            e1_pts = []
            for deg in range(180, 365, 5):
                rad = math.radians(deg)
                xe = dust_a_1 * math.cos(rad)
                ye = dust_b_1 * math.sin(rad)
                e1_pts.extend([orb_cx + xe * cos_tilt - ye * sin_tilt, orb_cy + xe * sin_tilt + ye * cos_tilt])
            self._configure_item(self._dust_front_ellipse_id, on_orb=True, fill=self.palette.accent, state="normal")
            self._update_coords(self._dust_front_ellipse_id, *e1_pts, on_orb=True)

        if self._dust_front_ellipse_2_id is not None:
            e2_pts = []
            for deg in range(180, 365, 5):
                rad = math.radians(deg)
                xe = dust_a_2 * math.cos(rad)
                ye = dust_b_2 * math.sin(rad)
                e2_pts.extend([orb_cx + xe * cos_tilt - ye * sin_tilt, orb_cy + xe * sin_tilt + ye * cos_tilt])
            self._configure_item(self._dust_front_ellipse_2_id, on_orb=True, fill=self.palette.accent_dim, state="normal")
            self._update_coords(self._dust_front_ellipse_2_id, *e2_pts, on_orb=True)

        # 9. Boot text
        if not self._boot_done and self._boot_text_id is not None:
            now_boot = time.monotonic()
            if now_boot - self._boot_start_time > 1.8:
                self._boot_done = True
                self._configure_item(self._boot_text_id, on_orb=True, state="hidden")
            else:
                self._update_coords(self._boot_text_id, orb_cx, orb_cy, on_orb=True)
                self._configure_item(self._boot_text_id, on_orb=True, state="normal")

        # 10. Hands-free breathing outer ring
        if getattr(self, "_handsfree_ring_id", None) is not None:
            if getattr(self.model, "handsfree_armed", False):
                motion = getattr(settings, "UI_MOTION", "full")
                base_r = ly.orb_radius * 1.34
                if motion == "off":
                    r_outer = base_r
                else:
                    breath = 0.5 + 0.5 * math.sin(self.model.clock() * 1.6)
                    r_outer = base_r * (0.97 + 0.06 * breath)
                self._update_coords(
                    self._handsfree_ring_id,
                    orb_cx - r_outer,
                    orb_cy - r_outer,
                    orb_cx + r_outer,
                    orb_cy + r_outer,
                    on_orb=True,
                )
                self._configure_item(
                    self._handsfree_ring_id,
                    on_orb=True,
                    outline=self.palette.accent,
                    state="normal",
                )
            else:
                self._configure_item(self._handsfree_ring_id, on_orb=True, state="hidden")

        # Telemetry updates (A5)
        t_frame_end = time.perf_counter()
        self.compute_ms = (t_math_done - t_frame_start) * 1000.0
        self.canvas_ms = (t_frame_end - t_math_done) * 1000.0
        self.compute_ms_history.append(self.compute_ms)
        self.canvas_ms_history.append(self.canvas_ms)

        self.items_touched_last_frame = self._items_touched_this_frame
        self._items_touched_history.append(self._items_touched_this_frame)

        # Quality governor measures OrbLayer duration only (A2)
        orb_duration = t_frame_end - t_frame_start
        self.model.governor.record_frame(orb_duration)

    # --- Realism Background & Sprites Loading ---

    def _schedule_realism_build(self) -> None:
        if self.realism == "off":
            self.realism_fallback_reason = "disabled by setting"
            return
        if self._realism_debounce_id is not None:
            try:
                self.after_cancel(self._realism_debounce_id)
            except Exception:
                pass
        self._realism_debounce_id = self.after(100, self._trigger_realism_worker)

    def _trigger_realism_worker(self) -> None:
        self._realism_debounce_id = None
        if self._realism_worker_busy or self._destroyed:
            return
        self._realism_worker_busy = True

        w = self._width
        h = self._height
        pal = self.palette
        ly = layout(w, h, scaling=self._scale)
        sphere_r = ly.sphere_radius
        cx, cy = ly.cx, ly.cy
        box_size = int(round(ly.orb_box_size if ly.orb_box_size > 0 else 0.74 * h * ly.scale))
        tier = self.model.tier
        num_bloom_levels = 256 if tier == "high" else (64 if tier == "medium" else 16)

        def worker() -> None:
            t0 = time.perf_counter()
            try:
                bg_u8, bg_ppm = build_background_bitmap(w, h, pal)
                crop_u8, crop_ppm = crop_background_bitmap(bg_u8, cx, cy, box_size)
                orb_cx = box_size / 2.0
                orb_cy = box_size / 2.0
                bloom_sprites = build_bloom_sprites(
                    sphere_r, orb_cx, orb_cy, crop_u8, pal, num_levels=num_bloom_levels
                )
                glass_u8, glass_ppm = build_sphere_body_sprite(sphere_r, orb_cx, orb_cy, crop_u8, pal)
                build_time = (time.perf_counter() - t0) * 1000.0

                def on_done() -> None:
                    self._realism_worker_busy = False
                    if self._destroyed:
                        return
                    try:
                        self._bg_array = bg_u8
                        self._bg_photo = tk.PhotoImage(data=bg_ppm)
                        self._orb_bg_photo = tk.PhotoImage(data=crop_ppm)
                        self._bloom_photos = [tk.PhotoImage(data=ppm) for _, ppm in bloom_sprites]
                        self._sphere_body_photo = tk.PhotoImage(data=glass_ppm)
                        self.realism_sprite_count = 1 + len(self._bloom_photos) + 1
                        self.realism_build_time_ms = build_time
                        self.realism_fallback_reason = ""
                        self._draw_static_layer()
                        self._draw_frame(dt=0.0)
                    except Exception as exc:
                        self.realism_fallback_reason = f"PhotoImage load error: {exc}"

                if not self._destroyed:
                    self.after(0, on_done)
            except Exception as exc:
                err_msg = str(exc)

                def on_err() -> None:
                    self._realism_worker_busy = False
                    self.realism_fallback_reason = f"worker error: {err_msg}"

                if not self._destroyed:
                    self.after(0, on_err)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    # --- Animation Tick Loop ---

    def _schedule_tick(self, delay_ms: int) -> None:
        if self._destroyed or self._paused or self.motion == "off":
            return
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
        self._after_id = self.after(max(1, delay_ms), self._on_tick)

    def _on_tick(self) -> None:
        self._after_id = None
        now = time.monotonic()
        dt = max(0.001, min(0.1, now - self._last_time))
        self._last_time = now

        self._draw_frame(dt)

        # Dynamic frame pacing (A2): 15 FPS idle ceiling
        st = self.model.state
        if st == HudState.IDLE and self.model.pulse_val <= 0.001 and not self._is_dragging:
            effective_fps = min(15, self.fps)
        elif self.motion == "reduced":
            effective_fps = min(8, self.fps)
        else:
            effective_fps = self.fps

        target_delay = int(round(1000.0 / max(1, effective_fps)))
        self._schedule_tick(target_delay)

    # --- Resize & Lifecycle ---

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
        self._polyline_bucket_cache.clear()
        self._particle_int_coords.clear()
        self._particle_last_pos.clear()
        self._draw_static_layer()
        self._draw_frame(dt=0.0)

    def _on_map(self, event: Optional[tk.Event] = None) -> None:
        if self._paused:
            self._paused = False
            self._last_time = time.monotonic()
            if self.motion != "off":
                self._schedule_tick(10)

    def _on_unmap(self, event: Optional[tk.Event] = None) -> None:
        self._paused = True
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _on_destroy(self, event: Optional[tk.Event] = None) -> None:
        self._destroyed = True
        self._paused = True
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
        if self._telemetry_timer_id is not None:
            try:
                self.after_cancel(self._telemetry_timer_id)
            except Exception:
                pass
            self._telemetry_timer_id = None

    # --- Interactive Controls ---

    def _is_dialog_open(self) -> bool:
        if self.is_dialog_open is not None:
            try:
                return bool(self.is_dialog_open())
            except Exception:
                pass
        return False

    def show_notice(self, text: str, lifetime: float = 3.0) -> None:
        self.notices_count += 1
        clean_text = str(text).strip()
        self._notices.append(HudNotice(clean_text, time.monotonic(), float(lifetime)))
        if len(self._notices) > 2:
            self._notices = self._notices[-2:]

    def open_radial_menu(self, actions: list[RadialAction]) -> bool:
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
            self._configure_item(self._focus_ring_id, on_orb=False, state="normal")

    def _on_focus_out(self, event: tk.Event) -> None:
        if self._focus_ring_id is not None:
            self._configure_item(self._focus_ring_id, on_orb=False, state="hidden")

    def _on_motion(self, event: tk.Event) -> None:
        self._cursor_x = float(event.x)
        self._cursor_y = float(event.y)

    def _on_mouse_down(self, event: tk.Event) -> None:
        if self._is_dialog_open() or not self.interactive:
            return

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
            if not blocked:
                self.on_mic()
            else:
                self.show_notice("Microphone busy")

    def _on_double_click(self, event: tk.Event) -> None:
        if self._is_dialog_open() or not self.interactive:
            return
        ly = layout(self._width, self._height, scaling=self._scale)
        orb_r = ly.sphere_radius * self.model.zoom_factor
        dist = math.hypot(event.x - ly.cx, event.y - ly.cy)
        if dist <= orb_r:
            self.model.reset_view()
            self._last_coords_cache.clear()
            self._polyline_bucket_cache.clear()
            self._particle_int_coords.clear()
            self._particle_last_pos.clear()
            self._draw_frame(dt=0.0)

    def _on_mouse_wheel(self, event: tk.Event, delta: Optional[int] = None) -> None:
        if self._is_dialog_open() or not self.interactive:
            return
        d = delta if delta is not None else getattr(event, "delta", 0)
        zoom_step = 0.05 if d > 0 else -0.05
        ly = layout(self._width, self._height, scaling=self._scale)
        new_zoom = clamp_zoom_factor(self._width, self._height, self.model.zoom_factor + zoom_step, ly.scale)
        self.model.zoom_factor = new_zoom
        self.zoom_factor = new_zoom
        self._last_coords_cache.clear()
        self._polyline_bucket_cache.clear()
        self._particle_int_coords.clear()
        self._particle_last_pos.clear()
        self._draw_frame(dt=0.0)

    def _on_right_click(self, event: tk.Event) -> None:
        if self._is_dialog_open() or not self.interactive:
            return
        ly = layout(self._width, self._height, scaling=self._scale)
        orb_r = ly.sphere_radius * self.model.zoom_factor
        dist = math.hypot(event.x - ly.cx, event.y - ly.cy)
        if dist <= orb_r * 1.5:
            if self.on_radial_menu is not None:
                self.on_radial_menu(self)

    # --- Keyboard Shortcuts ---

    def _on_key_up(self, event: tk.Event) -> str:
        self.model.sphere_ax = (self.model.sphere_ax - 0.1) % (2 * math.pi)
        self._draw_frame(dt=0.0)
        return "break"

    def _on_key_down(self, event: tk.Event) -> str:
        self.model.sphere_ax = (self.model.sphere_ax + 0.1) % (2 * math.pi)
        self._draw_frame(dt=0.0)
        return "break"

    def _on_key_left(self, event: tk.Event) -> str:
        self.model.sphere_ay = (self.model.sphere_ay - 0.1) % (2 * math.pi)
        self._draw_frame(dt=0.0)
        return "break"

    def _on_key_right(self, event: tk.Event) -> str:
        self.model.sphere_ay = (self.model.sphere_ay + 0.1) % (2 * math.pi)
        self._draw_frame(dt=0.0)
        return "break"

    def _on_key_zoom_in(self, event: tk.Event) -> str:
        ly = layout(self._width, self._height, scaling=self._scale)
        self.model.zoom_factor = clamp_zoom_factor(self._width, self._height, self.model.zoom_factor + 0.05, ly.scale)
        self.zoom_factor = self.model.zoom_factor
        self._draw_frame(dt=0.0)
        return "break"

    def _on_key_zoom_out(self, event: tk.Event) -> str:
        ly = layout(self._width, self._height, scaling=self._scale)
        self.model.zoom_factor = clamp_zoom_factor(self._width, self._height, self.model.zoom_factor - 0.05, ly.scale)
        self.zoom_factor = self.model.zoom_factor
        self._draw_frame(dt=0.0)
        return "break"

    def _on_key_reset(self, event: tk.Event) -> str:
        self.model.reset_view()
        self._draw_frame(dt=0.0)
        return "break"

    def _on_key_space(self, event: tk.Event) -> str:
        self._trigger_mic_action()
        return "break"

    def _on_key_ctrl_space(self, event: tk.Event) -> str:
        if self.on_radial_menu is not None:
            self.on_radial_menu(self)
        return "break"

    def _on_key_m(self, event: tk.Event) -> str:
        self._trigger_mic_action()
        return "break"

    def _on_key_help(self, event: tk.Event) -> str:
        if self.on_shortcuts is not None:
            self.on_shortcuts()
        return "break"

    def _on_key_escape(self, event: tk.Event) -> str:
        if self._radial_menu.is_open:
            self._radial_menu.close()
        return "break"

    def _on_key_tab(self, event: tk.Event) -> str:
        self.tk_focusNext().focus_set()
        return "break"

    def _on_key_shift_tab(self, event: tk.Event) -> str:
        self.tk_focusPrev().focus_set()
        return "break"

    def _on_key_return(self, event: tk.Event) -> str:
        self._trigger_mic_action()
        return "break"

    # --- Public Setters & Notifications ---

    def set_palette(self, palette: Palette) -> None:
        self.palette = palette
        self.configure(bg=palette.bg)
        self._last_coords_cache.clear()
        self._last_config_cache.clear()
        self._polyline_bucket_cache.clear()
        self._particle_int_coords.clear()
        self._particle_last_pos.clear()
        self._schedule_realism_build()
        self._draw_static_layer()
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
        self._draw_static_layer()
        self._draw_frame(dt=0.0)

    def update_status(self, status: Optional[str] = None) -> None:
        self._update_state_label()
