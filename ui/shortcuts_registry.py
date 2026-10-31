"""Single source-of-truth keyboard shortcuts registry for ORBIT.

Maintains all key bindings across UI components and surfaces an overlay dialog
listing every available keyboard shortcut grouped by functional area.
"""

from __future__ import annotations

from dataclasses import dataclass
import tkinter as tk
from tkinter import ttk
from typing import Any, Optional

from ui.theme import Palette, apply_theme, get_palette


@dataclass(frozen=True)
class Shortcut:
    """Definition of a registered keyboard shortcut."""

    key: str
    scope: str
    description: str
    handler_name: str


# Single source of truth registry for all UI shortcuts
SHORTCUTS: list[Shortcut] = [
    # Global shortcuts
    Shortcut("Ctrl+M", "global", "Toggle microphone listening", "_on_ctrl_m"),
    Shortcut("Ctrl+Shift+M", "global", "Toggle hands-free listening", "_on_toggle_handsfree"),
    Shortcut("Ctrl+Space", "global", "Toggle microphone listening", "_on_ctrl_space"),
    Shortcut("Ctrl+K", "global", "Open command palette", "_on_ctrl_k"),
    Shortcut("F1", "global", "Show keyboard shortcuts overlay", "_on_f1"),
    Shortcut("?", "global", "Show shortcuts overlay (when chat not focused)", "_on_question"),
    Shortcut("Ctrl+1", "global", "Dock: Activity tab", "_on_dock_activity"),
    Shortcut("Ctrl+2", "global", "Dock: Routines tab", "_on_dock_routines"),
    Shortcut("Ctrl+3", "global", "Dock: Toggle microphone", "_on_toggle_mic"),
    Shortcut("Ctrl+4", "global", "Dock: Settings window", "_on_settings_click"),
    Shortcut("Ctrl+5", "global", "Dock: Doctor window", "_on_doctor_click"),

    # Chat input box
    Shortcut("Return", "chat", "Send message", "_on_enter"),
    Shortcut("Shift+Return", "chat", "Insert newline in input box", "_on_shift_enter"),
    Shortcut("Ctrl+M", "chat", "Toggle microphone listening", "_on_ctrl_m"),

    # HUD Canvas navigation (when focused)
    Shortcut("Up", "hud", "Tilt sphere pitch up", "_on_key_up"),
    Shortcut("Down", "hud", "Tilt sphere pitch down", "_on_key_down"),
    Shortcut("Left", "hud", "Rotate sphere yaw left", "_on_key_left"),
    Shortcut("Right", "hud", "Rotate sphere yaw right", "_on_key_right"),
    Shortcut("+", "hud", "Zoom in orb", "_on_key_zoom_in"),
    Shortcut("-", "hud", "Zoom out orb", "_on_key_zoom_out"),
    Shortcut("0", "hud", "Reset sphere rotation and zoom", "_on_key_reset"),
    Shortcut("Space", "hud", "Toggle microphone", "_on_key_space"),
    Shortcut("Ctrl+Space", "hud", "Toggle microphone", "_on_key_ctrl_space"),
    Shortcut("m", "hud", "Open radial quick menu", "_on_key_m"),
    Shortcut("?", "hud", "Show shortcuts overlay", "_on_key_help"),
    Shortcut("Escape", "hud", "Close radial menu", "_on_key_escape"),

    # Dock Canvas navigation (when focused)
    Shortcut("Left", "dock", "Focus previous dock item", "_on_key_left"),
    Shortcut("Right", "dock", "Focus next dock item", "_on_key_right"),
    Shortcut("Return", "dock", "Activate focused dock item", "_on_key_activate"),
    Shortcut("Space", "dock", "Activate focused dock item", "_on_key_activate"),

    # Dialogs (ConfirmDialog, PlanDialog)
    Shortcut("Return", "dialog", "Confirm / Allow proposed action", "_on_enter"),
    Shortcut("KP_Enter", "dialog", "Confirm / Allow proposed action", "_on_enter"),
    Shortcut("Escape", "dialog", "Cancel / Deny proposed action", "_on_cancel"),

    # Radial Quick Menu
    Shortcut("Left", "radial", "Select previous action", "_on_key_prev"),
    Shortcut("Right", "radial", "Select next action", "_on_key_next"),
    Shortcut("Up", "radial", "Select previous action", "_on_key_prev"),
    Shortcut("Down", "radial", "Select next action", "_on_key_next"),
    Shortcut("Tab", "radial", "Select next action", "_on_key_next"),
    Shortcut("Shift+Tab", "radial", "Select previous action", "_on_key_prev"),
    Shortcut("Return", "radial", "Execute selected action", "_on_key_activate"),
    Shortcut("Escape", "radial", "Close radial menu", "_on_key_close"),

    # Command Palette
    Shortcut("Up", "palette", "Select previous result", "_on_key_up"),
    Shortcut("Down", "palette", "Select next result", "_on_key_down"),
    Shortcut("Return", "palette", "Execute selected action", "_on_key_enter"),
    Shortcut("Escape", "palette", "Close command palette", "_on_key_escape"),
]


