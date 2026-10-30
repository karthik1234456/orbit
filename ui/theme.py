"""Color palettes, WCAG contrast calculations, font selection, and TTK styling for ORBIT HUD interface.

Pure theming definitions with no active behaviors:
- Palettes: 'cyan', 'gold', and 'mono'
- WCAG contrast verification
- Heading and data font fallbacks
- TTK 'clam' theme configuration
- Chat text tag styling
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Palette:
    """Color palette specification for ORBIT UI and HUD components."""

    name: str
    bg: str
    panel: str
    panel_edge: str
    text: str
    text_dim: str
    accent: str
    accent_dim: str
    ok: str
    warn: str
    danger: str


PALETTES: dict[str, Palette] = {
    "cyan": Palette(
        name="cyan",
        bg="#05080c",          # Near-black deep teal
        panel="#09141c",       # Dark teal panel
        panel_edge="#154354",  # Subtle teal edge
        text="#e0f7fa",        # Bright icy white-cyan (>= 15:1 contrast)
        text_dim="#80deea",    # Aqua secondary text (>= 12:1 contrast)
        accent="#00e5ff",      # Electric cyan highlight
        accent_dim="#007a87",  # Deep cyan glow
        ok="#00e676",          # Neon emerald
        warn="#ffb300",        # Warm amber
        danger="#ff1744",      # High-tech warning red
    ),
    "gold": Palette(
        name="gold",
        bg="#08080d",          # Near-black warm obsidian with dark vignette
        panel="#181510",       # Warm graphite/gold panel
        panel_edge="#7a6640",  # Burnished bronze panel edge (#7A6640)
        text="#fff8e7",        # Champagne white (>= 17:1 contrast)
        text_dim="#e6cc9a",    # Champagne secondary text (>= 12:1 contrast)
        accent="#d6ae47",      # Accent gold highlight (#D6AE47)
        accent_dim="#7e6c48",  # Dim bronze glow (#7E6C48)
        ok="#81c784",          # Soft sage green
        warn="#ffa000",        # Amber alert
        danger="#e53935",      # Deep crimson
    ),
    "mono": Palette(
        name="mono",
        bg="#0a0c0f",          # Deep graphite slate
        panel="#14181f",       # Dark slate panel
        panel_edge="#2b323d",  # Subtle charcoal border
        text="#f4f6f9",        # Crisp white (>= 16:1 contrast)
        text_dim="#a8b4c4",    # Silver slate secondary text (>= 8:1 contrast)
        accent="#ffffff",      # Pure white highlight
        accent_dim="#64748b",  # Cool slate glow
        ok="#4ade80",          # Mint green
        warn="#fbbf24",        # Amber
        danger="#f87171",      # Coral red
    ),
}


def get_palette(name: Optional[str] = None) -> Palette:
    """Retrieve color palette by name, defaulting to 'cyan' if unknown or unset."""
    if not name:
        return PALETTES["cyan"]
    return PALETTES.get(name.strip().lower(), PALETTES["cyan"])


def _channel_luminance(val: float) -> float:
    """Compute relative luminance channel contribution according to sRGB specification."""
    if val <= 0.04045:
        return val / 12.92
    return ((val + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_code: str) -> float:
    """Compute relative luminance of a #rrggbb or #rgb color string."""
    clean = hex_code.lstrip("#")
    if len(clean) == 3:
        clean = "".join(c * 2 for c in clean)
    if len(clean) != 6:
        return 0.0
    r = int(clean[0:2], 16) / 255.0
    g = int(clean[2:4], 16) / 255.0
    b = int(clean[4:6], 16) / 255.0
    return 0.2126 * _channel_luminance(r) + 0.7152 * _channel_luminance(g) + 0.0722 * _channel_luminance(b)


def contrast_ratio(fg: str, bg: str) -> float:
    """Calculate WCAG 2.1 relative contrast ratio between foreground and background colors."""
    l1 = relative_luminance(fg)
    l2 = relative_luminance(bg)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def get_heading_font(root: Optional[Any] = None, size: int = 11) -> tuple[str, int, str]:
    """Select modern heading font with graceful system fallback."""
    available: list[str] = []
    if root is not None:
        try:
            import tkinter.font as tkfont
            available = [f.lower() for f in tkfont.families(root)]
        except Exception:
            pass

    for candidate in ("bahnschrift light", "bahnschrift semilight", "bahnschrift", "segoe ui light", "segoe ui", "helvetica", "arial"):
        if not available or candidate in available:
            return (candidate.title(), size, "bold")
    return ("Helvetica", size, "bold")


def get_data_font(root: Optional[Any] = None, size: int = 9) -> tuple[str, int]:
    """Select monospaced data font with graceful system fallback."""
    available: list[str] = []
    if root is not None:
        try:
            import tkinter.font as tkfont
            available = [f.lower() for f in tkfont.families(root)]
        except Exception:
            pass

    for candidate in ("consolas", "cascadia code", "courier new"):
        if not available or candidate in available:
            return (candidate.title(), size)
    return ("TkFixedFont", size)


