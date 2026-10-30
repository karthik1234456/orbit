"""Vector-rendered interactive bottom dock for ORBIT desktop assistant.

Features:
- Single focusable Tkinter Canvas (takefocus=True)
- 5 pure vector-drawn round icons (Activity, Routines, Microphone, Settings, Doctor)
- Center microphone icon with dynamic audio level ring and blocked/disabled states
- Hover glow, tooltips, and distinct focus ring
- Keyboard navigation: Left/Right arrows, Space/Enter to activate, Ctrl+1..5 direct triggers
- Invokes identical existing UI callbacks without new permissions or action paths
"""

from __future__ import annotations

import math
import tkinter as tk
from typing import Any, Callable, Optional

from ui.theme import Palette, get_palette


class DockCanvas(tk.Canvas):
    """Focusable, keyboard-accessible dock of vector icons for primary assistant actions."""

    ICON_COUNT = 5

    def __init__(
        self,
        parent: tk.Widget,
        palette: Optional[Palette] = None,
        on_activity: Optional[Callable[[], None]] = None,
        on_routines: Optional[Callable[[], None]] = None,
        on_mic: Optional[Callable[[], None]] = None,
        on_settings: Optional[Callable[[], None]] = None,
        on_doctor: Optional[Callable[[], None]] = None,
        get_voice_state: Optional[Callable[[], tuple[str, bool, str]]] = None,
        **kwargs: Any,
    ) -> None:
        self.palette = palette or get_palette("cyan")
        self.on_activity = on_activity
        self.on_routines = on_routines
        self.on_mic = on_mic
        self.on_settings = on_settings
        self.on_doctor = on_doctor
        self.get_voice_state = get_voice_state

        kwargs.setdefault("bg", self.palette.bg)
        kwargs.setdefault("height", 110)
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("takefocus", True)
        super().__init__(parent, **kwargs)

        self._focused_idx: int = 2  # Default focus on Center Mic
        self._hover_idx: Optional[int] = None
        self._has_keyboard_focus: bool = False
        self._mic_level: float = 0.0

        # Tooltip state
        self._tip_window: Optional[tk.Toplevel] = None

        # Bind events
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Left>", self._on_key_left)
        self.bind("<Right>", self._on_key_right)
        self.bind("<Return>", self._on_key_activate)
        self.bind("<space>", self._on_key_activate)
        self.bind("<Motion>", self._on_mouse_motion)
        self.bind("<Leave>", self._on_mouse_leave)
        self.bind("<Button-1>", self._on_mouse_click)

        # Ctrl+1..5 global bindings on parent/root
        for i in range(1, 6):
            self.bind_all(f"<Control-Key-{i}>", lambda e, idx=i - 1: self._trigger_index(idx))

        self.bind("<Configure>", lambda e: self.redraw())
        self.redraw()

    # --- Tooltip Helpers ---

    def _get_tooltip_text(self, idx: int) -> str:
        if idx == 0:
            return "Activity log (Ctrl+1)"
        if idx == 1:
            return "Routines automation (Ctrl+2)"
        if idx == 2:
            if self.get_voice_state:
                st, blocked, reason = self.get_voice_state()
                if blocked and reason:
                    return f"Microphone: {reason} (Ctrl+3 / Ctrl+M)"
                if st == "listening":
                    return "Click to stop recording (Ctrl+3 / Ctrl+M)"
            return "Microphone click-to-talk (Ctrl+3 / Ctrl+M)"
        if idx == 3:
            return "Settings panel (Ctrl+4)"
        if idx == 4:
            return "System doctor (Ctrl+5)"
        return ""

    def _show_tooltip(self, text: str, x: int, y: int) -> None:
        self._hide_tooltip()
        if not text:
            return
        try:
            tw = tk.Toplevel(self)
            tw.wm_overrideredirect(True)
            rx = self.winfo_rootx() + x + 10
            ry = self.winfo_rooty() + y - 28
            tw.wm_geometry(f"+{rx}+{ry}")
            lbl = tk.Label(
                tw,
                text=text,
                bg=self.palette.panel,
                fg=self.palette.text,
                relief="solid",
                bd=1,
                font=("Segoe UI", 8),
                padx=5,
                pady=2,
            )
            lbl.pack()
            self._tip_window = tw
        except Exception:
            self._tip_window = None

    def _hide_tooltip(self) -> None:
        if self._tip_window:
            try:
                self._tip_window.destroy()
            except Exception:
                pass
            self._tip_window = None

    # --- Input Handling ---

    def _on_focus_in(self, event: tk.Event) -> None:
        self._has_keyboard_focus = True
        self.redraw()

    def _on_focus_out(self, event: tk.Event) -> None:
        self._has_keyboard_focus = False
        self._hide_tooltip()
        self.redraw()

    def _on_key_left(self, event: tk.Event) -> str:
        self._focused_idx = (self._focused_idx - 1) % self.ICON_COUNT
        self.redraw()
        return "break"

    def _on_key_right(self, event: tk.Event) -> str:
        self._focused_idx = (self._focused_idx + 1) % self.ICON_COUNT
        self.redraw()
        return "break"

    def _on_key_activate(self, event: tk.Event) -> str:
        self._trigger_index(self._focused_idx)
        return "break"

    def _get_icon_centers(self) -> list[tuple[float, float, float]]:
        """Compute (cx, cy, radius) for each of the 5 dock icons."""
        w = max(200, self.winfo_width())
        h = max(50, self.winfo_height())
        cy = h / 2.0
        cx = w / 2.0

        try:
            top = self.winfo_toplevel()
            parent_w = float(top.winfo_width())
            parent_h = float(top.winfo_height())
        except Exception:
            parent_w = 0.0
            parent_h = 0.0

        if parent_w >= 600.0 and parent_h >= 400.0:
            from ui.hud_core import layout

            try:
                tk_scale = float(self.tk.call("tk", "scaling"))
                if tk_scale < 0.5 or tk_scale > 4.0:
                    tk_scale = 1.0
            except Exception:
                tk_scale = 1.0

            ly = layout(parent_w, parent_h, scaling=tk_scale)
            centers = []
            for i, x_val in enumerate(ly.dock_xs):
                r = ly.dock_mic_radius if i == 2 else ly.dock_side_radius
                off = x_val - ly.cx
                centers.append((cx + off, cy, r))
            return centers

        spacing = 58.0
        centers = []
        for i in range(self.ICON_COUNT):
            x = cx + (i - 2) * spacing
            r = 22.0 if i == 2 else 17.0
            centers.append((x, cy, r))
        return centers

    def _on_mouse_motion(self, event: tk.Event) -> None:
        centers = self._get_icon_centers()
        found: Optional[int] = None
        for i, (cx, cy, r) in enumerate(centers):
            dist = math.hypot(event.x - cx, event.y - cy)
            if dist <= r + 4:
                found = i
                break

        if found != self._hover_idx:
            self._hover_idx = found
            self.redraw()
            if found is not None:
                self._show_tooltip(self._get_tooltip_text(found), event.x, event.y)
            else:
                self._hide_tooltip()

    def _on_mouse_leave(self, event: tk.Event) -> None:
        self._hover_idx = None
        self._hide_tooltip()
        self.redraw()

    def _on_mouse_click(self, event: tk.Event) -> None:
        self.focus_set()
        centers = self._get_icon_centers()
        for i, (cx, cy, r) in enumerate(centers):
            dist = math.hypot(event.x - cx, event.y - cy)
            if dist <= r + 4:
                self._focused_idx = i
                self._trigger_index(i)
                break

    def _trigger_index(self, idx: int) -> None:
        """Dispatch action for the given icon index calling the existing handlers."""
        if idx == 0 and callable(self.on_activity):
            self.on_activity()
        elif idx == 1 and callable(self.on_routines):
            self.on_routines()
        elif idx == 2 and callable(self.on_mic):
            self.on_mic()
        elif idx == 3 and callable(self.on_settings):
            self.on_settings()
        elif idx == 4 and callable(self.on_doctor):
            self.on_doctor()

    def set_mic_level(self, level: float) -> None:
        """Update live audio level for microphone pulsing ring."""
        self._mic_level = max(0.0, min(1.0, float(level)))
        self.redraw()

    def set_palette(self, palette: Palette) -> None:
        """Update palette and redraw dock."""
        self.palette = palette
        self.configure(bg=palette.bg)
        self.redraw()

    # --- Drawing Logic ---

    def redraw(self) -> None:
        """Render all 5 vector icons with hover and focus effects."""
        self.delete("all")
        centers = self._get_icon_centers()

        # Check voice state
        v_state = "idle"
        v_blocked = False
        if self.get_voice_state:
            v_state, v_blocked, _ = self.get_voice_state()

        for i, (cx, cy, r) in enumerate(centers):
            is_hover = (i == self._hover_idx)
            is_focus = (i == self._focused_idx and self._has_keyboard_focus)

            # Background circle
            if self.palette.name == "gold":
                bg_col = "#262015" if i == 2 else self.palette.panel
                edge_col = self.palette.accent if is_hover else "#C3A668"
            else:
                bg_col = self.palette.panel_edge if is_hover else self.palette.panel
                edge_col = self.palette.accent if is_hover else self.palette.panel_edge

            stroke_w = 2 if is_hover else (1.5 if self.palette.name == "gold" else 1)
            self.create_oval(
                cx - r, cy - r, cx + r, cy + r,
                fill=bg_col,
                outline=edge_col,
                width=stroke_w,
            )

            # Focus ring
            if is_focus:
                fr = r + 4
                self.create_oval(
                    cx - fr, cy - fr, cx + fr, cy + fr,
                    outline=self.palette.accent,
                    width=2,
                )

            # Draw vector iconography
            color = self.palette.text if not is_hover else self.palette.accent
            if i == 2 and v_blocked:
                color = self.palette.text_dim

            if i == 0:
                self._draw_clock_icon(cx, cy, r * 0.55, color)
            elif i == 1:
                self._draw_routines_icon(cx, cy, r * 0.55, color)
            elif i == 2:
                self._draw_mic_icon(cx, cy, r * 0.50, color, v_state)
            elif i == 3:
                self._draw_gear_icon(cx, cy, r * 0.52, color)
            elif i == 4:
                self._draw_doctor_icon(cx, cy, r * 0.55, color)

    def _draw_clock_icon(self, cx: float, cy: float, sz: float, color: str) -> None:
        # Outer clock dial
        self.create_oval(cx - sz, cy - sz, cx + sz, cy + sz, outline=color, width=1.5)
        # Center dot
        self.create_oval(cx - 1.5, cy - 1.5, cx + 1.5, cy + 1.5, fill=color, outline="")
        # Hour & Minute hands (pointing to 2 o'clock)
        self.create_line(cx, cy, cx, cy - sz * 0.65, fill=color, width=1.5)
        self.create_line(cx, cy, cx + sz * 0.45, cy + sz * 0.1, fill=color, width=1.5)

    def _draw_routines_icon(self, cx: float, cy: float, sz: float, color: str) -> None:
        # 3 Linked Nodes (triangle formation)
        n1 = (cx, cy - sz * 0.65)
        n2 = (cx - sz * 0.70, cy + sz * 0.50)
        n3 = (cx + sz * 0.70, cy + sz * 0.50)

        # Linking branches
        self.create_line(n1[0], n1[1], n2[0], n2[1], fill=color, width=1.2)
        self.create_line(n1[0], n1[1], n3[0], n3[1], fill=color, width=1.2)
        self.create_line(n2[0], n2[1], n3[0], n3[1], fill=color, width=1.2)

        # Node circles
        nr = 3.0
        for nx, ny in (n1, n2, n3):
            self.create_oval(nx - nr, ny - nr, nx + nr, ny + nr, fill=color, outline="")

    def _draw_mic_icon(self, cx: float, cy: float, sz: float, color: str, v_state: str) -> None:
        # Capsule body
        cw = sz * 0.42
        ch = sz * 0.85
        self.create_oval(cx - cw, cy - ch, cx + cw, cy - ch + 2 * cw, fill=color, outline="")
        self.create_rectangle(cx - cw, cy - ch + cw, cx + cw, cy + ch - cw, fill=color, outline="")
        self.create_oval(cx - cw, cy + ch - 2 * cw, cx + cw, cy + ch, fill=color, outline="")

        # Base arc cradle
        rw = sz * 0.75
        self.create_arc(
            cx - rw, cy - rw * 0.6, cx + rw, cy + rw * 0.9,
            start=180, extent=180,
            style="arc",
            outline=color,
            width=1.5,
        )
        # Stand stem & foot
        self.create_line(cx, cy + rw * 0.9, cx, cy + rw * 1.35, fill=color, width=1.5)
        self.create_line(cx - sz * 0.4, cy + rw * 1.35, cx + sz * 0.4, cy + rw * 1.35, fill=color, width=1.5)

        # Audio level indicator ring when listening
        if v_state == "listening":
            level_r = 22.0 + self._mic_level * 5.0
            self.create_oval(
                cx - level_r, cy - level_r, cx + level_r, cy + level_r,
                outline=self.palette.ok,
                width=1.5,
            )

    def _draw_gear_icon(self, cx: float, cy: float, sz: float, color: str) -> None:
        # Gear body
        self.create_oval(cx - sz * 0.7, cy - sz * 0.7, cx + sz * 0.7, cy + sz * 0.7, outline=color, width=1.5)
        self.create_oval(cx - sz * 0.25, cy - sz * 0.25, cx + sz * 0.25, cy + sz * 0.25, fill=color, outline="")
        # 6 Gear teeth
        for k in range(6):
            ang = (math.pi / 3.0) * k
            tx1 = cx + math.cos(ang) * (sz * 0.65)
            ty1 = cy + math.sin(ang) * (sz * 0.65)
            tx2 = cx + math.cos(ang) * (sz * 1.0)
            ty2 = cy + math.sin(ang) * (sz * 1.0)
            self.create_line(tx1, ty1, tx2, ty2, fill=color, width=2.5)

    def _draw_doctor_icon(self, cx: float, cy: float, sz: float, color: str) -> None:
        # Heartbeat pulse line (EKG)
        points = [
            (cx - sz, cy),
            (cx - sz * 0.5, cy),
            (cx - sz * 0.3, cy - sz * 0.55),
            (cx - sz * 0.1, cy + sz * 0.65),
            (cx + sz * 0.15, cy - sz * 0.75),
            (cx + sz * 0.35, cy + sz * 0.35),
            (cx + sz * 0.55, cy),
            (cx + sz, cy),
        ]
        flat_pts = [coord for pt in points for coord in pt]
        self.create_line(*flat_pts, fill=color, width=1.6)

