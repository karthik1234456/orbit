"""Interactive standalone preview tool for the ORBIT high-tech HUD interface.

Key controls:
- Keys 1-6: Force HUD states (1: Idle, 2: Listening, 3: Thinking, 4: Speaking, 5: Confirming, 6: Error)
- M: Cycle motion modes (full -> reduced -> off)
- T: Cycle theme palettes (cyan -> gold -> mono)
- Mic Slider: Adjust simulated microphone audio level
- Dock icons: Click or use Left/Right arrows + Enter or Ctrl+1..5 to test actions
"""

from __future__ import annotations

from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk

# Ensure project root is in sys.path when executed directly as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.version import VERSION
from ui.dock import DockCanvas
from ui.hud import HudCanvas
from ui.hud_core import HudModel
from ui.theme import apply_theme, get_data_font, get_palette


class HudPreviewApp:
    """Preview window runner for inspecting and debugging the ORBIT HUD."""

    THEMES = ["cyan", "gold", "mono"]
    MOTIONS = ["full", "reduced", "off"]

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"ORBIT v{VERSION} - HUD Preview & Telemetry")
        self.root.geometry("800x680")
        self.root.minsize(640, 520)

        self.theme_idx = 0
        self.motion_idx = 0
        self.palette = get_palette(self.THEMES[self.theme_idx])
        apply_theme(self.root, self.palette)

        self.model = HudModel()
        self._manual_state: str = "auto"

        self._build_ui()
        self._bind_keys()
        self._update_telemetry()

    def _build_ui(self) -> None:
        self.root.configure(bg=self.palette.bg)

        # Header info banner
        self.header_frame = tk.Frame(self.root, bg=self.palette.bg, pady=6)
        self.header_frame.pack(fill="x", padx=12)

        self.title_lbl = tk.Label(
            self.header_frame,
            text=f"ORBIT SCI-FI HUD SYSTEM v{VERSION}",
            font=("Segoe UI", 11, "bold"),
            fg=self.palette.accent,
            bg=self.palette.bg,
        )
        self.title_lbl.pack(side="left")

        self.telemetry_lbl = tk.Label(
            self.header_frame,
            text="",
            font=get_data_font(self.root, 9),
            fg=self.palette.text_dim,
            bg=self.palette.bg,
        )
        self.telemetry_lbl.pack(side="right")

        # Main HUD Canvas
        self.hud_canvas = HudCanvas(
            self.root,
            model=self.model,
            palette=self.palette,
            fps=30,
            motion=self.MOTIONS[self.motion_idx],
            height=300,
        )
        self.hud_canvas.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        # Bottom Dock Canvas
        self.dock_canvas = DockCanvas(
            self.root,
            palette=self.palette,
            on_activity=lambda: self._set_notice("Dock Trigger: Activity"),
            on_routines=lambda: self._set_notice("Dock Trigger: Routines"),
            on_mic=self._toggle_mic_state,
            on_settings=lambda: self._set_notice("Dock Trigger: Settings"),
            on_doctor=lambda: self._set_notice("Dock Trigger: Doctor"),
            get_voice_state=self._get_voice_state,
        )
        self.dock_canvas.pack(fill="x", padx=12, pady=(0, 6))

        # Controls panel
        ctrl_frame = tk.Frame(self.root, bg=self.palette.panel, bd=1, relief="solid")
        ctrl_frame.pack(fill="x", padx=12, pady=(0, 10))

        # State buttons row
        btn_row = tk.Frame(ctrl_frame, bg=self.palette.panel, pady=6, padx=8)
        btn_row.pack(fill="x")

        states = [
            ("1: Idle", self._set_idle),
            ("2: Listening", self._set_listening),
            ("3: Thinking", self._set_thinking),
            ("4: Speaking", self._set_speaking),
            ("5: Confirm", self._set_confirming),
            ("6: Error", self._set_error),
        ]
        for txt, cmd in states:
            b = ttk.Button(btn_row, text=txt, command=cmd)
            b.pack(side="left", padx=3)

        # Mode toggles & mic slider
        slider_row = tk.Frame(ctrl_frame, bg=self.palette.panel, pady=6, padx=8)
        slider_row.pack(fill="x")

        self.theme_btn = ttk.Button(slider_row, text="Theme (T): cyan", command=self._cycle_theme)
        self.theme_btn.pack(side="left", padx=4)

        self.motion_btn = ttk.Button(slider_row, text="Motion (M): full", command=self._cycle_motion)
        self.motion_btn.pack(side="left", padx=4)

        tk.Label(
            slider_row,
            text="Simulated Mic:",
            bg=self.palette.panel,
            fg=self.palette.text_dim,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(12, 4))

        self.mic_scale = ttk.Scale(
            slider_row,
            from_=0.0,
            to=1.0,
            value=0.0,
            orient="horizontal",
            command=self._on_scale_change,
        )
        self.mic_scale.pack(side="left", fill="x", expand=True, padx=4)

        # Notice readout
        self.notice_lbl = tk.Label(
            ctrl_frame,
            text="Ready. Keys: 1-6 (states), M (motion), T (theme), Arrows+Enter (dock)",
            bg=self.palette.panel,
            fg=self.palette.ok,
            font=("Segoe UI", 9, "italic"),
            pady=4,
        )
        self.notice_lbl.pack(fill="x")

    def _bind_keys(self) -> None:
        self.root.bind("<Key-1>", lambda e: self._set_idle())
        self.root.bind("<Key-2>", lambda e: self._set_listening())
        self.root.bind("<Key-3>", lambda e: self._set_thinking())
        self.root.bind("<Key-4>", lambda e: self._set_speaking())
        self.root.bind("<Key-5>", lambda e: self._set_confirming())
        self.root.bind("<Key-6>", lambda e: self._set_error())
        self.root.bind("<Key-t>", lambda e: self._cycle_theme())
        self.root.bind("<Key-T>", lambda e: self._cycle_theme())
        self.root.bind("<Key-m>", lambda e: self._cycle_motion())
        self.root.bind("<Key-M>", lambda e: self._cycle_motion())

    def _set_idle(self) -> None:
        self._manual_state = "idle"
        self.model.set_confirming(False)
        self.model.set_busy(False)
        self.model.set_speaking(False)
        self.model.set_listening(False)
        self._set_notice("State set to IDLE")

    def _set_listening(self) -> None:
        self._manual_state = "listening"
        self.model.set_confirming(False)
        self.model.set_busy(False)
        self.model.set_speaking(False)
        self.model.set_listening(True)
        self._set_notice("State set to LISTENING")

    def _set_thinking(self) -> None:
        self._manual_state = "thinking"
        self.model.set_confirming(False)
        self.model.set_busy(True)
        self.model.set_speaking(False)
        self.model.set_listening(False)
        self._set_notice("State set to THINKING (busy)")

    def _set_speaking(self) -> None:
        self._manual_state = "speaking"
        self.model.set_confirming(False)
        self.model.set_busy(False)
        self.model.set_speaking(True)
        self.model.set_listening(False)
        self._set_notice("State set to SPEAKING")

    def _set_confirming(self) -> None:
        self._manual_state = "confirming"
        self.model.set_confirming(True)
        self.model.set_busy(False)
        self.model.set_speaking(False)
        self.model.set_listening(False)
        self._set_notice("State set to CONFIRMING (approval)")

    def _set_error(self) -> None:
        self._manual_state = "error"
        self.model.flash_error()
        self._set_notice("State set to ERROR (1.5s decay)")

    def _cycle_theme(self) -> None:
        self.theme_idx = (self.theme_idx + 1) % len(self.THEMES)
        theme_name = self.THEMES[self.theme_idx]
        self.palette = get_palette(theme_name)
        apply_theme(self.root, self.palette)
        self.root.configure(bg=self.palette.bg)
        self.header_frame.configure(bg=self.palette.bg)
        self.title_lbl.configure(bg=self.palette.bg, fg=self.palette.accent)
        self.telemetry_lbl.configure(bg=self.palette.bg, fg=self.palette.text_dim)
        self.hud_canvas.set_palette(self.palette)
        self.dock_canvas.set_palette(self.palette)
        self.theme_btn.configure(text=f"Theme (T): {theme_name}")
        self._set_notice(f"Theme switched to {theme_name}")

    def _cycle_motion(self) -> None:
        self.motion_idx = (self.motion_idx + 1) % len(self.MOTIONS)
        motion_name = self.MOTIONS[self.motion_idx]
        self.hud_canvas.motion = motion_name
        self.motion_btn.configure(text=f"Motion (M): {motion_name}")
        self._set_notice(f"Motion mode set to {motion_name}")

    def _on_scale_change(self, val_str: str) -> None:
        try:
            val = float(val_str)
            self.model.set_mic_level(val)
            self.dock_canvas.set_mic_level(val)
        except Exception:
            pass

    def _toggle_mic_state(self) -> None:
        if self._manual_state == "listening":
            self._set_idle()
        else:
            self._set_listening()

    def _get_voice_state(self) -> tuple[str, bool, str]:
        st = "listening" if self._manual_state == "listening" else "idle"
        return (st, False, "")

    def _set_notice(self, msg: str) -> None:
        self.notice_lbl.configure(text=msg)

    def _update_telemetry(self) -> None:
        st_name = self.model.state.value.upper()
        tier = self.model.tier.upper()
        avg_ft = self.model.average_frame_time * 1000.0
        downgrades = self.model.downgrades_count
        self.telemetry_lbl.configure(
            text=f"STATE: {st_name} | TIER: {tier} | FRAME: {avg_ft:.1f}ms | DROPS: {downgrades}"
        )
        self.root.after(200, self._update_telemetry)


def main() -> None:
    root = tk.Tk()
    HudPreviewApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