def get_shortcuts(scope: Optional[str] = None) -> list[Shortcut]:
    """Retrieve registered shortcuts, optionally filtered by scope."""
    if scope is None:
        return list(SHORTCUTS)
    return [s for s in SHORTCUTS if s.scope == scope]


def format_shortcuts_overlay_text() -> str:
    """Format human-readable shortcuts listing grouped by functional scope."""
    sections: list[str] = []
    scope_titles = [
        ("global", "GLOBAL SHORTCUTS"),
        ("chat", "CHAT INPUT"),
        ("hud", "HUD ORB NAVIGATION (WHEN FOCUSED)"),
        ("dock", "DOCK NAVIGATION (WHEN FOCUSED)"),
        ("radial", "RADIAL QUICK MENU"),
        ("palette", "COMMAND PALETTE (CTRL+K)"),
        ("dialog", "APPROVAL & PLAN DIALOGS"),
    ]

    for scope_key, title in scope_titles:
        items = get_shortcuts(scope_key)
        if not items:
            continue
        lines = [f"=== {title} ==="]
        for item in items:
            lines.append(f"  {item.key:<14}  {item.description}")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def validate_registry() -> tuple[bool, list[str]]:
    """Validate registry consistency, detecting any collisions or prohibited input captures.

    Returns:
        (is_valid, list_of_error_messages)
    """
    errors: list[str] = []

    # Check for duplicates within the same scope
    seen: set[tuple[str, str]] = set()
    for s in SHORTCUTS:
        pair = (s.scope, s.key.lower())
        if pair in seen:
            errors.append(f"Duplicate shortcut registered: scope='{s.scope}', key='{s.key}'")
        seen.add(pair)

    # Prohibited keys in chat input box: only Return, Shift+Return, and Ctrl+M are allowed
    allowed_chat_keys = {"return", "shift+return", "ctrl+m"}
    for s in get_shortcuts("chat"):
        if s.key.lower() not in allowed_chat_keys:
            errors.append(f"Prohibited chat key binding: '{s.key}' in scope 'chat'")

    return (len(errors) == 0, errors)


class ShortcutsOverlay(tk.Toplevel):
    """Clean, themed modal-free window displaying the complete shortcuts registry."""

    def __init__(
        self,
        parent: tk.Widget,
        palette: Optional[Palette] = None,
        on_close: Optional[Any] = None,
    ) -> None:
        super().__init__(parent)
        self.title("Keyboard Shortcuts")
        self.geometry("540x520")
        self.minsize(440, 360)
        self._on_close_cb = on_close

        pal = palette or get_palette("gold")
        apply_theme(self, pal)
        self.configure(bg=pal.bg)

        # Container
        frame = tk.Frame(self, bg=pal.bg, padx=16, pady=14)
        frame.pack(fill="both", expand=True)

        header = tk.Label(
            frame,
            text="ORBIT KEYBOARD SHORTCUTS",
            font=("Segoe UI", 12, "bold"),
            fg=pal.accent,
            bg=pal.bg,
        )
        header.pack(anchor="w", pady=(0, 10))

        # Text display area
        text_widget = tk.Text(
            frame,
            bg=pal.panel,
            fg=pal.text,
            insertbackground=pal.text,
            relief="solid",
            bd=1,
            font=("Consolas", 9),
            padx=10,
            pady=10,
            wrap="word",
        )
        text_widget.pack(fill="both", expand=True)

        overlay_content = format_shortcuts_overlay_text()
        text_widget.insert("1.0", overlay_content)
        text_widget.configure(state="disabled")

        # Bottom dismiss button
        btn_frame = tk.Frame(frame, bg=pal.bg, pady=8)
        btn_frame.pack(fill="x")

        close_btn = ttk.Button(btn_frame, text="Close (Esc)", command=self._do_close)
        close_btn.pack(side="right")

        self.bind("<Escape>", lambda e: self._do_close())
        self.protocol("WM_DELETE_WINDOW", self._do_close)

        # Center over parent
        try:
            self.transient(parent.winfo_toplevel())
            px = parent.winfo_rootx() + (parent.winfo_width() // 2) - 270
            py = parent.winfo_rooty() + (parent.winfo_height() // 2) - 260
            self.geometry(f"+{max(10, px)}+{max(10, py)}")
        except Exception:
            pass

        self.focus_set()

    def _do_close(self) -> None:
        if self._on_close_cb is not None:
            try:
                self._on_close_cb()
            except Exception:
                pass
        self.destroy()
