"""Radial quick-action ring menu for the ORBIT HUD.

Opens a ring of six equal wedges / items positioned radially around the telemetry orb:
1. New conversation
2. Speak replies on or off
3. Stop speaking and mic
4. Doctor
5. Settings
6. Cycle theme

Supports both mouse hovering/clicking and keyboard navigation (arrow keys/Tab to move,
Enter to activate, Esc to close, outside click to close).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable, Optional
import tkinter as tk

from ui.theme import Palette, get_palette


@dataclass
class RadialAction:
    """Action item displayed within the radial menu."""

    label: str
    handler: Callable[[], None]
    icon: str = ""


class RadialMenu:
    """Radial menu controller rendered on a Tkinter canvas."""

    def __init__(
        self,
        canvas: tk.Canvas,
        palette: Optional[Palette] = None,
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        self.canvas = canvas
        self.palette = palette or get_palette("gold")
        self.on_close = on_close
        self.actions: list[RadialAction] = []
        self.selected_index: int = 0
        self.is_open: bool = False
        self.cx: float = 0.0
        self.cy: float = 0.0
        self.radius: float = 140.0
        self.item_radius: float = 26.0

        # Canvas item tracking
        self._item_ids: list[int] = []

    def open(
        self,
        cx: float,
        cy: float,
        radius: float,
        actions: list[RadialAction],
        initial_selection: int = 0,
    ) -> bool:
        """Open the radial menu centered at (cx, cy) with the specified actions."""
        if not actions:
            return False

        self.cx = float(cx)
        self.cy = float(cy)
        self.radius = max(80.0, float(radius))
        self.actions = list(actions)
        self.selected_index = initial_selection % len(self.actions)
        self.is_open = True
        self.redraw()
        return True

    def close(self) -> None:
        """Close and clear the radial menu from the canvas."""
        if not self.is_open:
            return
        self.is_open = False
        self._clear_items()
        if self.on_close is not None:
            try:
                self.on_close()
            except Exception:
                pass

    def _clear_items(self) -> None:
        for item_id in self._item_ids:
            try:
                self.canvas.delete(item_id)
            except Exception:
                pass
        self._item_ids.clear()

    def get_item_positions(self) -> list[tuple[float, float]]:
        """Calculate (x, y) center coordinates for each of the radial actions."""
        n = len(self.actions)
        if n == 0:
            return []
        positions: list[tuple[float, float]] = []
        # Start from top (-90 degrees) and distribute clockwise
        step = (2 * math.pi) / n
        start_angle = -math.pi / 2.0
        for i in range(n):
            angle = start_angle + i * step
            x = self.cx + self.radius * math.cos(angle)
            y = self.cy + self.radius * math.sin(angle)
            positions.append((x, y))
        return positions

    def redraw(self) -> None:
        """Redraw all radial menu rings, wedge buttons, and labels."""
        self._clear_items()
        if not self.is_open or not self.actions:
            return

        pal = self.palette
        positions = self.get_item_positions()

        # 1. Subtle connecting ring
        ring_id = self.canvas.create_oval(
            self.cx - self.radius,
            self.cy - self.radius,
            self.cx + self.radius,
            self.cy + self.radius,
            outline=pal.panel_edge,
            width=1,
            dash=(3, 3),
        )
        self._item_ids.append(ring_id)

        # 2. Draw each action badge
        for i, (ax, ay) in enumerate(positions):
            action = self.actions[i]
            is_sel = (i == self.selected_index)
            r = self.item_radius * (1.15 if is_sel else 1.0)

            # Outer glow / focus ring for selected
            if is_sel:
                glow_id = self.canvas.create_oval(
                    ax - (r + 4),
                    ay - (r + 4),
                    ax + (r + 4),
                    ay + (r + 4),
                    outline=pal.accent,
                    width=2,
                )
                self._item_ids.append(glow_id)

            bg_col = pal.panel if not is_sel else pal.accent
            fg_col = pal.accent if not is_sel else pal.bg
            border_col = pal.accent if is_sel else pal.panel_edge

            badge_id = self.canvas.create_oval(
                ax - r,
                ay - r,
                ax + r,
                ay + r,
                fill=bg_col,
                outline=border_col,
                width=1,
            )
            self._item_ids.append(badge_id)

            # Label / icon text
            # Calculate short label or numeric index
            display_text = action.icon or f"{i + 1}"
            text_id = self.canvas.create_text(
                ax,
                ay,
                text=display_text,
                fill=fg_col,
                font=("Segoe UI", 10, "bold"),
            )
            self._item_ids.append(text_id)

            # Description pill text below/above badge
            lbl_y = ay + (r + 14) if ay >= self.cy else ay - (r + 14)
            lbl_id = self.canvas.create_text(
                ax,
                lbl_y,
                text=action.label,
                fill=pal.accent if is_sel else pal.text_dim,
                font=("Segoe UI", 8, "bold" if is_sel else "normal"),
            )
            self._item_ids.append(lbl_id)

    def select_next(self) -> None:
        """Select next radial action in wrap-around order."""
        if not self.actions:
            return
        self.selected_index = (self.selected_index + 1) % len(self.actions)
        self.redraw()

    def select_prev(self) -> None:
        """Select previous radial action in wrap-around order."""
        if not self.actions:
            return
        self.selected_index = (self.selected_index - 1) % len(self.actions)
        self.redraw()

    def execute_selected(self) -> bool:
        """Trigger the currently selected action's handler and close the menu."""
        if not self.is_open or not self.actions:
            return False
        action = self.actions[self.selected_index]
        self.close()
        action.handler()
        return True

    def handle_mouse_motion(self, x: float, y: float) -> bool:
        """Check if cursor moved over an action badge and update selection."""
        if not self.is_open or not self.actions:
            return False
        positions = self.get_item_positions()
        hit = -1
        hit_dist_sq = (self.item_radius * 1.5) ** 2
        for i, (ax, ay) in enumerate(positions):
            dist_sq = (x - ax) ** 2 + (y - ay) ** 2
            if dist_sq <= hit_dist_sq:
                hit = i
                break

        if hit != -1 and hit != self.selected_index:
            self.selected_index = hit
            self.redraw()
            return True
        return False

    def handle_mouse_click(self, x: float, y: float) -> bool:
        """Check if cursor clicked on an action badge or outside."""
        if not self.is_open:
            return False
        positions = self.get_item_positions()
        hit = -1
        hit_dist_sq = (self.item_radius * 1.5) ** 2
        for i, (ax, ay) in enumerate(positions):
            dist_sq = (x - ax) ** 2 + (y - ay) ** 2
            if dist_sq <= hit_dist_sq:
                hit = i
                break

        if hit != -1:
            self.selected_index = hit
            return self.execute_selected()
        else:
            # Click was outside any badge -> close radial menu
            self.close()
            return False

    def handle_key(self, key_name: str) -> bool:
        """Handle keyboard navigation keys (arrows, Tab, Enter, Escape)."""
        if not self.is_open:
            return False
        k = key_name.lower()
        if k in ("right", "down", "tab"):
            self.select_next()
            return True
        elif k in ("left", "up"):
            self.select_prev()
            return True
        elif k in ("return", "kp_enter", "space"):
            return self.execute_selected()
        elif k in ("escape",):
            self.close()
            return True
        return False
