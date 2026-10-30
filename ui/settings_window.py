"""Settings editor window for ORBIT.

Displays whitelisted non-secret configuration settings, source origin tags,
environment override warnings, restart-required markers, and provides atomic
saving with .env.bak creation and doctor validation.
"""

from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, Optional, Union

from config.settings_store import (
    SPECS,
    SaveResult,
    SettingsError,
    read_effective,
    save,
    validate,
)
from core.doctor import Probes, check_env_file


class SettingsWindow(tk.Toplevel):
    """Configuration dialog for editable non-secret settings."""

    def __init__(
        self,
        parent: tk.Tk,
        controller: Optional[Any] = None,
        env_path: Optional[Union[str, Path]] = None,
        on_saved: Optional[Callable[[SaveResult], None]] = None,
        environ: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__(parent)
        self.parent_root = parent
        self.controller = controller
        self.env_path = Path(env_path) if env_path is not None else Path(".env")
        self.on_saved = on_saved

        self.title("ORBIT Settings")
        self.transient(parent)
        self.geometry("720x640")
        self.minsize(580, 480)

        # Load effective settings
        self.effective = read_effective(env_path=self.env_path, environ=environ)
        self.widget_vars: dict[str, tk.Variable] = {}

        self._build_ui()

    def _build_ui(self) -> None:
        main_frame = ttk.Frame(self, padding=12)
        main_frame.pack(fill="both", expand=True)

        header_lbl = ttk.Label(
            main_frame,
            text="Settings Configuration",
            font=("Segoe UI", 12, "bold"),
        )
        header_lbl.pack(anchor="w", pady=(0, 4))

        sub_lbl = ttk.Label(
            main_frame,
            text="Non-secret settings stored in .env. Secrets (API keys) are managed by hand.",
            font=("Segoe UI", 9, "italic"),
            foreground="#4b5563",
        )
        sub_lbl.pack(anchor="w", pady=(0, 10))

        # Scrollable container for setting rows
        canvas = tk.Canvas(main_frame, borderwidth=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(main_frame, orient="vertical", command=canvas.yview)
        scroll_content = ttk.Frame(canvas)

        scroll_content.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas_window = canvas.create_window((0, 0), window=scroll_content, anchor="nw")

        def _on_canvas_configure(event):
            canvas.itemconfig(canvas_window, width=event.width)

        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="top", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y", in_=canvas)

        # One row per spec
        for row_idx, (key, spec) in enumerate(SPECS.items()):
            eff = self.effective.get(key)
            eff_val = eff.value if eff else spec.default
            source = eff.source if eff else "default"
            overridden = eff.overridden_by_env if eff else False

            row = ttk.Frame(scroll_content, padding=(4, 6))
            row.pack(fill="x", expand=True)

            # Left block: Key and description
            key_box = ttk.Frame(row, width=220)
            key_box.pack(side="left", anchor="w")

            key_lbl = ttk.Label(
                key_box,
                text=key,
                font=("Segoe UI", 9, "bold"),
            )
            key_lbl.pack(anchor="w")

            desc_lbl = ttk.Label(
                key_box,
                text=spec.description,
                font=("Segoe UI", 8),
                foreground="#6b7280",
                wraplength=210,
            )
            desc_lbl.pack(anchor="w")

            # Control widget block
            ctrl_box = ttk.Frame(row, width=180)
            ctrl_box.pack(side="left", padx=(10, 10))

            if spec.type == "bool":
                var = tk.BooleanVar(value=(eff_val == "1"))
                self.widget_vars[key] = var
                cb = ttk.Checkbutton(
                    ctrl_box,
                    text="Enabled" if key != "ORBIT_MOCK" else "Mock mode (no real model)",
                    variable=var,
                )
                cb.pack(anchor="w")
            elif spec.type == "enum":
                init_val = eff_val or ("empty" if key == "STT_LANGUAGE" and not eff_val else eff_val)
                var = tk.StringVar(value=init_val)
                self.widget_vars[key] = var
                opts = list(spec.allowed or ())
                combo = ttk.Combobox(
                    ctrl_box,
                    textvariable=var,
                    values=opts,
                    state="readonly",
                    width=14,
                )
                combo.pack(anchor="w")
            elif spec.type == "int":
                var = tk.StringVar(value=eff_val)
                self.widget_vars[key] = var
                spin = ttk.Spinbox(
                    ctrl_box,
                    from_=spec.min_val or 0,
                    to=spec.max_val or 99999,
                    textvariable=var,
                    width=10,
                )
                spin.pack(anchor="w")
            elif spec.type == "float":
                var = tk.StringVar(value=eff_val)
                self.widget_vars[key] = var
                ent = ttk.Entry(ctrl_box, textvariable=var, width=10)
                ent.pack(anchor="w")
            else:  # text
                var = tk.StringVar(value=eff_val)
                self.widget_vars[key] = var
                ent = ttk.Entry(ctrl_box, textvariable=var, width=18)
                ent.pack(anchor="w")

            # Status / Origin tag block
            tag_box = ttk.Frame(row)
            tag_box.pack(side="left", fill="x", expand=True)

            source_lbl = ttk.Label(
                tag_box,
                text=f"[{source}]",
                font=("Segoe UI", 8),
                foreground="#4b5563",
            )
            source_lbl.pack(side="left")

            if overridden:
                over_lbl = tk.Label(
                    tag_box,
                    text="⚠ overridden by environment",
                    font=("Segoe UI", 8, "italic"),
                    fg="#d97706",
                )
                over_lbl.pack(side="left", padx=(6, 0))

            if spec.restart_required:
                rst_lbl = ttk.Label(
                    tag_box,
                    text="restart required",
                    font=("Segoe UI", 8, "italic"),
                    foreground="#9ca3af",
                )
                rst_lbl.pack(side="right", padx=(8, 0))

        # Voice & Neural TTS status panel (read-only)
        from config.paths import voices_dir
        from voice.voices import list_voices

        voice_panel = ttk.LabelFrame(main_frame, text="Voice & Neural TTS", padding=(10, 6))
        voice_panel.pack(fill="x", side="bottom", pady=(8, 0))

        engine = self.effective.get("TTS_ENGINE").value if "TTS_ENGINE" in self.effective else "sapi"
        selected_voice = self.effective.get("PIPER_VOICE").value if "PIPER_VOICE" in self.effective else ""

        try:
            v_dir = voices_dir()
            valid_voices = list_voices(v_dir)
        except Exception:
            valid_voices = []

        voices_str = ", ".join(valid_voices) if valid_voices else "(none found in data/voices)"

        info_box = ttk.Frame(voice_panel)
        info_box.pack(side="left", fill="x", expand=True)

        engine_lbl = ttk.Label(
            info_box,
            text=f"Active TTS Engine: {engine}" + (f" | Model: {selected_voice}" if selected_voice else ""),
            font=("Segoe UI", 9, "bold"),
        )
        engine_lbl.pack(anchor="w")

        found_lbl = ttk.Label(
            info_box,
            text=f"Installed Voices: {voices_str}",
            font=("Segoe UI", 8),
            foreground="#4b5563",
        )
        found_lbl.pack(anchor="w", pady=(2, 0))

        self.test_voice_btn = ttk.Button(
            voice_panel,
            text="Test Voice",
            command=self._on_test_voice,
        )
        self.test_voice_btn.pack(side="right", padx=(6, 0))

        # Bottom Action Bar
        btn_bar = ttk.Frame(main_frame, padding=(0, 12, 0, 0))
        btn_bar.pack(fill="x", side="bottom")

        cancel_btn = ttk.Button(btn_bar, text="Cancel", command=self.destroy)
        cancel_btn.pack(side="right", padx=(6, 0))

        save_btn = ttk.Button(btn_bar, text="Save Settings", command=self._on_save)
        save_btn.pack(side="right")

    def _on_test_voice(self) -> None:
        speaker = None
        vc = getattr(self.controller, "voice_controller", None)
        if vc is not None:
            speaker = getattr(vc, "speaker", None)
        if speaker is None:
            from voice.tts_engines import get_speaker

            speaker = get_speaker()
        if speaker and hasattr(speaker, "speak"):
            speaker.speak("This is ORBIT. Voice check complete.", blocking=False)

    def _on_save(self) -> None:
        from config.paths import is_demo_mode

        if is_demo_mode():
            messagebox.showerror("Save Error", "Settings can't be changed in demo mode", parent=self)
            return

        changes: dict[str, str] = {}

        for key, spec in SPECS.items():
            var = self.widget_vars.get(key)
            if var is None:
                continue

            if spec.type == "bool":
                raw_val = "1" if bool(var.get()) else "0"
            else:
                raw_val = str(var.get()).strip()

            try:
                norm_val = validate(key, raw_val)
            except SettingsError as exc:
                messagebox.showerror("Invalid Setting", str(exc), parent=self)
                return

            current_eff = self.effective.get(key)
            current_val = current_eff.value if current_eff else spec.default

            if norm_val != current_val:
                changes[key] = norm_val

        # Check PLAN_PREVIEW=never confirmation
        if changes.get("PLAN_PREVIEW") == "never":
            proceed = messagebox.askyesno(
                "Disable Plan Preview?",
                "Plans will run without a preview dialog. Per-step approvals for apps and sites still apply.\n\nProceed with saving?",
                parent=self,
            )
            if not proceed:
                return

        # Check ROUTINE_PREVIEW=never confirmation
        if changes.get("ROUTINE_PREVIEW") == "never":
            proceed = messagebox.askyesno(
                "Disable Routine Preview?",
                "Routines will run without a preview dialog. Per-step approvals for apps and sites still apply.\n\nProceed with saving?",
                parent=self,
            )
            if not proceed:
                return

        if not changes:
            messagebox.showinfo("Settings", "No settings were changed.", parent=self)
            self.destroy()
            return

        try:
            result = save(self.env_path, changes)
        except SettingsError as exc:
            messagebox.showerror("Save Error", str(exc), parent=self)
            return

        # Audit logging for saved changes
        audit_logger = None
        if self.controller:
            app = getattr(self.controller, "app", None)
            if app and getattr(app, "agent", None):
                audit_logger = getattr(app.agent, "audit", None)
            elif getattr(self.controller, "agent", None):
                audit_logger = getattr(self.controller.agent, "audit", None)

        if audit_logger is not None:
            for k, (old_v, new_v) in result.changed.items():
                audit_logger.log(
                    tool="settings",
                    args={"key": k, "old_value": old_v, "new_value": new_v},
                    decision="settings_change",
                    outcome="success",
                )

        # Run read-only doctor .env check
        target_path = self.env_path
        probes = Probes(
            path_exists=lambda p: Path(target_path if p == ".env" else p).exists(),
            read_text=lambda p: Path(target_path if p == ".env" else p).read_text(encoding="utf-8", errors="replace"),
        )
        doc_check = check_env_file(probes)

        # Construct feedback dialog message
        msg_parts = [
            f"Changed keys: {', '.join(result.changed.keys())}",
            ".env.bak was created.",
            "Restart ORBIT to apply.",
        ]
        if result.warnings:
            msg_parts.append("")
            msg_parts.extend(result.warnings)

        if doc_check.status == "FAIL":
            msg_parts.append("")
            msg_parts.append(f"Doctor .env Notice:\n{doc_check.detail}")

        messagebox.showinfo("Settings Saved", "\n".join(msg_parts), parent=self)

        if self.on_saved:
            self.on_saved(result)

        self.destroy()
