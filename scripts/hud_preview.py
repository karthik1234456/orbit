"""Interactive standalone preview tool for the ORBIT high-tech HUD interface.

Key controls:
- Keys 1-6: Force HUD states (1: Idle, 2: Listening, 3: Thinking, 4: Speaking, 5: Confirming, 6: Error)
- M: Cycle motion modes (full -> reduced -> off)
- T: Cycle theme palettes (cyan -> gold -> mono)
- B: Cycle realism modes (full -> lite -> off)
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
from ui.hud_core import HudModel, layout
from ui.theme import apply_theme, get_data_font, get_palette


class HudPreviewApp:
    """Preview window runner for inspecting and debugging the ORBIT HUD."""

    THEMES = ["cyan", "gold", "mono"]
    MOTIONS = ["full", "reduced", "off"]
    REALISMS = ["full", "lite", "off"]

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"ORBIT v{VERSION} - HUD Preview & Telemetry")
        self.root.geometry("800x680")
        self.root.minsize(640, 520)

        self.theme_idx = 0
        self.motion_idx = 0
        self.realism_idx = 0
        self.palette = get_palette(self.THEMES[self.theme_idx])
        apply_theme(self.root, self.palette)

        self.model = HudModel()
        self._manual_state: str = "auto"

        self._ref_visible: bool = False
        self._ref_photo = None
        self.ref_label = None
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

        # Main HUD Canvas & Reference Container
        self.hud_container = tk.Frame(self.root, bg=self.palette.bg)
        self.hud_container.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        self.ref_label = tk.Label(self.hud_container, bg=self.palette.bg)

        self.hud_canvas = HudCanvas(
            self.hud_container,
            model=self.model,
            palette=self.palette,
            fps=30,
            motion=self.MOTIONS[self.motion_idx],
            height=300,
        )
        self.hud_canvas.pack(side="right", fill="both", expand=True)

        # Bottom Dock Canvas
        # Size dock canvas from layout so circles are strictly inside canvas
        try:
            tk_s = float(self.root.tk.call("tk", "scaling"))
        except Exception:
            tk_s = 1.0
        init_ly = layout(800, 680, scaling=tk_s)

        self.dock_canvas = DockCanvas(
            self.root,
            palette=self.palette,
            height=init_ly.dock_height,
            on_activity=lambda: self._set_notice("Dock Trigger: Activity"),
            on_routines=lambda: self._set_notice("Dock Trigger: Routines"),
            on_mic=self._toggle_mic_state,
            on_settings=lambda: self._set_notice("Dock Trigger: Settings"),
            on_doctor=lambda: self._set_notice("Dock Trigger: Doctor"),
            get_voice_state=self._get_voice_state,
        )
        self.dock_canvas.pack(fill="x", padx=12, pady=(0, 6))
        self.root.bind("<Configure>", self._on_window_configure, add="+")

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

        # Pulse buttons row
        pulse_row = tk.Frame(ctrl_frame, bg=self.palette.panel, pady=4, padx=8)
        pulse_row.pack(fill="x")
        ttk.Button(pulse_row, text="Pulse (P)", command=self._trigger_full_pulse).pack(side="left", padx=2)
        ttk.Button(pulse_row, text="Complete (C)", command=self._trigger_mild_pulse).pack(side="left", padx=2)
        ttk.Button(pulse_row, text="Heartbeat (H)", command=self._trigger_heartbeat_pulse).pack(side="left", padx=2)
        ttk.Button(pulse_row, text="Reply (S)", command=self._trigger_soft_pulse).pack(side="left", padx=2)
        ttk.Button(pulse_row, text="Show Ref (R)", command=self._toggle_reference).pack(side="left", padx=2)

        # Mode toggles & mic slider
        slider_row = tk.Frame(ctrl_frame, bg=self.palette.panel, pady=6, padx=8)
        slider_row.pack(fill="x")

        self.theme_btn = ttk.Button(slider_row, text="Theme (T): cyan", command=self._cycle_theme)
        self.theme_btn.pack(side="left", padx=4)

        self.motion_btn = ttk.Button(slider_row, text="Motion (M): full", command=self._cycle_motion)
        self.motion_btn.pack(side="left", padx=4)

        self.realism_btn = ttk.Button(slider_row, text="Realism (B): full", command=self._cycle_realism)
        self.realism_btn.pack(side="left", padx=4)

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
            text="Ready. Keys: 1-6 (states), P/C/H/S (pulses), R (ref), M (motion), T (theme), B (realism)",
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
        self.root.bind("<Key-b>", lambda e: self._cycle_realism())
        self.root.bind("<Key-B>", lambda e: self._cycle_realism())
        self.root.bind("<Key-M>", lambda e: self._cycle_motion())
        self.root.bind("<Key-r>", lambda e: self._toggle_reference())
        self.root.bind("<Key-R>", lambda e: self._toggle_reference())
        self.root.bind("<Key-p>", lambda e: self._trigger_full_pulse())
        self.root.bind("<Key-P>", lambda e: self._trigger_full_pulse())
        self.root.bind("<Key-c>", lambda e: self._trigger_mild_pulse())
        self.root.bind("<Key-C>", lambda e: self._trigger_mild_pulse())
        self.root.bind("<Key-h>", lambda e: self._trigger_heartbeat_pulse())
        self.root.bind("<Key-H>", lambda e: self._trigger_heartbeat_pulse())
        self.root.bind("<Key-s>", lambda e: self._trigger_soft_pulse())
        self.root.bind("<Key-S>", lambda e: self._trigger_soft_pulse())

    def _trigger_full_pulse(self) -> None:
        self.model.trigger_pulse("full", 1.0)
        self._set_notice("Triggered FULL pulse (amplitude 1.0)")

    def _trigger_mild_pulse(self) -> None:
        self.model.trigger_pulse("mild", 0.45)
        self._set_notice("Triggered MILD pulse for task completion (amplitude 0.45)")

    def _trigger_heartbeat_pulse(self) -> None:
        self.model.trigger_pulse("heartbeat", 0.25)
        self._set_notice("Triggered HEARTBEAT pulse (amplitude 0.25)")

    def _trigger_soft_pulse(self) -> None:
        self.model.trigger_pulse("soft", 0.50)
        self._set_notice("Triggered SOFT reply pulse (amplitude 0.50)")

    def _toggle_reference(self) -> None:
        if self._ref_visible:
            self._ref_visible = False
            if self.ref_label is not None:
                self.ref_label.pack_forget()
            self._set_notice("Reference image hidden")
            return

        ref_path = Path(__file__).resolve().parent.parent / "design_reference" / "reference.png"
        if not ref_path.exists():
            self._set_notice("design_reference/reference.png not found")
            return

        try:
            from PIL import Image, ImageTk
            img = Image.open(ref_path)
            h = max(240, self.hud_canvas.winfo_height())
            aspect = img.width / max(1, img.height)
            w = int(h * aspect)
            img = img.resize((w, h), Image.Resampling.LANCZOS)
            self._ref_photo = ImageTk.PhotoImage(img)
            self.ref_label.configure(image=self._ref_photo, bg=self.palette.bg)
            self.ref_label.pack(side="left", padx=(0, 6), fill="y")
            self._ref_visible = True
            self._set_notice("Showing design reference beside live HUD")
        except Exception as exc:
            self._set_notice(f"Failed to load reference image: {exc}")

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

    def _cycle_realism(self) -> None:
        self.realism_idx = (self.realism_idx + 1) % len(self.REALISMS)
        r_name = self.REALISMS[self.realism_idx]
        self.hud_canvas.realism = r_name
        self.hud_canvas._schedule_realism_build()
        self.realism_btn.configure(text=f"Realism (B): {r_name}")
        self._set_notice(f"Realism mode set to {r_name}")

    def _on_window_configure(self, event: tk.Event) -> None:
        if event.widget == self.root:
            try:
                tk_s = float(self.root.tk.call("tk", "scaling"))
            except Exception:
                tk_s = 1.0
            ly = layout(event.width, event.height, scaling=tk_s)
            self.dock_canvas.configure(height=ly.dock_height)

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
        inst_ft = getattr(self.hud_canvas, "_last_frame_duration", 0.0) * 1000.0
        downgrades = self.model.downgrades_count
        items_last = getattr(self.hud_canvas, "items_touched_last_frame", 0)
        items_avg = getattr(self.hud_canvas, "items_touched_average", 0.0)
        realism_mode = getattr(self.hud_canvas, "realism", "full").upper()
        self.telemetry_lbl.configure(
            text=(
                f"STATE: {st_name} | TIER: {tier} | FRAME: {avg_ft:.1f}ms (last: {inst_ft:.1f}ms) | "
                f"ITEMS: {items_last} (avg: {items_avg:.0f}) | REALISM: {realism_mode} | DROPS: {downgrades}"
            )
        )
        self.root.after(200, self._update_telemetry)


def main() -> None:
    root = tk.Tk()
    HudPreviewApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
