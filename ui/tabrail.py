"""Vertical Tab Rail component for ORBIT.

Provides a 36px wide rail with vector-drawn icons, active accent bar,
hover tooltips, unread count badges, keyboard navigation (Alt+1..9, arrows),
and vertical mousewheel scrolling.
"""

from dataclasses import dataclass
import tkinter as tk
from typing import Callable, Optional

from ui.theme import get_palette, Palette


@dataclass
class TabDef:
    tab_id: str
    label: str
    icon_type: str
    tooltip: str
    badge_count: int = 0


TAB_DEFINITIONS = [
    ("today", "Today", "today", "Today (Alt+1)"),
    ("activity", "Activity", "activity", "Activity (Alt+2)"),
    ("context", "Context", "context", "Context (Alt+3)"),
    ("memories", "Memories", "memories", "Memories (Alt+4)"),
    ("documents", "Documents", "documents", "Documents (Alt+5)"),
    ("reminders", "Reminders", "reminders", "Reminders (Alt+6)"),
    ("routines", "Routines", "routines", "Routines (Alt+7)"),
    ("contacts", "Contacts", "contacts", "Contacts (Alt+8)"),
    ("files", "Files", "files", "Files (Alt+9)"),
    ("system", "System", "system", "System"),
    ("insights", "Insights", "insights", "Insights"),
    ("verify", "Verify", "verify", "Verify"),
    ("last_run", "Last run", "last_run", "Last run"),
]