def apply_theme(root: Any, palette: Palette) -> None:
    """Apply styling across ttk widgets and configure text tags using the specified palette."""
    try:
        from tkinter import ttk
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # General defaults
        style.configure(".", background=palette.bg, foreground=palette.text)
        style.configure("TFrame", background=palette.bg)
        style.configure("TLabel", background=palette.bg, foreground=palette.text)

        # Buttons
        style.configure(
            "TButton",
            background=palette.panel,
            foreground=palette.text,
            borderwidth=1,
            bordercolor=palette.panel_edge,
            lightcolor=palette.panel,
            darkcolor=palette.panel,
            focuscolor=palette.accent,
            padding=(6, 2),
        )
        style.map(
            "TButton",
            background=[("active", palette.panel_edge), ("disabled", palette.bg)],
            foreground=[("disabled", palette.text_dim)],
        )

        # Checkbuttons
        style.configure(
            "TCheckbutton",
            background=palette.bg,
            foreground=palette.text,
            focuscolor=palette.accent,
        )
        style.map(
            "TCheckbutton",
            background=[("active", palette.bg)],
            foreground=[("disabled", palette.text_dim)],
        )

        # Entry and Combobox
        style.configure(
            "TEntry",
            fieldbackground=palette.panel,
            foreground=palette.text,
            insertcolor=palette.accent,
            bordercolor=palette.panel_edge,
            lightcolor=palette.panel,
            darkcolor=palette.panel,
        )
        style.configure(
            "TCombobox",
            fieldbackground=palette.panel,
            background=palette.panel,
            foreground=palette.text,
            arrowcolor=palette.accent,
            bordercolor=palette.panel_edge,
        )

        # Scrollbars
        style.configure(
            "TScrollbar",
            troughcolor=palette.bg,
            background=palette.panel,
            bordercolor=palette.panel_edge,
            arrowcolor=palette.text_dim,
        )
        style.map("TScrollbar", background=[("active", palette.panel_edge)])

        # Progressbar
        style.configure(
            "TProgressbar",
            troughcolor=palette.panel,
            background=palette.accent,
            bordercolor=palette.panel_edge,
        )

        # Separator
        style.configure("TSeparator", background=palette.panel_edge)

        # Notebook & Tabs
        style.configure(
            "TNotebook",
            background=palette.bg,
            borderwidth=0,
            tabmargins=[2, 2, 2, 0],
        )
        tab_selected_fg = "#E6CC9A" if palette.name == "gold" else palette.accent
        tab_idle_fg = "#6E5C3E" if palette.name == "gold" else palette.text_dim
        style.configure(
            "TNotebook.Tab",
            background=palette.panel,
            foreground=tab_idle_fg,
            padding=[8, 3],
            bordercolor=palette.panel_edge,
            lightcolor=palette.panel,
            darkcolor=palette.panel,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", palette.bg), ("active", palette.panel_edge)],
            foreground=[("selected", tab_selected_fg), ("active", palette.text)],
            bordercolor=[("selected", palette.accent_dim)],
        )

        # Treeview
        style.configure(
            "Treeview",
            background=palette.panel,
            foreground=palette.text,
            fieldbackground=palette.panel,
            bordercolor=palette.panel_edge,
            borderwidth=1,
        )
        style.map(
            "Treeview",
            background=[("selected", palette.panel_edge)],
            foreground=[("selected", palette.accent)],
        )
        style.configure(
            "Treeview.Heading",
            background=palette.bg,
            foreground=palette.text_dim,
            bordercolor=palette.panel_edge,
            relief="flat",
            padding=[4, 2],
        )
        style.map(
            "Treeview.Heading",
            background=[("active", palette.panel)],
            foreground=[("active", palette.text)],
        )
    except Exception:
        pass


def style_chat_tags(text_widget: Any, palette: Palette) -> None:
    """Configure color tags on a Tkinter ScrolledText or Text chat widget."""
    try:
        text_widget.configure(
            background=palette.panel,
            foreground=palette.text,
            insertbackground=palette.accent,
            selectbackground=palette.panel_edge,
            selectforeground=palette.text,
        )
        text_widget.tag_configure("user", foreground=palette.accent, font=("Segoe UI", 10, "bold"))
        text_widget.tag_configure("orbit_prefix", foreground=palette.ok, font=("Segoe UI", 10, "bold"))
        text_widget.tag_configure("orbit", foreground=palette.text, font=("Segoe UI", 10))
        text_widget.tag_configure("system", foreground=palette.text_dim, font=("Segoe UI", 9, "italic"))
        text_widget.tag_configure("error", foreground=palette.danger, font=("Segoe UI", 10, "bold"))
        bubble_bg = "#302B23" if palette.name == "gold" else palette.panel
        text_widget.tag_configure("bubble", background=bubble_bg, foreground=palette.text)
    except Exception:
        pass