class TabRail(tk.Frame):
    """36px-wide vertical icon rail replacing the ttk.Notebook tab strip."""

    ITEM_HEIGHT = 38
    WIDTH = 36

    def __init__(
        self,
        master: tk.Widget,
        palette: Optional[Palette] = None,
        on_select: Optional[Callable[[str], None]] = None,
        **kwargs,
    ) -> None:
        self.palette = palette or get_palette()
        kwargs.setdefault("bg", self.palette.panel)
        kwargs.setdefault("width", self.WIDTH)
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(master, **kwargs)

        self.on_select = on_select
        self._tabs: list[TabDef] = []
        self._tabs_by_id: dict[str, TabDef] = {}
        self._active_id: Optional[str] = None
        self._focused_idx: int = 0
        self._hovered_id: Optional[str] = None
        self._scroll_offset_y: int = 0
        self._tooltip_win: Optional[tk.Toplevel] = None
        self._tooltip_after: Optional[str] = None

        # Build canvas
        self.canvas = tk.Canvas(
            self,
            width=self.WIDTH,
            bg=self.palette.panel,
            bd=0,
            highlightthickness=1,
            highlightcolor=self.palette.accent,
            highlightbackground=self.palette.panel,
            takefocus=True,
        )
        self.canvas.pack(fill="both", expand=True)

        # Bind events
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Up>", self._on_key_up)
        self.canvas.bind("<Down>", self._on_key_down)
        self.canvas.bind("<Return>", self._on_key_enter)
        self.canvas.bind("<space>", self._on_key_enter)
        self.canvas.bind("<FocusIn>", self._on_focus_in)
        self.canvas.bind("<FocusOut>", self._on_focus_out)

        # Populate standard tabs
        for tab_id, label, icon_type, tooltip in TAB_DEFINITIONS:
            self.add_tab(tab_id, label, icon_type, tooltip)

        if self._tabs:
            self._active_id = self._tabs[0].tab_id

    def add_tab(self, tab_id: str, label: str, icon_type: str = "", tooltip: str = "") -> None:
        """Add or update a tab in the rail."""
        if tab_id in self._tabs_by_id:
            tab = self._tabs_by_id[tab_id]
            tab.label = label
            tab.icon_type = icon_type or tab_id
            tab.tooltip = tooltip or label
        else:
            tab = TabDef(
                tab_id=tab_id,
                label=label,
                icon_type=icon_type or tab_id,
                tooltip=tooltip or label,
            )
            self._tabs.append(tab)
            self._tabs_by_id[tab_id] = tab

        if not self._active_id:
            self._active_id = tab_id
        self._redraw()

    def set_badge(self, tab_id: str, count: int) -> None:
        """Update unread count badge for a tab."""
        tab = self._tabs_by_id.get(tab_id)
        if tab:
            tab.badge_count = max(0, int(count))
            self._redraw()

    def get_badge(self, tab_id: str) -> int:
        """Return current badge count for a tab."""
        tab = self._tabs_by_id.get(tab_id)
        return tab.badge_count if tab else 0

    def select_tab(self, tab_id: str, trigger_callback: bool = True) -> None:
        """Select a tab by tab_id or index."""
        target_id = tab_id
        if isinstance(tab_id, int):
            if 0 <= tab_id < len(self._tabs):
                target_id = self._tabs[tab_id].tab_id
            else:
                return

        if target_id not in self._tabs_by_id:
            return

        self._active_id = target_id
        for i, t in enumerate(self._tabs):
            if t.tab_id == target_id:
                self._focused_idx = i
                break

        self._ensure_visible(self._focused_idx)
        self._redraw()

        if trigger_callback and self.on_select:
            self.on_select(target_id)

    def get_active_tab(self) -> Optional[str]:
        """Return the currently selected tab id."""
        return self._active_id

    def get_tabs(self) -> list[str]:
        """Return list of tab IDs."""
        return [t.tab_id for t in self._tabs]

    # --- Internal rendering ---

    def _ensure_visible(self, idx: int) -> None:
        """Scroll if needed to make tab at idx visible."""
        top_y = idx * self.ITEM_HEIGHT + self._scroll_offset_y
        bot_y = top_y + self.ITEM_HEIGHT
        canv_h = self.canvas.winfo_height() or 400
        if top_y < 0:
            self._scroll_offset_y -= top_y
        elif bot_y > canv_h:
            self._scroll_offset_y -= (bot_y - canv_h)

    def _on_configure(self, event=None) -> None:
        self._redraw()

    def _redraw(self) -> None:
        self.canvas.delete("all")
        accent = self.palette.accent
        panel_bg = self.palette.panel
        dim_fg = self.palette.text_dim
        text_fg = self.palette.text

        # Draw rail right border
        self.canvas.create_line(self.WIDTH - 1, 0, self.WIDTH - 1, 3000, fill="#202634")

        y_offset = self._scroll_offset_y
        for idx, tab in enumerate(self._tabs):
            top_y = idx * self.ITEM_HEIGHT + y_offset
            bot_y = top_y + self.ITEM_HEIGHT
            center_y = (top_y + bot_y) // 2
            center_x = self.WIDTH // 2

            is_active = (tab.tab_id == self._active_id)
            is_hovered = (tab.tab_id == self._hovered_id)

            # Hover / Active background
            if is_active:
                self.canvas.create_rectangle(
                    3, top_y + 2, self.WIDTH - 2, bot_y - 2,
                    fill="#151d2d", outline="", tags=f"tab_{tab.tab_id}"
                )
                # Active left accent bar
                self.canvas.create_rectangle(
                    0, top_y + 4, 3, bot_y - 4,
                    fill=accent, outline=""
                )
            elif is_hovered:
                self.canvas.create_rectangle(
                    3, top_y + 2, self.WIDTH - 2, bot_y - 2,
                    fill="#101826", outline="", tags=f"tab_{tab.tab_id}"
                )

            # Icon color
            icon_color = accent if is_active else (text_fg if is_hovered else dim_fg)

            # Draw vector glyph
            self._draw_icon(center_x, center_y, tab.icon_type, icon_color, is_active)

            # Badge if > 0
            if tab.badge_count > 0:
                badge_text = str(tab.badge_count) if tab.badge_count <= 9 else "9+"
                bx = self.WIDTH - 8
                by = top_y + 8
                self.canvas.create_oval(
                    bx - 6, by - 6, bx + 6, by + 6,
                    fill="#e05252", outline=panel_bg, width=1
                )
                self.canvas.create_text(
                    bx, by,
                    text=badge_text,
                    fill="#ffffff",
                    font=("Segoe UI", 7, "bold")
                )

        # Focus ring on canvas when focused
        if self.canvas.focus_get() == self.canvas:
            f_top = self._focused_idx * self.ITEM_HEIGHT + y_offset
            self.canvas.create_rectangle(
                3, f_top + 2, self.WIDTH - 2, f_top + self.ITEM_HEIGHT - 2,
                outline=accent, width=1, dash=(2, 2)
            )

    def _draw_icon(self, cx: int, cy: int, icon_type: str, color: str, is_active: bool) -> None:
        """Draw vector icon glyph inside 22x22 area centered at (cx, cy)."""
        r = 9
        if icon_type == "today":
            # Clock / sun: circle with hour and minute hands
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=1)
            self.canvas.create_line(cx, cy, cx, cy - 5, fill=color, width=1)
            self.canvas.create_line(cx, cy, cx + 4, cy, fill=color, width=1)
        elif icon_type == "activity":
            # Pulse waveform
            pts = [
                cx - 8, cy,
                cx - 4, cy,
                cx - 2, cy - 6,
                cx + 1, cy + 6,
                cx + 4, cy - 2,
                cx + 8, cy,
            ]
            self.canvas.create_line(pts, fill=color, width=1, smooth=False)
        elif icon_type == "context":
            # Stacked layers / list
            self.canvas.create_line(cx - 7, cy - 5, cx + 7, cy - 5, fill=color, width=1)
            self.canvas.create_line(cx - 7, cy, cx + 7, cy, fill=color, width=1)
            self.canvas.create_line(cx - 7, cy + 5, cx + 7, cy + 5, fill=color, width=1)
        elif icon_type == "memories":
            # 4-point star/spark
            pts = [
                cx, cy - 8,
                cx + 2, cy - 2,
                cx + 8, cy,
                cx + 2, cy + 2,
                cx, cy + 8,
                cx - 2, cy + 2,
                cx - 8, cy,
                cx - 2, cy - 2,
            ]
            self.canvas.create_polygon(pts, fill=color if is_active else "", outline=color, width=1)
        elif icon_type == "documents":
            # Document page with folded corner
            self.canvas.create_rectangle(cx - 6, cy - 8, cx + 6, cy + 8, outline=color, width=1)
            self.canvas.create_line(cx - 3, cy - 3, cx + 3, cy - 3, fill=color, width=1)
            self.canvas.create_line(cx - 3, cy + 1, cx + 3, cy + 1, fill=color, width=1)
            self.canvas.create_line(cx - 3, cy + 5, cx + 1, cy + 5, fill=color, width=1)
        elif icon_type == "reminders":
            # Bell shape
            self.canvas.create_arc(cx - 6, cy - 6, cx + 6, cy + 6, start=0, extent=180, outline=color, width=1)
            self.canvas.create_line(cx - 7, cy + 3, cx + 7, cy + 3, fill=color, width=1)
            self.canvas.create_oval(cx - 1, cy + 4, cx + 1, cy + 6, fill=color, outline="")
        elif icon_type == "routines":
            # Play in circle / gear
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=1)
            pts = [cx - 2, cy - 4, cx + 5, cy, cx - 2, cy + 4]
            self.canvas.create_polygon(pts, fill=color, outline="")
        elif icon_type == "contacts":
            # User head & shoulders
            self.canvas.create_oval(cx - 3, cy - 7, cx + 3, cy - 1, outline=color, width=1)
            self.canvas.create_arc(cx - 7, cy - 1, cx + 7, cy + 9, start=0, extent=180, outline=color, width=1)
        elif icon_type == "files":
            # Folder outline
            self.canvas.create_line(cx - 7, cy - 3, cx - 7, cy + 6, cx + 7, cy + 6, cx + 7, cy - 3, fill=color, width=1)
            self.canvas.create_line(cx - 7, cy - 3, cx - 3, cy - 3, cx - 1, cy - 6, cx + 4, cy - 6, cx + 5, cy - 3, cx + 7, cy - 3, fill=color, width=1)
        elif icon_type == "system":
            # CPU chip
            self.canvas.create_rectangle(cx - 5, cy - 5, cx + 5, cy + 5, outline=color, width=1)
            for d in (-3, 3):
                self.canvas.create_line(cx + d, cy - 8, cx + d, cy - 5, fill=color, width=1)
                self.canvas.create_line(cx + d, cy + 5, cx + d, cy + 8, fill=color, width=1)
                self.canvas.create_line(cx - 8, cy + d, cx - 5, cy + d, fill=color, width=1)
                self.canvas.create_line(cx + 5, cy + d, cx + 8, cy + d, fill=color, width=1)
        elif icon_type == "insights":
            # 3 vertical bars
            self.canvas.create_rectangle(cx - 7, cy + 2, cx - 3, cy + 7, fill=color, outline="")
            self.canvas.create_rectangle(cx - 2, cy - 3, cx + 2, cy + 7, fill=color, outline="")
            self.canvas.create_rectangle(cx + 3, cy - 7, cx + 7, cy + 7, fill=color, outline="")
        elif icon_type == "verify":
            # Shield with checkmark
            pts = [
                cx, cy - 7,
                cx + 6, cy - 4,
                cx + 5, cy + 3,
                cx, cy + 8,
                cx - 5, cy + 3,
                cx - 6, cy - 4,
            ]
            self.canvas.create_polygon(pts, outline=color, fill="", width=1)
            self.canvas.create_line(cx - 2, cy, cx, cy + 3, cx + 3, cy - 3, fill=color, width=1)
        elif icon_type == "last_run":
            # Terminal prompt '>_'
            self.canvas.create_line(cx - 6, cy - 4, cx - 1, cy, cx - 6, cy + 4, fill=color, width=1)
            self.canvas.create_line(cx + 1, cy + 4, cx + 6, cy + 4, fill=color, width=1)
        else:
            # Fallback circle
            self.canvas.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, outline=color, width=1)

    # --- Interaction Handlers ---

    def _get_tab_at_y(self, y: int) -> Optional[TabDef]:
        idx = (y - self._scroll_offset_y) // self.ITEM_HEIGHT
        if 0 <= idx < len(self._tabs):
            return self._tabs[idx]
        return None

    def _on_click(self, event) -> None:
        self.canvas.focus_set()
        tab = self._get_tab_at_y(event.y)
        if tab:
            self.select_tab(tab.tab_id)

    def _on_motion(self, event) -> None:
        tab = self._get_tab_at_y(event.y)
        new_hover = tab.tab_id if tab else None
        if new_hover != self._hovered_id:
            self._hovered_id = new_hover
            self._redraw()
            self._hide_tooltip()
            if tab:
                self._schedule_tooltip(event.x_root, event.y_root, tab.tooltip)

    def _on_leave(self, event) -> None:
        if self._hovered_id is not None:
            self._hovered_id = None
            self._redraw()
        self._hide_tooltip()

    def _on_mousewheel(self, event) -> None:
        delta = event.delta
        if not delta:
            return
        step = (delta // 120) * self.ITEM_HEIGHT
        total_h = len(self._tabs) * self.ITEM_HEIGHT
        canv_h = self.canvas.winfo_height() or 400
        min_offset = min(0, canv_h - total_h)

        new_offset = max(min_offset, min(0, self._scroll_offset_y + step))
        if new_offset != self._scroll_offset_y:
            self._scroll_offset_y = new_offset
            self._redraw()

    def _on_key_up(self, event) -> str:
        if self._focused_idx > 0:
            self._focused_idx -= 1
            self._ensure_visible(self._focused_idx)
            self._redraw()
        return "break"

    def _on_key_down(self, event) -> str:
        if self._focused_idx < len(self._tabs) - 1:
            self._focused_idx += 1
            self._ensure_visible(self._focused_idx)
            self._redraw()
        return "break"

    def _on_key_enter(self, event) -> str:
        if 0 <= self._focused_idx < len(self._tabs):
            self.select_tab(self._tabs[self._focused_idx].tab_id)
        return "break"

    def _on_focus_in(self, event) -> None:
        self._redraw()

    def _on_focus_out(self, event) -> None:
        self._redraw()

    # --- Tooltips ---

    def _schedule_tooltip(self, root_x: int, root_y: int, text: str) -> None:
        self._hide_tooltip()
        self._tooltip_after = self.after(300, lambda: self._show_tooltip(root_x, root_y, text))

    def _show_tooltip(self, root_x: int, root_y: int, text: str) -> None:
        self._hide_tooltip()
        tw = tk.Toplevel(self)
        tw.wm_overrideredirect(True)
        tw.geometry(f"+{root_x + 15}+{root_y - 10}")

        lbl = tk.Label(
            tw,
            text=text,
            background="#1e2330",
            foreground="#e0e8f5",
            font=("Segoe UI", 8),
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=3,
        )
        lbl.pack()
        self._tooltip_win = tw

    def _hide_tooltip(self) -> None:
        if self._tooltip_after:
            try:
                self.after_cancel(self._tooltip_after)
            except Exception:
                pass
            self._tooltip_after = None
        if self._tooltip_win:
            try:
                self._tooltip_win.destroy()
            except Exception:
                pass
            self._tooltip_win = None
