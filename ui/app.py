"""Graphical user interface for ORBIT assistant built with Tkinter and TTK.

This module provides the primary window layout, streaming conversation view,
multi-tab activity/memory/docs inspector, and secure modal confirmation dialog.
Worker threads communicate strictly via ChatController; all widget updates
execute exclusively on the Tkinter main thread.
"""

import json
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from tkinter import messagebox, scrolledtext, ttk
from typing import Any, Callable, Optional

from config import settings
from core.version import VERSION
from ui.dock import DockCanvas
from ui.hud import HudCanvas
from ui.hud_core import HudModel
from ui.theme import apply_theme, get_data_font, get_palette, style_chat_tags

LAST_HUD_ERROR: Optional[str] = None
from ui.controller import ChatController, ConfirmRequest, UiEvent


class ToolTip:
    """Simple Tkinter tooltip displayed on hover."""

    def __init__(self, widget: tk.Widget, text: str = "") -> None:
        self.widget = widget
        self.text = text
        self.tip_window: Optional[tk.Toplevel] = None
        self.widget.bind("<Enter>", self.show_tip)
        self.widget.bind("<Leave>", self.hide_tip)

    def set_text(self, text: str) -> None:
        self.text = text

    def show_tip(self, event: Optional[tk.Event] = None) -> None:
        if not self.text or self.tip_window or not self.widget.winfo_exists():
            return
        try:
            x = self.widget.winfo_rootx() + 20
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
            self.tip_window = tw = tk.Toplevel(self.widget)
            tw.wm_overrideredirect(True)
            tw.wm_geometry(f"+{x}+{y}")
            label = ttk.Label(
                tw,
                text=self.text,
                justify="left",
                relief="solid",
                borderwidth=1,
                font=("Segoe UI", 8),
                padding=(4, 2),
            )
            label.pack()
        except Exception:
            self.tip_window = None

    def hide_tip(self, event: Optional[tk.Event] = None) -> None:
        if self.tip_window:
            try:
                self.tip_window.destroy()
            except Exception:
                pass
            self.tip_window = None


class ConfirmDialog(tk.Toplevel):
    """Modal dialog prompting the user to authorize a sensitive tool execution.

    Security guarantees:
    - Mouse and keyboard only: Voice input NEVER resolves, skips, shortens, or auto-approves confirmations.
    - Deny is focused and default.
    - Allow button is disabled for the first 800ms to prevent accidental click-throughs.
    - Closing the window, Escape, or timeout triggers automatic DENY.
    - Enter key does NOT approve.
    - Resolves confirmation via ChatController strictly once.
    """

    def __init__(
        self,
        parent: tk.Tk,
        controller: ChatController,
        request: ConfirmRequest,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.request = request
        self.request_id = request.id
        self._resolved = False
        self._countdown_timer: Optional[str] = None
        self._remaining_s = float(request.timeout_s)

        self.title("Action Confirmation Required")
        self.transient(parent)
        self.resizable(False, False)
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass
        palette = getattr(parent, "palette", None) or get_palette()
        try:
            self.configure(bg=palette.bg)
        except Exception:
            pass

        # Build modal content
        self._build_ui()

        # Modal grab
        self.grab_set()

        # Keybindings and window protocol
        self.protocol("WM_DELETE_WINDOW", lambda: self._resolve(False))
        self.bind("<Escape>", lambda e: self._resolve(False))
        self.bind("<Destroy>", self._on_dialog_destroy)
        # Enter must NEVER mean Allow
        self.bind("<Return>", lambda e: self._on_enter())
        self.bind("<KP_Enter>", lambda e: self._on_enter())

        # Allow button starts disabled for 800ms
        self.allow_btn.configure(state="disabled")
        self.after(800, self._enable_allow)

        # Deny button receives initial focus
        self._initial_focus = self.deny_btn
        self.deny_btn.focus_set()

        # Start countdown timer
        self._tick_countdown()

    def focus_get(self) -> Any:
        """Return the currently focused widget, falling back to initial focus when withdrawn."""
        focused = super().focus_get()
        if focused is not None:
            return focused
        return getattr(self, "_initial_focus", None)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        header_lbl = ttk.Label(
            frame,
            text=f"ORBIT requests permission to run: {self.request.tool}",
            font=("Segoe UI", 11, "bold"),
        )
        header_lbl.pack(anchor="w", pady=(0, 8))

        if self.request.warning:
            warn_lbl = tk.Label(
                frame,
                text=f"⚠ WARNING: {self.request.warning}",
                fg="red",
                font=("Segoe UI", 10, "bold"),
                wraplength=450,
                justify="left",
            )
            warn_lbl.pack(anchor="w", pady=(0, 8))

        args_lbl = ttk.Label(frame, text="Parameters (redacted):")
        args_lbl.pack(anchor="w", pady=(0, 4))

        palette = getattr(self.master, "palette", None) or get_palette()
        self.args_text = tk.Text(
            frame,
            height=6,
            width=55,
            wrap=tk.WORD,
            font=("Consolas", 9),
            bg=palette.panel,
            fg=palette.text,
            insertbackground=palette.accent,
        )
        self.args_text.pack(fill="both", expand=True, pady=(0, 10))
        try:
            formatted_args = json.dumps(self.request.args, indent=2)
        except Exception:
            formatted_args = str(self.request.args)
        self.args_text.insert("1.0", formatted_args)
        # Selectable but read-only
        self.args_text.configure(state="disabled")

        self.countdown_lbl = ttk.Label(
            frame,
            text=f"auto-deny in {int(self._remaining_s)} s",
            font=("Segoe UI", 9, "italic"),
        )
        self.countdown_lbl.pack(anchor="w", pady=(0, 12))

        # Button bar
        btn_bar = ttk.Frame(frame)
        btn_bar.pack(fill="x", side="bottom")

        self.deny_btn = ttk.Button(
            btn_bar,
            text="Deny (Esc)",
            default="active",
            command=lambda: self._resolve(False),
        )
        self.deny_btn.pack(side="right", padx=(6, 0))

        self.allow_btn = ttk.Button(
            btn_bar,
            text="Allow",
            command=lambda: self._resolve(True),
        )
        self.allow_btn.pack(side="right")

    def _enable_allow(self) -> None:
        if not self._resolved and self.winfo_exists():
            self.allow_btn.configure(state="normal")

    def _tick_countdown(self) -> None:
        if self._resolved or not self.winfo_exists():
            return
        if self._remaining_s <= 0:
            self._resolve(False)
            return

        self.countdown_lbl.configure(text=f"auto-deny in {int(self._remaining_s)} s")
        self._remaining_s -= 1.0
        self._countdown_timer = self.after(1000, self._tick_countdown)

    def _on_enter(self) -> str:
        # Enter triggers safe default (Deny)
        self._resolve(False)
        return "break"

    def _on_dialog_destroy(self, event: Optional[tk.Event] = None) -> None:
        if event is None or getattr(event, "widget", None) == self:
            if not self._resolved:
                self._resolve(False)

    def _resolve(self, allowed: bool) -> None:
        if self._resolved:
            return
        self._resolved = True

        if self._countdown_timer is not None:
            try:
                self.after_cancel(self._countdown_timer)
            except Exception:
                pass
            self._countdown_timer = None

        try:
            self.controller.resolve_confirmation(self.request_id, allowed)
        finally:
            try:
                self.grab_release()
            except Exception:
                pass
            try:
                self.destroy()
            except Exception:
                pass


class PlanDialog(tk.Toplevel):
    """Modal dialog prompting the user to preview and authorize execution of a plan.

    Security guarantees:
    - Mouse and keyboard only: Voice input NEVER resolves, skips, shortens, or auto-approves plan previews.
    - Cancel is focused and default.
    - Run button is disabled for the first 800ms to prevent accidental click-throughs.
    - Closing the window, Escape, or timeout triggers automatic Cancel (DENY).
    - Enter key never approves (triggers safe default: Cancel).
    - Resolves confirmation via ChatController strictly once.
    """

    def __init__(
        self,
        parent: tk.Tk,
        controller: ChatController,
        plan_id: Any,
        labels: list[str],
        permissions: list[str],
        timeout_s: float = 60.0,
        kind: str = "plan",
        name: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.plan_id = plan_id
        self.labels = list(labels or [])
        self.permissions = list(permissions or [])
        self.kind = kind
        self.name = name
        self._resolved = False
        self._countdown_timer: Optional[str] = None
        self._remaining_s = float(timeout_s)

        if self.kind == "save_routine":
            self.title("Save Routine")
        else:
            self.title("Plan Preview Required")
        self.transient(parent)
        self.resizable(False, False)
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass
        palette = getattr(parent, "palette", None) or get_palette()
        try:
            self.configure(bg=palette.bg)
        except Exception:
            pass

        self._build_ui()
        self.text_area = self.steps_text
        self.grab_set()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda e: self._on_cancel())
        self.bind("<Destroy>", self._on_dialog_destroy)
        self.bind("<Return>", lambda e: self._on_enter())
        self.bind("<KP_Enter>", lambda e: self._on_enter())

        self.run_btn.configure(state="disabled")
        self.after(800, self._enable_run)

        self._initial_focus = self.cancel_btn
        self.cancel_btn.focus_set()

        self._tick_countdown()

    def _on_close(self) -> None:
        self._resolve(False)

    def _on_cancel(self) -> None:
        self._resolve(False)

    @property
    def _remaining(self) -> float:
        return self._remaining_s

    @_remaining.setter
    def _remaining(self, val: float) -> None:
        self._remaining_s = float(val)

    def _tick(self) -> None:
        self._tick_countdown()

    def focus_get(self) -> Any:
        """Return the currently focused widget, falling back to initial focus when withdrawn."""
        focused = super().focus_get()
        if focused is not None:
            return focused
        return getattr(self, "_initial_focus", None)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        if self.kind == "save_routine":
            header_text = f"Save routine '{self.name}' with these steps?"
        elif self.kind == "routine":
            header_text = f"Routine '{self.name}' ({len(self.labels)} steps):"
        else:
            header_text = f"ORBIT requests permission to run a plan ({len(self.labels)} steps):"

        header_lbl = ttk.Label(
            frame,
            text=header_text,
            font=("Segoe UI", 11, "bold"),
        )
        header_lbl.pack(anchor="w", pady=(0, 8))

        steps_lines = []
        for idx, lbl in enumerate(self.labels, 1):
            perm = self.permissions[idx - 1] if idx - 1 < len(self.permissions) else "SAFE"
            note = " (asks for approval)" if perm in ("asks for approval", "confirm", True) else ""
            steps_lines.append(f"{idx}. {lbl}{note}")

        if self.kind != "save_routine":
            steps_lines.append("")
            steps_lines.append("Each step that opens apps or sites may still ask for its own approval")

        steps_text_content = "\n".join(steps_lines)

        palette = getattr(self.master, "palette", None) or get_palette()
        self.steps_text = tk.Text(
            frame,
            height=min(8, max(3, len(self.labels) + 2)),
            width=55,
            wrap=tk.WORD,
            font=("Consolas", 9),
            bg=palette.panel,
            fg=palette.text,
            insertbackground=palette.accent,
        )
        self.steps_text.pack(fill="both", expand=True, pady=(0, 6))
        self.steps_text.insert("1.0", steps_text_content)
        self.steps_text.configure(state="disabled")

        if self.kind != "save_routine":
            note_lbl = ttk.Label(
                frame,
                text="Each step that opens apps or sites may still ask for its own approval",
                font=("Segoe UI", 9, "italic"),
                wraplength=450,
                justify="left",
            )
            note_lbl.pack(anchor="w", pady=(0, 8))

        self.countdown_lbl = ttk.Label(
            frame,
            text=f"auto-cancel in {int(self._remaining_s)} s",
            font=("Segoe UI", 9, "italic"),
        )
        self.countdown_lbl.pack(anchor="w", pady=(0, 12))

        # Button bar
        btn_bar = ttk.Frame(frame)
        btn_bar.pack(fill="x", side="bottom")

        self.cancel_btn = ttk.Button(
            btn_bar,
            text="Cancel (Esc)",
            default="active",
            command=lambda: self._resolve(False),
        )
        self.cancel_btn.pack(side="right", padx=(6, 0))

        run_text = "Save" if self.kind == "save_routine" else "Run"
        self.run_btn = ttk.Button(
            btn_bar,
            text=run_text,
            command=lambda: self._resolve(True),
        )
        self.run_btn.pack(side="right")

    def _enable_run(self) -> None:
        if not self._resolved and self.winfo_exists():
            self.run_btn.configure(state="normal")

    def _tick_countdown(self) -> None:
        if self._resolved or not self.winfo_exists():
            return
        if self._remaining_s <= 0:
            self._resolve(False)
            return

        self.countdown_lbl.configure(text=f"auto-cancel in {int(self._remaining_s)} s")
        self._remaining_s -= 1.0
        self._countdown_timer = self.after(1000, self._tick_countdown)

    def _on_enter(self) -> str:
        # Enter triggers safe default (Cancel)
        self._resolve(False)
        return "break"

    def _on_dialog_destroy(self, event: Optional[tk.Event] = None) -> None:
        if event is None or getattr(event, "widget", None) == self:
            if not self._resolved:
                self._resolve(False)

    def _resolve(self, allowed: bool) -> None:
        if self._resolved:
            return
        self._resolved = True

        if self._countdown_timer is not None:
            try:
                self.after_cancel(self._countdown_timer)
            except Exception:
                pass
            self._countdown_timer = None

        try:
            self.controller.resolve_plan(self.plan_id, allowed)
        finally:
            try:
                self.grab_release()
            except Exception:
                pass
            try:
                self.destroy()
            except Exception:
                pass


class ReminderToast(tk.Toplevel):
    """Non-modal notification toast for fired reminders and timers.

    Displays reminder text, Dismiss, and Snooze 5 min buttons.
    Never grabs focus or interrupts typing in the main window.
    Auto-destroys after 60 seconds.
    """

    def __init__(
        self,
        parent: tk.Tk,
        item: Any,
        on_dismiss: Callable[[int], None],
        on_snooze: Callable[[int], None],
        on_close: Optional[Callable[["ReminderToast"], None]] = None,
    ) -> None:
        super().__init__(parent)
        self.parent_root = parent
        self.item = item
        self.on_dismiss = on_dismiss
        self.on_snooze = on_snooze
        self.on_close_cb = on_close
        self._closed = False

        self.title("Reminder")
        self.resizable(False, False)
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass

        # Ensure window and widgets never accept keyboard focus or grab
        self.configure(takefocus=0)

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)

        header_lbl = ttk.Label(frame, text="Reminder", font=("Segoe UI", 10, "bold"))
        header_lbl.pack(anchor="w", pady=(0, 4))

        item_text = getattr(item, "text", "") or ""
        text_lbl = ttk.Label(
            frame,
            text=item_text,
            wraplength=260,
            font=("Segoe UI", 9),
            justify="left",
        )
        text_lbl.pack(anchor="w", fill="x", expand=True, pady=(0, 8))

        btn_bar = ttk.Frame(frame)
        btn_bar.pack(fill="x", side="bottom")

        self.dismiss_btn = ttk.Button(
            btn_bar,
            text="Dismiss",
            takefocus=0,
            command=self._do_dismiss,
        )
        self.dismiss_btn.pack(side="right", padx=(4, 0))

        self.snooze_btn = ttk.Button(
            btn_bar,
            text="Snooze 5 min",
            takefocus=0,
            command=self._do_snooze,
        )
        self.snooze_btn.pack(side="right")

        # Auto-close after 60 seconds
        self._auto_close_id: Optional[str] = self.after(60000, self._auto_close)

        # Deflect any focus back to the parent
        self.bind("<FocusIn>", self._deflect_focus)

        self.protocol("WM_DELETE_WINDOW", self._do_dismiss)

    def _deflect_focus(self, event: Optional[tk.Event] = None) -> None:
        try:
            if hasattr(self.parent_root, "input_text"):
                self.parent_root.input_text.focus_set()
            elif hasattr(self.parent_root, "focus_set"):
                self.parent_root.focus_set()
        except Exception:
            pass

    def _do_dismiss(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._auto_close_id is not None:
            try:
                self.after_cancel(self._auto_close_id)
            except Exception:
                pass
            self._auto_close_id = None
        try:
            item_id = getattr(self.item, "id", None)
            if item_id is not None:
                self.on_dismiss(item_id)
        finally:
            self._destroy_self()

    def _do_snooze(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._auto_close_id is not None:
            try:
                self.after_cancel(self._auto_close_id)
            except Exception:
                pass
            self._auto_close_id = None
        try:
            item_id = getattr(self.item, "id", None)
            if item_id is not None:
                self.on_snooze(item_id)
        finally:
            self._destroy_self()

    def _auto_close(self) -> None:
        self._do_dismiss()

    def _destroy_self(self) -> None:
        if self.on_close_cb:
            try:
                self.on_close_cb(self)
            except Exception:
                pass
        try:
            self.destroy()
        except Exception:
            pass


class DoctorWindow(tk.Toplevel):
    """Read-only window displaying ORBIT Environment Doctor report."""

    def __init__(self, parent: tk.Tk, report_text: str) -> None:
        super().__init__(parent)
        self.title("ORBIT Environment Doctor")
        self.geometry("780x540")
        self.minsize(500, 350)
        self.report_text = report_text

        palette = getattr(parent, "palette", None) or get_palette()
        try:
            self.configure(bg=palette.bg)
        except Exception:
            pass

        container = ttk.Frame(self, padding=8)
        container.pack(fill="both", expand=True)

        self.text_area = scrolledtext.ScrolledText(
            container,
            wrap=tk.WORD,
            font=("Consolas", 9),
            bg=palette.panel,
            fg=palette.text,
            insertbackground=palette.accent,
        )
        self.text_area.insert("1.0", report_text)
        self.text_area.configure(state="disabled")
        self.text_area.pack(fill="both", expand=True, pady=(0, 8))

        btn_box = ttk.Frame(container)
        btn_box.pack(fill="x", side="bottom")

        self.copy_btn = ttk.Button(btn_box, text="Copy report", command=self._on_copy)
        self.copy_btn.pack(side="left")

        self.close_btn = ttk.Button(btn_box, text="Close", command=self.destroy)
        self.close_btn.pack(side="right")

    def _on_copy(self) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(self.report_text)
            self.copy_btn.configure(text="Copied!")
            self.after(2000, lambda: self.copy_btn.configure(text="Copy report") if self.winfo_exists() else None)
        except Exception:
            pass


class OrbitWindow:
    """Main application window for ORBIT desktop assistant."""

    def __init__(
        self,
        root: tk.Tk,
        controller: ChatController,
        mode_label: str,
        notice: Optional[str] = None,
        voice_controller: Optional[Any] = None,
    ) -> None:
        self.root = root
        self.controller = controller
        self.mode_label = mode_label
        self.notice = notice

        self._destroyed = False
        self._shutdown_called = False
        self._streaming = False
        self._confirm_dialog: Optional[ConfirmDialog] = None
        self._plan_dialog: Optional[PlanDialog] = None
        self._plan_total_steps: int = 0
        self._view_task_counter: int = 0
        self._current_view_task: int = 0
        self._last_activity_task: Optional[int] = None
        self._tasks_with_ui_error: set[int] = set()
        self._current_task_had_denial: bool = False
        self._pending_voice_reviewed: bool = False

        from config.paths import is_demo_mode

        base_title = f"ORBIT v{VERSION} Desktop Assistant"
        if is_demo_mode():
            base_title += " - DEMO MODE (sample data)"
        self.root.title(base_title)

        ui_style = getattr(settings, "UI_STYLE", "hud").lower().strip()
        if ui_style == "hud":
            self.root.geometry("1180x760")
            self.root.minsize(960, 640)
        else:
            self.root.geometry("1020x680")
            self.root.minsize(800, 500)

        theme_name = getattr(settings, "UI_THEME", "cyan")
        self.palette = get_palette(theme_name)
        apply_theme(self.root, self.palette)

        self.hud_model: Optional[HudModel] = None
        self.hud_canvas: Optional[HudCanvas] = None
        self.dock_canvas: Optional[DockCanvas] = None
        self._notebook_visible = True

        # Wire shutdown protocols
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Destroy>", self._on_root_destroyed)

        # Wire voice controller
        self._last_speaker_poll = 0.0
        self._speak_replies = bool(getattr(settings, "SPEAK_REPLIES", False))
        is_mock = "MOCK" in self.mode_label or bool(getattr(settings, "ORBIT_MOCK", False))
        if voice_controller is not None:
            self.voice_controller = voice_controller
        elif getattr(self.controller, "voice_controller", None) is not None:
            self.voice_controller = self.controller.voice_controller
        else:
            from ui.voice_controller import VoiceUiController
            if is_mock:
                from voice.audio import FakeRecorder
                from voice.stt import FakeTranscriber
                from voice.tts import NullSpeaker

                class _MockWindowRecorder(FakeRecorder):
                    def record(self, stop_event: Optional[threading.Event] = None) -> Any:
                        if stop_event is not None:
                            stop_event.wait(timeout=getattr(settings, "MAX_RECORD_SECONDS", 30))
                        return super().record(stop_event=stop_event)

                rec_fac = lambda level_callback=None: _MockWindowRecorder(level_callback=level_callback)
                trans_fac = lambda: FakeTranscriber(default_text="Voice mock response")
                speaker = NullSpeaker()
            else:
                from voice.audio import Recorder
                from voice.stt import WhisperTranscriber
                from voice.tts import SubprocessSpeaker

                rec_fac = lambda level_callback=None: Recorder(
                    device=getattr(settings, "MIC_DEVICE", None),
                    max_seconds=getattr(settings, "MAX_RECORD_SECONDS", 30),
                    level_callback=level_callback,
                )
                trans_fac = lambda: WhisperTranscriber(
                    model_name=getattr(settings, "STT_MODEL", "base.en"),
                    language=getattr(settings, "STT_LANGUAGE", "en"),
                )
                speaker = SubprocessSpeaker(rate=getattr(settings, "TTS_RATE", 180))

            emit_fn = getattr(self.controller, "_emit", None)
            if emit_fn is None:
                emit_fn = getattr(self.controller, "emit", None)
            if emit_fn is None:
                def fallback_emit(k: str, d: Optional[dict] = None) -> None:
                    evs = getattr(self.controller, "events", None)
                    if isinstance(evs, list):
                        evs.append(UiEvent(kind=k, data=d or {}))
                emit_fn = fallback_emit

            self.voice_controller = VoiceUiController(
                recorder_factory=rec_fac,
                transcriber_factory=trans_fac,
                speaker=speaker,
                emit=emit_fn,
                is_blocked=self._is_blocked,
            )
        try:
            self.controller.voice_controller = self.voice_controller
        except Exception:
            pass
        if hasattr(self.voice_controller, "speak_replies"):
            self.voice_controller.speak_replies = self._speak_replies

        # Wire reminder store and scheduler
        self.reminder_store = getattr(self.controller, "reminder_store", None)
        if self.reminder_store is None and hasattr(self.controller, "app"):
            self.reminder_store = getattr(self.controller.app, "reminder_store", None)
        self._active_toasts: list[ReminderToast] = []

        clock_fn = getattr(self.controller, "clock", None)
        if clock_fn is None and hasattr(self.controller, "app"):
            clock_fn = getattr(self.controller.app, "clock", None)
        if clock_fn is None:
            clock_fn = time.time
        self.clock = clock_fn

        if self.reminder_store is not None:
            from reminders.scheduler import ReminderScheduler, format_missed_message
            self.reminder_scheduler: Optional[ReminderScheduler] = ReminderScheduler(
                store=self.reminder_store,
                clock=self.clock,
                on_due=self._on_reminder_due,
            )
            try:
                missed = self.reminder_scheduler.startup()
                if missed:
                    msg = format_missed_message(missed)
                    self._append_chat(f"{msg}\n\n", ("system",))
            except Exception as exc:
                print(f"Error checking missed reminders: {exc}", file=sys.stderr)
        else:
            self.reminder_scheduler = None

        # Wire routine store
        self.routine_store = getattr(self.controller, "routine_store", None)
        if self.routine_store is None and hasattr(self.controller, "app"):
            self.routine_store = getattr(self.controller.app, "routine_store", None)

        self.hud_model = HudModel(clock=self.clock)

        # Build UI layout
        self._build_layout()

        # Migration notice flag
        self._migration_notified = False

        # Populate initial tab contents
        self._refresh_memories()
        self._refresh_documents()
        self._refresh_last_run()
        self._refresh_context_tab()
        self._refresh_reminders_tab()
        self._refresh_routines_tab()
        self._refresh_status_bar_reminder()
        self._check_index_migrated()

        # Start timer polling for controller events (UI thread only)
        self.root.after(50, self._poll)
        self.root.after(1000, self._poll_reminders)

        # Fast startup doctor check (background thread)
        if getattr(settings, "DOCTOR_ON_START", True):
            self._start_doctor_startup_check()

    # --- Voice Helpers & Callbacks ---

    def _is_blocked(self) -> bool:
        """Check if voice interaction should be blocked."""
        if getattr(self.controller, "is_busy", False):
            return True
        if self._confirm_dialog is not None and self._confirm_dialog.winfo_exists():
            return True
        if self._plan_dialog is not None and self._plan_dialog.winfo_exists():
            return True
        if getattr(self, "voice_controller", None) is not None:
            if self.voice_controller.is_speaking():
                return True
        return False

    def _check_voice_hardware_and_packages(self) -> tuple[bool, str]:
        """Check if audio input hardware and STT packages are available."""
        is_mock = "MOCK" in self.mode_label or bool(getattr(settings, "ORBIT_MOCK", False))
        if is_mock:
            return True, ""
        if sys.modules.get("sounddevice") is None:
            return False, "sounddevice is not installed"
        try:
            from voice.audio import list_input_devices
            devices = list_input_devices()
            if not devices:
                return False, "No microphone input device found"
        except Exception as exc:
            return False, f"Microphone unavailable: {exc}"

        if sys.modules.get("faster_whisper") is None:
            return False, "faster_whisper is not installed"
        try:
            import faster_whisper
            if faster_whisper is None:
                return False, "faster_whisper is not installed"
        except Exception as exc:
            return False, f"Speech recognition unavailable: {exc}"

        return True, ""

    def _update_voice_ui(self) -> None:
        """Update voice button, label, and tooltip to reflect current controller state."""
        if not getattr(self, "voice_controller", None) or not hasattr(self, "mic_btn"):
            return
        is_mock = "MOCK" in self.mode_label or bool(getattr(settings, "ORBIT_MOCK", False))
        vc_state = getattr(self.voice_controller, "state", "idle")
        can_use, reason = self._check_voice_hardware_and_packages()

        if not can_use:
            self.mic_btn.configure(state="disabled")
            if hasattr(self, "mic_tooltip"):
                self.mic_tooltip.set_text(reason)
            return

        blocked = self._is_blocked()
        if vc_state == "listening":
            self.mic_btn.configure(text="Stop recording", state="normal")
            if hasattr(self, "mic_tooltip"):
                self.mic_tooltip.set_text("Click to stop recording (Ctrl+M)")
        elif vc_state == "transcribing":
            self.mic_btn.configure(text="Transcribing...", state="disabled")
            if hasattr(self, "mic_tooltip"):
                self.mic_tooltip.set_text("Transcribing speech...")
        elif vc_state == "speaking":
            btn_text = "mock voice" if is_mock else "Mic"
            self.mic_btn.configure(text=btn_text, state="disabled")
            if hasattr(self, "mic_tooltip"):
                self.mic_tooltip.set_text("Speaking reply...")
        else:
            btn_text = "mock voice" if is_mock else "Mic"
            if blocked:
                self.mic_btn.configure(text=btn_text, state="disabled")
                if hasattr(self, "mic_tooltip"):
                    self.mic_tooltip.set_text("Voice input unavailable while busy")
            else:
                self.mic_btn.configure(text=btn_text, state="normal")
                if hasattr(self, "mic_tooltip"):
                    tip = "Mock voice recording (Ctrl+M)" if is_mock else "Click to talk (Ctrl+M)"
                    self.mic_tooltip.set_text(tip)

    def _on_toggle_mic(self) -> None:
        """Handle mic button click or Ctrl+M shortcut."""
        if getattr(self, "voice_controller", None) is None:
            return
        if self.voice_controller.state == "listening":
            self.voice_controller.stop_listening()
        else:
            if self._is_blocked():
                return
            self.voice_controller.start_listening()
        self._update_voice_ui()

    def _on_ctrl_m(self, event: Optional[tk.Event] = None) -> str:
        """Handle Ctrl+M shortcut to toggle microphone."""
        if hasattr(self, "mic_btn") and str(self.mic_btn.cget("state")) != "disabled":
            self._on_toggle_mic()
        return "break"

    def _on_toggle_speak_replies(self) -> None:
        """Handle toggle of Speak replies checkbox."""
        val = bool(self.speak_replies_var.get())
        self._speak_replies = val
        if getattr(self, "voice_controller", None) is not None:
            self.voice_controller.speak_replies = val

    def _build_layout(self) -> None:
        ui_style = getattr(settings, "UI_STYLE", "hud").lower().strip()
        if ui_style == "classic":
            self._build_classic_layout()
            return

        try:
            self._build_hud_layout()
        except Exception as exc:
            import traceback
            traceback.print_exc(file=sys.stderr)
            global LAST_HUD_ERROR
            LAST_HUD_ERROR = f"HUD initialization failed: {exc}"
            self._cleanup_failed_hud_build()
            self._build_classic_layout()
            self._append_chat(
                f"HUD initialization failed ({exc}); falling back to classic layout.\n\n",
                ("system",),
            )

    def _cleanup_failed_hud_build(self) -> None:
        if self.hud_canvas is not None:
            try:
                self.hud_canvas.destroy()
            except Exception:
                pass
            self.hud_canvas = None
        if self.dock_canvas is not None:
            try:
                self.dock_canvas.destroy()
            except Exception:
                pass
            self.dock_canvas = None
        for child in list(self.root.winfo_children()):
            try:
                child.destroy()
            except Exception:
                pass

    def _build_hud_layout(self) -> None:
        main_container = tk.Frame(self.root, bg=self.palette.bg)
        main_container.pack(fill="both", expand=True)

        # Status Bar at the bottom
        self._build_status_bar(main_container)

        # Dock Canvas right above the status bar
        self.dock_canvas = DockCanvas(
            main_container,
            palette=self.palette,
            on_activity=self._on_dock_activity,
            on_routines=self._on_dock_routines,
            on_mic=self._on_toggle_mic,
            on_settings=self._on_settings_click,
            on_doctor=self._on_doctor_click,
            get_voice_state=self._get_dock_voice_state,
        )
        self.dock_canvas.pack(fill="x", side="bottom", pady=(2, 4))

        # Main horizontal split: Center (HUD + Chat + Input) | Right (Tabs)
        self.paned = ttk.PanedWindow(main_container, orient="horizontal")
        self.paned.pack(fill="both", expand=True, padx=8, pady=(8, 2))

        # --- Center Column ---
        self.left_frame = tk.Frame(self.paned, bg=self.palette.bg)
        self.paned.add(self.left_frame, weight=3)

        # 1. Top HUD Canvas
        fps_val = getattr(settings, "HUD_FPS", 30)
        motion_val = getattr(settings, "UI_MOTION", "full")
        self.hud_canvas = HudCanvas(
            self.left_frame,
            model=self.hud_model,
            palette=self.palette,
            fps=fps_val,
            motion=motion_val,
            height=210,
        )
        self.hud_canvas.pack(fill="x", side="top", pady=(0, 4))

        # 2. Chat history & input container
        chat_box = tk.Frame(self.left_frame, bg=self.palette.bg)
        chat_box.pack(fill="both", expand=True)

        self.history = scrolledtext.ScrolledText(
            chat_box,
            wrap=tk.WORD,
            state="disabled",
            font=get_data_font(self.root, 10),
            bg=self.palette.panel,
            fg=self.palette.text,
            insertbackground=self.palette.accent,
            selectbackground=self.palette.panel_edge,
            selectforeground=self.palette.text,
            bd=1,
            relief="solid",
            highlightthickness=1,
            highlightbackground=self.palette.panel_edge,
            highlightcolor=self.palette.accent_dim,
        )
        self.history.pack(fill="both", expand=True, pady=(0, 6))
        style_chat_tags(self.history, self.palette)

        # Voice status bar (above input frame, packed when active)
        self.voice_bar = tk.Frame(chat_box, bg=self.palette.bg, padx=0, pady=2)
        self.voice_status_lbl = tk.Label(
            self.voice_bar,
            text="",
            font=("Segoe UI", 9, "italic"),
            fg=self.palette.accent,
            bg=self.palette.bg,
        )
        self.voice_status_lbl.pack(side="left", padx=(0, 8))

        self.level_bar = ttk.Progressbar(
            self.voice_bar,
            orient="horizontal",
            mode="determinate",
            length=100,
            maximum=100,
        )

        # Input frame
        input_frame = tk.Frame(chat_box, bg=self.palette.bg)
        input_frame.pack(fill="x", side="bottom")

        self.input_text = tk.Text(
            input_frame,
            height=3,
            wrap=tk.WORD,
            font=get_data_font(self.root, 10),
            bg=self.palette.panel,
            fg=self.palette.text,
            insertbackground=self.palette.accent,
            selectbackground=self.palette.panel_edge,
            selectforeground=self.palette.text,
            bd=1,
            relief="solid",
            highlightthickness=1,
            highlightbackground=self.palette.panel_edge,
            highlightcolor=self.palette.accent_dim,
        )
        self.input_text.pack(side="left", fill="both", expand=True, padx=(0, 6))

        # Bind Enter to send and Shift+Enter to newline
        self.input_text.bind("<Return>", self._on_enter)
        self.input_text.bind("<Shift-Return>", self._on_shift_enter)

        btn_box = tk.Frame(input_frame, bg=self.palette.bg)
        btn_box.pack(side="right", fill="y")

        self.send_btn = ttk.Button(btn_box, text="Send", command=self._on_send)
        self.send_btn.pack(fill="x", pady=(0, 2))

        is_mock = "MOCK" in self.mode_label or bool(getattr(settings, "ORBIT_MOCK", False))
        mic_text = "mock voice" if is_mock else "Mic"
        self.mic_btn = ttk.Button(btn_box, text=mic_text, command=self._on_toggle_mic)
        self.mic_btn.pack(fill="x", pady=(0, 2))
        self.mic_tooltip = ToolTip(self.mic_btn, "")

        self.stop_btn = ttk.Button(btn_box, text="Stop", command=self._on_stop)
        self.stop_btn.pack(fill="x", pady=(0, 2))
        self.stop_btn.configure(state="disabled")

        self.speak_replies_var = tk.BooleanVar(value=self._speak_replies)
        self.speak_replies_cb = ttk.Checkbutton(
            btn_box,
            text="Speak replies",
            variable=self.speak_replies_var,
            command=self._on_toggle_speak_replies,
        )
        self.speak_replies_cb.pack(fill="x")

        # Hide mic button and speak replies checkbox if VOICE_IN_UI is False
        if not getattr(settings, "VOICE_IN_UI", True):
            self.mic_btn.pack_forget()
            self.speak_replies_cb.pack_forget()

        # Keyboard shortcuts for voice
        self.input_text.bind("<Control-m>", self._on_ctrl_m)
        self.input_text.bind("<Control-M>", self._on_ctrl_m)
        self.root.bind("<Control-m>", self._on_ctrl_m)
        self.root.bind("<Control-M>", self._on_ctrl_m)

        self._update_voice_ui()

        # --- Right Column: Notebook ---
        self.right_frame = tk.Frame(self.paned, bg=self.palette.bg, padx=2, pady=2)
        self.paned.add(self.right_frame, weight=2)

        self.notebook = ttk.Notebook(self.right_frame)
        self.notebook.pack(fill="both", expand=True)

        self._build_activity_tab()
        self._build_context_tab()
        self._build_memories_tab()
        self._build_documents_tab()
        self._build_reminders_tab()
        self._build_routines_tab()
        self._build_last_run_tab()

    def _build_classic_layout(self) -> None:
        # Main vertical splitter: content area + bottom status bar
        main_container = ttk.Frame(self.root)
        main_container.pack(fill="both", expand=True)

        # Horizontal paned layout: Left (Chat + Input) | Right (Tabs)
        self.paned = ttk.PanedWindow(main_container, orient="horizontal")
        self.paned.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        # --- Left Pane: Chat history & input ---
        self.left_frame = ttk.Frame(self.paned, padding=4)
        self.paned.add(self.left_frame, weight=3)

        # Read-only ScrolledText for chat history
        self.history = scrolledtext.ScrolledText(
            self.left_frame,
            wrap=tk.WORD,
            state="disabled",
            font=("Segoe UI", 10),
        )
        self.history.pack(fill="both", expand=True, pady=(0, 8))

        # Color and style tags
        style_chat_tags(self.history, self.palette)

        # Voice status bar (above input frame, packed when active)
        self.voice_bar = ttk.Frame(self.left_frame, padding=(0, 2))
        self.voice_status_lbl = ttk.Label(
            self.voice_bar,
            text="",
            font=("Segoe UI", 9, "italic"),
            foreground="#1976d2",
        )
        self.voice_status_lbl.pack(side="left", padx=(0, 8))

        self.level_bar = ttk.Progressbar(
            self.voice_bar,
            orient="horizontal",
            mode="determinate",
            length=100,
            maximum=100,
        )

        # Input frame
        input_frame = ttk.Frame(self.left_frame)
        input_frame.pack(fill="x", side="bottom")

        self.input_text = tk.Text(
            input_frame,
            height=3,
            wrap=tk.WORD,
            font=("Segoe UI", 10),
        )
        self.input_text.pack(side="left", fill="both", expand=True, padx=(0, 6))

        # Bind Enter to send and Shift+Enter to newline
        self.input_text.bind("<Return>", self._on_enter)
        self.input_text.bind("<Shift-Return>", self._on_shift_enter)

        btn_box = ttk.Frame(input_frame)
        btn_box.pack(side="right", fill="y")

        self.send_btn = ttk.Button(btn_box, text="Send", command=self._on_send)
        self.send_btn.pack(fill="x", pady=(0, 2))

        is_mock = "MOCK" in self.mode_label or bool(getattr(settings, "ORBIT_MOCK", False))
        mic_text = "mock voice" if is_mock else "Mic"
        self.mic_btn = ttk.Button(btn_box, text=mic_text, command=self._on_toggle_mic)
        self.mic_btn.pack(fill="x", pady=(0, 2))
        self.mic_tooltip = ToolTip(self.mic_btn, "")

        self.stop_btn = ttk.Button(btn_box, text="Stop", command=self._on_stop)
        self.stop_btn.pack(fill="x", pady=(0, 2))
        self.stop_btn.configure(state="disabled")

        self.speak_replies_var = tk.BooleanVar(value=self._speak_replies)
        self.speak_replies_cb = ttk.Checkbutton(
            btn_box,
            text="Speak replies",
            variable=self.speak_replies_var,
            command=self._on_toggle_speak_replies,
        )
        self.speak_replies_cb.pack(fill="x")

        # Hide mic button and speak replies checkbox if VOICE_IN_UI is False
        if not getattr(settings, "VOICE_IN_UI", True):
            self.mic_btn.pack_forget()
            self.speak_replies_cb.pack_forget()

        # Keyboard shortcuts for voice
        self.input_text.bind("<Control-m>", self._on_ctrl_m)
        self.input_text.bind("<Control-M>", self._on_ctrl_m)
        self.root.bind("<Control-m>", self._on_ctrl_m)
        self.root.bind("<Control-M>", self._on_ctrl_m)

        self._update_voice_ui()

        # --- Right Pane: Notebook with Tabs ---
        self.right_frame = ttk.Frame(self.paned, padding=4)
        self.paned.add(self.right_frame, weight=2)

        self.notebook = ttk.Notebook(self.right_frame)
        self.notebook.pack(fill="both", expand=True)

        self._build_activity_tab()
        self._build_context_tab()
        self._build_memories_tab()
        self._build_documents_tab()
        self._build_reminders_tab()
        self._build_routines_tab()
        self._build_last_run_tab()

        # --- Status Bar ---
        self._build_status_bar(main_container)

    def _on_dock_activity(self) -> None:
        self._toggle_or_select_tab(self.activity_frame)

    def _on_dock_routines(self) -> None:
        self._toggle_or_select_tab(self.routines_frame)

    def _toggle_or_select_tab(self, target_frame: tk.Widget) -> None:
        if not hasattr(self, "paned") or not hasattr(self, "right_frame"):
            return
        if not self._notebook_visible:
            try:
                self.paned.add(self.right_frame, weight=2)
                self._notebook_visible = True
            except Exception:
                pass
            if hasattr(self, "notebook"):
                self.notebook.select(target_frame)
        else:
            try:
                curr = self.notebook.select()
                if str(curr) == str(target_frame):
                    self.paned.forget(self.right_frame)
                    self._notebook_visible = False
                else:
                    self.notebook.select(target_frame)
            except Exception:
                self.notebook.select(target_frame)

    def _get_dock_voice_state(self) -> tuple[str, bool, str]:
        can_use, reason = self._check_voice_hardware_and_packages()
        if not can_use:
            return ("disabled", True, reason)
        blocked = self._is_blocked()
        if blocked:
            return ("blocked", True, "Voice input unavailable while busy")
        vc_state = "idle"
        if getattr(self, "voice_controller", None) is not None:
            vc_state = getattr(self.voice_controller, "state", "idle")
        return (vc_state, False, "")

    def _build_activity_tab(self) -> None:
        self.activity_frame = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(self.activity_frame, text="Activity")

        columns = ("task", "step", "tool", "status", "info")
        self.activity_tree = ttk.Treeview(
            self.activity_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        self.tree = self.activity_tree
        self.activity_tree.heading("task", text="Task")
        self.activity_tree.heading("step", text="#")
        self.activity_tree.heading("tool", text="Tool")
        self.activity_tree.heading("status", text="Status")
        self.activity_tree.heading("info", text="Detail")

        self.activity_tree.column("task", width=45, stretch=False)
        self.activity_tree.column("step", width=35, stretch=False)
        self.activity_tree.column("tool", width=110, stretch=False)
        self.activity_tree.column("status", width=80, stretch=False)
        self.activity_tree.column("info", width=180, stretch=True)

        tree_scroll = ttk.Scrollbar(self.activity_frame, orient="vertical", command=self.activity_tree.yview)
        self.activity_tree.configure(yscrollcommand=tree_scroll.set)

        self.activity_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

    def _build_context_tab(self) -> None:
        self.context_frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.context_frame, text="Context")

        self.context_vars = {
            "site": tk.StringVar(value="—"),
            "query": tk.StringVar(value="—"),
            "app": tk.StringVar(value="—"),
            "folder": tk.StringVar(value="—"),
            "browser": tk.StringVar(value="—"),
            "last_tool": tk.StringVar(value="—"),
            "age": tk.StringVar(value="—"),
        }

        grid_frame = ttk.Frame(self.context_frame)
        grid_frame.pack(fill="x", anchor="n", pady=(0, 16))

        fields = [
            ("Site:", "site"),
            ("Query:", "query"),
            ("App:", "app"),
            ("Folder:", "folder"),
            ("Browser:", "browser"),
            ("Last tool:", "last_tool"),
            ("Age:", "age"),
        ]

        self.context_labels = {}
        for row_idx, (label_text, key) in enumerate(fields):
            lbl = ttk.Label(grid_frame, text=label_text, font=("Segoe UI", 9, "bold"), width=12)
            lbl.grid(row=row_idx, column=0, sticky="w", pady=4)
            val_lbl = ttk.Label(grid_frame, textvariable=self.context_vars[key], font=("Segoe UI", 9))
            val_lbl.grid(row=row_idx, column=1, sticky="w", pady=4)
            self.context_labels[key] = val_lbl

        btn_box = ttk.Frame(self.context_frame)
        btn_box.pack(fill="x", side="bottom")

        self.new_conv_btn = ttk.Button(
            btn_box,
            text="New conversation",
            command=self._on_new_conversation,
        )
        self.new_conv_btn.pack(side="left")

        self.copy_diag_btn = ttk.Button(
            btn_box,
            text="Copy diagnostics",
            command=self._on_copy_diagnostics,
        )
        self.copy_diag_btn.pack(side="left", padx=(8, 0))

        self.doctor_btn = ttk.Button(
            btn_box,
            text="Doctor",
            command=self._on_doctor_click,
        )
        self.doctor_btn.pack(side="left", padx=(8, 0))

        self.settings_btn = ttk.Button(
            btn_box,
            text="Settings",
            command=self._on_settings_click,
        )
        self.settings_btn.pack(side="left", padx=(8, 0))

    def _build_memories_tab(self) -> None:
        self.memories_frame = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(self.memories_frame, text="Memories")

        self.memory_tree = ttk.Treeview(
            self.memories_frame,
            columns=("id", "category", "content"),
            show="headings",
            selectmode="browse",
        )
        self.memory_tree.heading("id", text="ID")
        self.memory_tree.heading("category", text="Category")
        self.memory_tree.heading("content", text="Memory Content")

        self.memory_tree.column("id", width=40, stretch=False)
        self.memory_tree.column("category", width=90, stretch=False)
        self.memory_tree.column("content", width=250, stretch=True)

        mem_scroll = ttk.Scrollbar(self.memories_frame, orient="vertical", command=self.memory_tree.yview)
        self.memory_tree.configure(yscrollcommand=mem_scroll.set)

        self.memory_tree.pack(fill="both", expand=True, pady=(0, 6))
        mem_scroll.pack(side="right", fill="y")

        mem_btn_box = ttk.Frame(self.memories_frame)
        mem_btn_box.pack(fill="x", side="bottom")

        del_btn = ttk.Button(mem_btn_box, text="Delete", command=self._on_delete_memory)
        del_btn.pack(side="left", padx=(0, 6))

        refresh_btn = ttk.Button(mem_btn_box, text="Refresh", command=self._refresh_memories)
        refresh_btn.pack(side="left")

    def _build_documents_tab(self) -> None:
        self.docs_frame = ttk.Frame(self.notebook, padding=6)
        # Check if doc_index is available
        has_doc_index = bool(
            self.controller.app
            and getattr(self.controller.app, "doc_index", None) is not None
        )
        if has_doc_index:
            self.notebook.add(self.docs_frame, text="Documents")

        self.docs_tree = ttk.Treeview(
            self.docs_frame,
            columns=("path", "chunks", "flagged"),
            show="headings",
            selectmode="browse",
        )
        self.docs_tree.heading("path", text="Document Path")
        self.docs_tree.heading("chunks", text="Chunks")
        self.docs_tree.heading("flagged", text="Flagged")

        self.docs_tree.column("path", width=220, stretch=True)
        self.docs_tree.column("chunks", width=60, stretch=False)
        self.docs_tree.column("flagged", width=60, stretch=False)

        docs_scroll = ttk.Scrollbar(self.docs_frame, orient="vertical", command=self.docs_tree.yview)
        self.docs_tree.configure(yscrollcommand=docs_scroll.set)

        self.docs_tree.pack(fill="both", expand=True, pady=(0, 6))
        docs_scroll.pack(side="right", fill="y")

        docs_btn_box = ttk.Frame(self.docs_frame)
        docs_btn_box.pack(fill="x", side="bottom")

        idx_btn = ttk.Button(docs_btn_box, text="Index", command=self._on_index_documents)
        idx_btn.pack(side="left", padx=(0, 6))

        refresh_btn = ttk.Button(docs_btn_box, text="Refresh", command=self._refresh_documents)
        refresh_btn.pack(side="left")

    def _build_reminders_tab(self) -> None:
        self.reminders_frame = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(self.reminders_frame, text="Reminders")

        columns = ("id", "kind", "text", "due_in")
        self.reminders_tree = ttk.Treeview(
            self.reminders_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        self.reminders_tree.heading("id", text="ID")
        self.reminders_tree.heading("kind", text="Kind")
        self.reminders_tree.heading("text", text="Reminder Text")
        self.reminders_tree.heading("due_in", text="Due In")

        self.reminders_tree.column("id", width=40, stretch=False)
        self.reminders_tree.column("kind", width=70, stretch=False)
        self.reminders_tree.column("text", width=240, stretch=True)
        self.reminders_tree.column("due_in", width=90, stretch=False)

        rem_scroll = ttk.Scrollbar(self.reminders_frame, orient="vertical", command=self.reminders_tree.yview)
        self.reminders_tree.configure(yscrollcommand=rem_scroll.set)

        self.reminders_tree.pack(fill="both", expand=True, pady=(0, 6))
        rem_scroll.pack(side="right", fill="y")

        rem_btn_box = ttk.Frame(self.reminders_frame)
        rem_btn_box.pack(fill="x", side="bottom")

        cancel_btn = ttk.Button(rem_btn_box, text="Cancel", command=self._on_cancel_reminder_tab)
        cancel_btn.pack(side="left", padx=(0, 6))

        refresh_btn = ttk.Button(rem_btn_box, text="Refresh", command=self._refresh_reminders_tab)
        refresh_btn.pack(side="left")

    def _refresh_reminders_tab(self) -> None:
        if not hasattr(self, "reminders_tree"):
            return
        for item_id in self.reminders_tree.get_children():
            self.reminders_tree.delete(item_id)

        if not getattr(self, "reminder_store", None):
            return

        try:
            now_ts = self.clock() if callable(getattr(self, "clock", None)) else time.time()
            now_dt = datetime.fromtimestamp(now_ts, timezone.utc)
            items = self.reminder_store.list_active()
            for it in items:
                due_dt = datetime.fromisoformat(it.due_at.replace("Z", "+00:00"))
                diff_s = int((due_dt - now_dt).total_seconds())
                if diff_s <= 0:
                    due_in_str = "due now"
                else:
                    m, s = divmod(diff_s, 60)
                    h, m = divmod(m, 60)
                    if h > 0:
                        due_in_str = f"{h}h {m:02d}m"
                    else:
                        due_in_str = f"{m:02d}:{s:02d}"
                self.reminders_tree.insert(
                    "",
                    "end",
                    values=(it.id, it.kind, it.text, due_in_str),
                )
        except Exception as exc:
            print(f"Error refreshing reminders tab: {exc}", file=sys.stderr)

    def _on_cancel_reminder_tab(self) -> None:
        if not hasattr(self, "reminders_tree"):
            return
        selected = self.reminders_tree.selection()
        if not selected:
            return
        vals = self.reminders_tree.item(selected[0], "values")
        if not vals:
            return
        rem_id = vals[0]
        rem_text = vals[2] if len(vals) > 2 else ""
        if messagebox.askyesno("Cancel Reminder", f"Cancel reminder {rem_id}: '{rem_text}'?", parent=self.root):
            try:
                if self.reminder_store is not None:
                    self.reminder_store.cancel(int(rem_id))
                self._refresh_reminders_tab()
                self._refresh_status_bar_reminder()
            except Exception as exc:
                messagebox.showerror("Error", f"Failed to cancel reminder: {exc}", parent=self.root)

    def _build_routines_tab(self) -> None:
        self.routines_frame = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(self.routines_frame, text="Routines")

        routines_enabled = getattr(settings, "ROUTINES_ENABLED", True)
        if hasattr(self.controller, "app") and self.controller.app is not None:
            if hasattr(self.controller.app, "routines_enabled"):
                routines_enabled = bool(self.controller.app.routines_enabled)

        if not routines_enabled:
            self.routines_disabled_lbl = ttk.Label(
                self.routines_frame,
                text="disabled",
                font=("Segoe UI", 10, "italic"),
                foreground="gray",
            )
            self.routines_disabled_lbl.pack(pady=10)

        columns = ("name", "steps", "last_run", "runs")
        self.routines_tree = ttk.Treeview(
            self.routines_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        self.routines_tree.heading("name", text="Name")
        self.routines_tree.heading("steps", text="Steps")
        self.routines_tree.heading("last_run", text="Last Run")
        self.routines_tree.heading("runs", text="Runs")

        self.routines_tree.column("name", width=160, stretch=True)
        self.routines_tree.column("steps", width=60, stretch=False)
        self.routines_tree.column("last_run", width=140, stretch=False)
        self.routines_tree.column("runs", width=60, stretch=False)

        routines_scroll = ttk.Scrollbar(self.routines_frame, orient="vertical", command=self.routines_tree.yview)
        self.routines_tree.configure(yscrollcommand=routines_scroll.set)

        self.routines_tree.pack(fill="both", expand=True, pady=(0, 6))
        routines_scroll.pack(side="right", fill="y")

        routines_btn_box = ttk.Frame(self.routines_frame)
        routines_btn_box.pack(fill="x", side="bottom")

        self.routine_run_btn = ttk.Button(routines_btn_box, text="Run", command=self._on_run_routine_tab)
        self.routine_run_btn.pack(side="left", padx=(0, 6))

        self.routine_show_btn = ttk.Button(routines_btn_box, text="Show steps", command=self._on_show_routine_steps_tab)
        self.routine_show_btn.pack(side="left", padx=(0, 6))

        self.routine_delete_btn = ttk.Button(routines_btn_box, text="Delete", command=self._on_delete_routine_tab)
        self.routine_delete_btn.pack(side="left", padx=(0, 6))

        self.routine_refresh_btn = ttk.Button(routines_btn_box, text="Refresh", command=self._refresh_routines_tab)
        self.routine_refresh_btn.pack(side="left")

        if not routines_enabled:
            self.routine_run_btn.configure(state="disabled")
            self.routine_show_btn.configure(state="disabled")
            self.routine_delete_btn.configure(state="disabled")

    def _refresh_routines_tab(self) -> None:
        if not hasattr(self, "routines_tree"):
            return
        for item_id in self.routines_tree.get_children():
            self.routines_tree.delete(item_id)

        routines_enabled = getattr(settings, "ROUTINES_ENABLED", True)
        if hasattr(self.controller, "app") and self.controller.app is not None:
            if hasattr(self.controller.app, "routines_enabled"):
                routines_enabled = bool(self.controller.app.routines_enabled)

        if not routines_enabled:
            self.routines_tree.insert("", "end", values=("disabled", "", "", ""))
            return

        store = getattr(self, "routine_store", None)
        if store is None and hasattr(self.controller, "routine_store"):
            store = getattr(self.controller, "routine_store", None)
        if store is None and hasattr(self.controller, "app"):
            store = getattr(self.controller.app, "routine_store", None)

        if store is None:
            return

        try:
            items = store.list()
            for r in items:
                name = getattr(r, "display_name", None) or (r.get("display_name") if isinstance(r, dict) else "")
                steps = getattr(r, "steps", None) or (r.get("steps") if isinstance(r, dict) else [])
                step_count = len(steps) if isinstance(steps, list) else 0
                last_run = getattr(r, "last_run_at", None) or (r.get("last_run_at") if isinstance(r, dict) else "")
                last_run_str = str(last_run) if last_run else "never"
                run_count = getattr(r, "run_count", 0) if not isinstance(r, dict) else r.get("run_count", 0)

                norm_name = getattr(r, "name", None) or (r.get("name") if isinstance(r, dict) else name)
                self.routines_tree.insert(
                    "",
                    "end",
                    iid=str(norm_name),
                    values=(name, step_count, last_run_str, run_count),
                )
        except Exception as exc:
            print(f"Error refreshing routines tab: {exc}", file=sys.stderr)

    def _on_run_routine_tab(self) -> None:
        if not hasattr(self, "routines_tree"):
            return
        selected = self.routines_tree.selection()
        if not selected:
            return
        vals = self.routines_tree.item(selected[0], "values") or ()
        if not vals:
            return
        name = str(vals[0]).strip()
        if not name or name == "disabled":
            return
        try:
            self.controller.submit(f"run routine {name}", source="ui")
        except Exception as exc:
            print(f"Error running routine: {exc}", file=sys.stderr)

    def _on_show_routine_steps_tab(self) -> None:
        if not hasattr(self, "routines_tree"):
            return
        selected = self.routines_tree.selection()
        if not selected:
            return
        vals = self.routines_tree.item(selected[0], "values") or ()
        if not vals:
            return
        name = str(vals[0]).strip()
        if not name or name == "disabled":
            return
        store = getattr(self, "routine_store", None)
        if store is None and hasattr(self.controller, "routine_store"):
            store = getattr(self.controller, "routine_store", None)
        if store is None and hasattr(self.controller, "app"):
            store = getattr(self.controller.app, "routine_store", None)
        if store is None:
            return
        try:
            routine = store.get(name)
            if not routine:
                messagebox.showinfo("Routine", f"Routine '{name}' not found.", parent=self.root)
                return
            from core.chains import describe_step
            steps = getattr(routine, "steps", []) or []
            step_lines = [f"{i}. {describe_step(s.get('tool', ''), s.get('args', {}))}" for i, s in enumerate(steps, 1)]
            content = "\n".join(step_lines) if step_lines else "No steps."
            messagebox.showinfo(f"Routine: {getattr(routine, 'display_name', name)}", content, parent=self.root)
        except Exception as exc:
            messagebox.showerror("Error", f"Failed to show routine steps: {exc}", parent=self.root)

    def _on_delete_routine_tab(self) -> None:
        if not hasattr(self, "routines_tree"):
            return
        selected = self.routines_tree.selection()
        if not selected:
            return
        vals = self.routines_tree.item(selected[0], "values") or ()
        if not vals:
            return
        name = str(vals[0]).strip()
        if not name or name == "disabled":
            return
        if messagebox.askyesno("Delete Routine", f"Delete routine '{name}'?", parent=self.root):
            store = getattr(self, "routine_store", None)
            if store is None and hasattr(self.controller, "routine_store"):
                store = getattr(self.controller, "routine_store", None)
            if store is None and hasattr(self.controller, "app"):
                store = getattr(self.controller.app, "routine_store", None)
            if store is not None:
                try:
                    store.delete(name)
                    self._refresh_routines_tab()
                except Exception as exc:
                    messagebox.showerror("Error", f"Failed to delete routine: {exc}", parent=self.root)

    def _refresh_status_bar_reminder(self) -> None:
        if not hasattr(self, "reminder_status_lbl"):
            return
        if not getattr(self, "reminder_store", None):
            self.reminder_status_lbl.configure(text="")
            return
        try:
            active = self.reminder_store.list_active()
            if not active:
                self.reminder_status_lbl.configure(text="")
                return
            active.sort(key=lambda x: x.due_at)
            soonest = active[0]
            now_ts = self.clock() if callable(getattr(self, "clock", None)) else time.time()
            now_dt = datetime.fromtimestamp(now_ts, timezone.utc)
            due_dt = datetime.fromisoformat(soonest.due_at.replace("Z", "+00:00"))
            diff_s = max(0, int((due_dt - now_dt).total_seconds()))
            m = diff_s // 60
            s = diff_s % 60
            self.reminder_status_lbl.configure(text=f"Next: {soonest.text} in {m:02d}:{s:02d}")
        except Exception:
            self.reminder_status_lbl.configure(text="")

    def _build_last_run_tab(self) -> None:
        self.last_run_frame = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(self.last_run_frame, text="Last run")

        self.last_run_text = scrolledtext.ScrolledText(
            self.last_run_frame,
            wrap=tk.WORD,
            state="disabled",
            font=("Consolas", 9),
        )
        self.last_run_text.pack(fill="both", expand=True)

    def _build_status_bar(self, container: ttk.Frame) -> None:
        status_bar = ttk.Frame(container, padding=(8, 4))
        status_bar.pack(fill="x", side="bottom")
        self.status_bar = status_bar

        # Mode indicator
        is_mock = "MOCK" in self.mode_label
        mode_color = "#e67e22" if is_mock else "#27ae60"
        self.mode_lbl = tk.Label(
            status_bar,
            text=self.mode_label,
            fg=mode_color,
            font=("Segoe UI", 9, "bold"),
        )
        self.mode_lbl.pack(side="left", padx=(0, 10))

        # Demo mode chip
        from config.paths import is_demo_mode

        self.demo_chip = tk.Label(
            status_bar,
            text="DEMO",
            fg="#e67e22",
            font=("Segoe UI", 8, "bold"),
            relief="groove",
            padx=4,
            pady=1,
        )
        if is_demo_mode():
            self.demo_chip.pack(side="left", padx=(0, 10))

        # Busy indicator
        self.busy_lbl = tk.Label(
            status_bar,
            text="● Idle",
            fg="gray",
            font=("Segoe UI", 9),
        )
        self.busy_lbl.pack(side="left", padx=(0, 12))

        # One-line context status (hidden when empty)
        self.context_lbl = tk.Label(
            status_bar,
            text="",
            fg="#2c3e50",
            font=("Segoe UI", 9),
        )

        # Plan progress indicator
        self.plan_status_lbl = tk.Label(
            status_bar,
            text="",
            fg="#2980b9",
            font=("Segoe UI", 9, "bold"),
        )
        self.plan_status_lbl.pack(side="left", padx=(0, 10))

        # Reminder next indicator
        self.reminder_status_lbl = tk.Label(
            status_bar,
            text="",
            fg="#8e44ad",
            font=("Segoe UI", 9),
        )
        self.reminder_status_lbl.pack(side="left", padx=(0, 10))

        # Doctor status chip (hidden when everything is OK)
        self.doctor_chip = tk.Label(
            status_bar,
            text="",
            fg="#c0392b",
            font=("Segoe UI", 8, "bold"),
            relief="groove",
            padx=4,
            pady=1,
            cursor="hand2",
        )
        self.doctor_chip.bind("<Button-1>", lambda e: self._on_doctor_click())

        # Notice banner
        if self.notice:
            self.notice_lbl = tk.Label(
                status_bar,
                text=self.notice,
                fg="#b85d00",
                font=("Segoe UI", 9, "italic"),
            )
            self.notice_lbl.pack(side="left", fill="x", expand=True)

    # --- Polling & Event Handling (UI Thread Only) ---

    def _mark_running_steps_interrupted(self) -> None:
        """Mark any activity row still showing 'running...' as 'interrupted'."""
        for item_id in self.activity_tree.get_children():
            values = list(self.activity_tree.item(item_id, "values"))
            if len(values) >= 4 and values[3] == "running...":
                values[3] = "interrupted"
                tool_name = str(values[2]) if len(values) > 2 else ""
                if not tool_name.startswith("✗") and not tool_name.startswith("✓"):
                    values[2] = f"✗ {tool_name}".strip()
                self.activity_tree.item(item_id, values=values)

    def _poll(self) -> None:
        if self._destroyed:
            return

        try:
            now = time.monotonic()
            if now - getattr(self, "_last_speaker_poll", 0.0) >= 0.2:
                self._last_speaker_poll = now
                if getattr(self, "voice_controller", None) is not None:
                    self.voice_controller.poll_speaker()

            events = self.controller.drain(max_items=100)
            for event in events:
                try:
                    self._handle_event(event)
                except Exception as exc:
                    import traceback
                    traceback.print_exc(file=sys.stderr)
                    task_id = None
                    try:
                        event_data = getattr(event, "data", None)
                        if isinstance(event_data, dict):
                            task_id = event_data.get("task")
                    except Exception:
                        pass
                    if task_id is None:
                        task_id = self._current_view_task or 0
                    if task_id not in self._tasks_with_ui_error:
                        self._tasks_with_ui_error.add(task_id)
                        err_msg = str(exc) or exc.__class__.__name__
                        self._append_chat(f"UI error: {err_msg}\n\n", ("system",))

            # Dynamically attach Documents tab if doc_index became available
            if "Documents" not in [self.notebook.tab(i, "text") for i in self.notebook.tabs()]:
                if (
                    self.controller.app
                    and getattr(self.controller.app, "doc_index", None) is not None
                ):
                    self.notebook.add(self.docs_frame, text="Documents")
                    self._refresh_documents()
        finally:
            if hasattr(self, "hud_model") and self.hud_model is not None:
                is_conf = (
                    self._confirm_dialog is not None
                    and not getattr(self._confirm_dialog, "_resolved", False)
                ) or (
                    self._plan_dialog is not None
                    and not getattr(self._plan_dialog, "_resolved", False)
                )
                self.hud_model.set_confirming(is_conf)
                self.hud_model.set_busy(bool(self.controller.is_busy))
                vc = getattr(self, "voice_controller", None)
                is_spk = vc is not None and getattr(vc, "is_speaking", lambda: False)()
                self.hud_model.set_speaking(is_spk)
                is_lis = vc is not None and getattr(vc, "state", "idle") == "listening"
                self.hud_model.set_listening(is_lis)

            try:
                self._update_busy_ui(self.controller.is_busy)
            except Exception:
                pass
            try:
                self._update_voice_ui()
            except Exception:
                pass
            try:
                self._refresh_context_tab()
            except Exception:
                pass
            if not self._destroyed:
                try:
                    self.root.after(50, self._poll)
                except Exception:
                    pass

    def _poll_reminders(self) -> None:
        if self._destroyed:
            return
        try:
            if self.reminder_scheduler is not None:
                self.reminder_scheduler.tick()
            self._refresh_status_bar_reminder()
        except Exception as exc:
            print(f"Error in reminders poll loop: {exc}", file=sys.stderr)
        finally:
            if not self._destroyed:
                try:
                    self.root.after(1000, self._poll_reminders)
                except Exception:
                    pass

    def _is_dialog_open(self) -> bool:
        try:
            if self._confirm_dialog is not None and self._confirm_dialog.winfo_exists():
                return True
        except Exception:
            pass
        try:
            if self._plan_dialog is not None and self._plan_dialog.winfo_exists():
                return True
        except Exception:
            pass
        return False

    def _on_reminder_due(self, item: Any) -> None:
        text = getattr(item, "text", "") or ""
        # Add a chat line for every fired reminder
        self._append_chat(f"Reminder: {text}\n\n", ("orbit",))

        # Windows bell
        try:
            import winsound
            winsound.MessageBeep()
        except Exception:
            try:
                self.root.bell()
            except Exception:
                pass

        # Speak replies if enabled and not currently recording
        if getattr(self, "_speak_replies", False):
            vc = getattr(self, "voice_controller", None)
            if vc is not None:
                vc_state = getattr(vc, "state", "idle")
                if vc_state != "listening":
                    speaker = getattr(vc, "speaker", None)
                    if speaker is not None and hasattr(speaker, "speak"):
                        try:
                            speaker.speak(f"Reminder: {text}")
                        except Exception:
                            pass

        self._refresh_reminders_tab()
        self._refresh_status_bar_reminder()

        # If a dialog is open or already 3 toasts at once, do not show toast
        if self._is_dialog_open() or len(self._active_toasts) >= 3:
            return

        try:
            toast = ReminderToast(
                parent=self.root,
                item=item,
                on_dismiss=self._dismiss_reminder,
                on_snooze=self._snooze_reminder,
                on_close=self._remove_toast,
            )
            self._active_toasts.append(toast)
        except Exception as exc:
            print(f"Failed to show reminder toast: {exc}", file=sys.stderr)

    def _dismiss_reminder(self, item_id: int) -> None:
        try:
            if self.reminder_store is not None:
                self.reminder_store.mark(item_id, "dismissed")
            self._refresh_reminders_tab()
            self._refresh_status_bar_reminder()
        except Exception as exc:
            print(f"Failed to dismiss reminder {item_id}: {exc}", file=sys.stderr)

    def _snooze_reminder(self, item_id: int) -> None:
        try:
            if self.reminder_store is not None:
                self.reminder_store.snooze(item_id, minutes=5)
            self._refresh_reminders_tab()
            self._refresh_status_bar_reminder()
        except Exception as exc:
            print(f"Failed to snooze reminder {item_id}: {exc}", file=sys.stderr)

    def _remove_toast(self, toast: ReminderToast) -> None:
        try:
            if toast in self._active_toasts:
                self._active_toasts.remove(toast)
        except Exception:
            pass

    def _handle_event(self, event: UiEvent) -> None:
        kind = getattr(event, "kind", "")
        data = getattr(event, "data", {})
        if not isinstance(data, dict):
            data = {}

        if kind == "delta":
            delta_text = data.get("delta") or data.get("text", "")
            if not self._streaming:
                self._streaming = True
                self._append_chat("ORBIT: ", ("orbit_prefix",))
            self._append_chat(delta_text, ("orbit",))

        elif kind == "reply":
            full_text = data.get("text") or data.get("reply", "")
            streamed = data.get("streamed", False)
            if self._current_task_had_denial or "user denied permission" in str(full_text).lower():
                full_text = "Okay, I didn't do that because you didn't approve it."
            if not streamed and not self._streaming:
                self._append_chat("ORBIT: ", ("orbit_prefix",))
                self._append_chat(f"{full_text}\n\n", ("orbit",))
            else:
                self._append_chat("\n\n", ("orbit",))
            self._streaming = False
            if getattr(self, "_speak_replies", False) and getattr(self, "voice_controller", None) is not None:
                self.voice_controller.speak_reply(full_text)

        elif kind == "voice_state":
            st = data.get("state", "idle")
            msg = data.get("message", "")
            if st == "listening":
                self.voice_status_lbl.configure(text="Listening... click to stop")
                if not self.voice_bar.winfo_ismapped():
                    self.voice_bar.pack(fill="x", side="bottom", padx=4, pady=(0, 2), before=self.input_text.master)
                if not self.level_bar.winfo_ismapped():
                    self.level_bar.pack(side="left", padx=4)
                    self.level_bar["value"] = 0
            elif st == "transcribing":
                self.voice_status_lbl.configure(text="Transcribing...")
                if not self.voice_bar.winfo_ismapped():
                    self.voice_bar.pack(fill="x", side="bottom", padx=4, pady=(0, 2), before=self.input_text.master)
                if self.level_bar.winfo_ismapped():
                    self.level_bar.pack_forget()
            elif st == "speaking":
                self.voice_status_lbl.configure(text="Speaking... (Stop to silence)")
                if not self.voice_bar.winfo_ismapped():
                    self.voice_bar.pack(fill="x", side="bottom", padx=4, pady=(0, 2), before=self.input_text.master)
                if self.level_bar.winfo_ismapped():
                    self.level_bar.pack_forget()
            elif st == "error":
                err_text = data.get("error", msg or "Voice error")
                self.voice_status_lbl.configure(text=f"Voice: {err_text}")
                if not self.voice_bar.winfo_ismapped():
                    self.voice_bar.pack(fill="x", side="bottom", padx=4, pady=(0, 2), before=self.input_text.master)
                if self.level_bar.winfo_ismapped():
                    self.level_bar.pack_forget()
            else:  # idle
                self.voice_status_lbl.configure(text="")
                if self.level_bar.winfo_ismapped():
                    self.level_bar.pack_forget()
                if self.voice_bar.winfo_ismapped():
                    self.voice_bar.pack_forget()
            self._update_voice_ui()

            if hasattr(self, "hud_model") and self.hud_model is not None:

                self.hud_model.set_listening(st == "listening")

                self.hud_model.set_speaking(st == "speaking")

                if st == "error":

                    self.hud_model.flash_error()

            if hasattr(self, "dock_canvas") and self.dock_canvas is not None:

                self.dock_canvas.redraw()

        elif kind == "voice_level":
            try:
                lvl = float(data.get("level", 0.0) or 0.0)
            except (ValueError, TypeError):
                lvl = 0.0
            if hasattr(self, "level_bar"):
                self.level_bar["value"] = max(0, min(100, int(lvl * 100)))
            if hasattr(self, "hud_model") and self.hud_model is not None:
                self.hud_model.set_mic_level(lvl)
            if hasattr(self, "dock_canvas") and self.dock_canvas is not None:
                self.dock_canvas.set_mic_level(lvl)

        elif kind == "voice_calibration_warning":
            warn_text = data.get("text") or "Noisy room: use a headset or move closer"
            self._append_chat(f"Voice: {warn_text}\n\n", ("system",))
            if hasattr(self, "status_lbl"):
                self.status_lbl.configure(text=f"Voice: {warn_text}")

        elif kind == "voice_review_needed":
            text = (data.get("text") or "").strip()
            reason = data.get("reason", "Low confidence")
            noise_st = data.get("noise_status", "")
            understood = data.get("understood_as")

            self.input_text.delete("1.0", tk.END)
            self.input_text.insert("1.0", text)
            self.input_text.mark_set("insert", tk.END)
            self.input_text.focus_set()
            self._pending_voice_reviewed = True

            reason_msg = f"Voice Review: {reason}"
            if noise_st:
                reason_msg += f" ({noise_st})"
            if understood:
                reason_msg += f" | Understood as: {understood}"
            self._append_chat(f"{reason_msg}\n\n", ("system",))
            if hasattr(self, "status_lbl"):
                self.status_lbl.configure(text=reason_msg)

        elif kind == "voice_transcript":
            text = (data.get("text") or "").strip()
            noise_st = data.get("noise_status", "")
            understood = data.get("understood_as")
            if text:
                heard_str = f"Heard: {text}"
                if noise_st:
                    heard_str += f" ({noise_st})"
                if understood:
                    heard_str += f" | Understood as: {understood}"
                self._append_chat(f"{heard_str}\n", ("system",))
                self._append_chat(f"User: {text}\n", ("user",))
                self.controller.submit(text, source="voice")

        elif kind == "voice_error":
            err_text = data.get("text") or data.get("error") or "Voice error"
            self._append_chat(f"Voice: {err_text}\n\n", ("system",))
            if hasattr(self, "hud_model") and self.hud_model is not None:
                self.hud_model.flash_error()

        elif kind == "error":
            self._mark_running_steps_interrupted()
            msg = data.get("message") or data.get("error", "An error occurred.")
            self._append_chat(f"Error: {msg}\n\n", ("error",))
            if hasattr(self, "hud_model") and self.hud_model is not None:
                self.hud_model.flash_error()
            self._streaming = False
            self._current_task_had_denial = False
            if data.get("task") is None:
                self._view_task_counter += 1

        elif kind == "busy":
            pass

        elif kind == "status":
            text = data.get("text", "")
            if hasattr(self, "plan_status_lbl"):
                self.plan_status_lbl.configure(text=text)
            self.busy_lbl.configure(text=text)

        elif kind == "task_done":
            self._mark_running_steps_interrupted()
            self._refresh_last_run()
            self._current_task_had_denial = False
            if data.get("task") is None:
                self._view_task_counter += 1

        elif kind == "step_start":
            idx = data.get("index", data.get("idx", ""))
            tool = data.get("tool", "")
            task = data.get("task")
            if task is None:
                if self._view_task_counter == 0:
                    self._view_task_counter = 1
                task = self._view_task_counter
            self._current_view_task = task

            if self._last_activity_task is not None and task != self._last_activity_task:
                sep_id = f"sep_t{task}"
                if not self.activity_tree.exists(sep_id):
                    self.activity_tree.insert("", "end", iid=sep_id, values=("—", "—", "—", "—", "—"))
            self._last_activity_task = task

            item_id = f"t{task}_s{idx}"
            if self.activity_tree.exists(item_id):
                self.activity_tree.item(item_id, values=(task, idx, tool, "running...", ""))
            else:
                self.activity_tree.insert("", "end", iid=item_id, values=(task, idx, tool, "running...", ""))
            self.activity_tree.yview_moveto(1.0)

        elif kind == "step_end":
            idx = data.get("index", data.get("idx", ""))
            tool = data.get("tool", "")
            status = data.get("status", "")
            detail = data.get("detail", data.get("result", ""))
            result_str = str(detail)[:60]
            symbol = "✓" if status == "success" else "✗"
            display_tool = f"{symbol} {tool}".strip()

            task = data.get("task")
            if task is None:
                if self._view_task_counter == 0:
                    self._view_task_counter = 1
                task = self._view_task_counter
            self._current_view_task = task

            if self._last_activity_task is not None and task != self._last_activity_task:
                sep_id = f"sep_t{task}"
                if not self.activity_tree.exists(sep_id):
                    self.activity_tree.insert("", "end", iid=sep_id, values=("—", "—", "—", "—", "—"))
            self._last_activity_task = task

            if status == "denied" or "user denied permission" in str(detail).lower():
                self._current_task_had_denial = True

            item_id = f"t{task}_s{idx}"
            if self.activity_tree.exists(item_id):
                self.activity_tree.item(item_id, values=(task, idx, display_tool, status, result_str))
            else:
                self.activity_tree.insert("", "end", iid=item_id, values=(task, idx, display_tool, status, result_str))
            self.activity_tree.yview_moveto(1.0)

        elif kind == "auto_retrieval":
            injected = int(data.get("injected", 0) or 0)
            if injected > 0:
                task = data.get("task")
                if task is None:
                    task = self._current_view_task or ""
                self.activity_tree.insert(
                    "",
                    "end",
                    values=(task, "", "documents", "retrieved", f"Searched your documents: {injected} passages"),
                )
                self.activity_tree.yview_moveto(1.0)

        elif kind == "confirm_request":
            if getattr(self, "voice_controller", None) is not None:
                self.voice_controller.cancel()
            req = data.get("request")
            if req is not None:
                try:
                    self.root.lift()
                except Exception:
                    pass
                try:
                    self.root.bell()
                except Exception:
                    pass
                self._confirm_dialog = ConfirmDialog(self.root, self.controller, req)
                try:
                    self._confirm_dialog.attributes("-topmost", True)
                    self._confirm_dialog.lift()
                    self._confirm_dialog.focus_force()
                except Exception:
                    pass

        elif kind == "plan_preview":
            if getattr(self, "voice_controller", None) is not None:
                self.voice_controller.cancel()
            plan_id = data.get("id")
            labels = data.get("labels", [])
            permissions = data.get("permissions", [])
            timeout_s = data.get("timeout_s", 60.0)
            dialog_kind = data.get("kind", "plan")
            dialog_name = data.get("name")
            try:
                self.root.lift()
            except Exception:
                pass
            try:
                self.root.bell()
            except Exception:
                pass
            self._plan_dialog = PlanDialog(
                self.root,
                self.controller,
                plan_id=plan_id,
                labels=labels,
                permissions=permissions,
                timeout_s=timeout_s,
                kind=dialog_kind,
                name=dialog_name,
            )
            try:
                self._plan_dialog.attributes("-topmost", True)
                self._plan_dialog.lift()
                self._plan_dialog.focus_force()
            except Exception:
                pass

        elif kind == "plan_start":
            try:
                plan_id = data.get("id", "")
                raw_steps = data.get("steps") or data.get("labels") or []
                total = len(raw_steps) if raw_steps else int(data.get("total", 0) or 0)
                self._plan_total_steps = total
                task = data.get("task", data.get("task_id"))
                if task is None:
                    if self._view_task_counter == 0:
                        self._view_task_counter = 1
                    task = self._view_task_counter
                self._current_view_task = task

                if self._last_activity_task is not None and task != self._last_activity_task:
                    sep_id = f"sep_t{task}"
                    if not self.activity_tree.exists(sep_id):
                        self.activity_tree.insert("", "end", iid=sep_id, values=("—", "—", "—", "—", "—"))
                self._last_activity_task = task

                header_text = f"Plan {plan_id}: {total} steps" if plan_id else f"Plan: {total} steps"
                header_id = f"t{task}_plan_{plan_id}" if plan_id else f"t{task}_plan_hdr"
                if self.activity_tree.exists(header_id):
                    self.activity_tree.item(header_id, values=(task, "—", header_text, "running...", ""))
                else:
                    self.activity_tree.insert("", "end", iid=header_id, values=(task, "—", header_text, "running...", ""))

                for s_idx, s in enumerate(raw_steps):
                    s_label = s.get("label", s.get("tool", "")) if isinstance(s, dict) else str(s)
                    item_id = f"t{task}_s{s_idx}"
                    if not self.activity_tree.exists(item_id):
                        self.activity_tree.insert("", "end", iid=item_id, values=(task, s_idx, s_label, "pending", ""))
                self.activity_tree.yview_moveto(1.0)
            except Exception:
                pass

        elif kind == "plan_step":
            try:
                idx_val = data.get("step", data.get("index", data.get("idx", 0)))
                try:
                    idx = int(idx_val)
                except Exception:
                    idx = 0
                label = data.get("label", data.get("tool", ""))
                status = data.get("status", "")
                task = data.get("task", data.get("task_id"))
                if task is None:
                    if self._view_task_counter == 0:
                        self._view_task_counter = 1
                    task = self._view_task_counter
                self._current_view_task = task

                if self._last_activity_task is not None and task != self._last_activity_task:
                    sep_id = f"sep_t{task}"
                    if not self.activity_tree.exists(sep_id):
                        self.activity_tree.insert("", "end", iid=sep_id, values=("—", "—", "—", "—", "—"))
                self._last_activity_task = task

                symbol = "✓" if status in ("success", "done") else ("✗" if status and status not in ("running", "pending") else "")
                display_tool = f"{symbol} {label}".strip() if symbol else str(label)

                item_id = f"t{task}_s{idx}"
                if self.activity_tree.exists(item_id):
                    self.activity_tree.item(item_id, values=(task, idx, display_tool, status, ""))
                else:
                    self.activity_tree.insert("", "end", iid=item_id, values=(task, idx, display_tool, status, ""))
                self.activity_tree.yview_moveto(1.0)

                total = self._plan_total_steps or int(data.get("total", 0) or idx + 1)
                k = idx + 1 if (total and idx < total) else idx
                status_text = f"Running plan: step {k} of {total}"
                if hasattr(self, "plan_status_lbl"):
                    self.plan_status_lbl.configure(text=status_text)
                self.busy_lbl.configure(text=f"● {status_text}")
            except Exception:
                pass

        elif kind == "plan_end":
            try:
                outcome = data.get("outcome", data.get("status", "")) if isinstance(data, dict) else ""
                task = data.get("task", data.get("task_id", self._current_view_task)) if isinstance(data, dict) else self._current_view_task
                if task is not None:
                    for item_id in self.activity_tree.get_children():
                        if item_id.startswith(f"t{task}_plan"):
                            vals = list(self.activity_tree.item(item_id, "values"))
                            if len(vals) >= 4:
                                vals[3] = outcome or "completed"
                                self.activity_tree.item(item_id, values=vals)
                if hasattr(self, "plan_status_lbl"):
                    self.plan_status_lbl.configure(text="")
                self._plan_total_steps = 0
            except Exception:
                pass

        elif kind == "doctor_fast_done":
            try:
                fails = int(data.get("fails", 0))
                warns = int(data.get("warns", 0))
                if hasattr(self, "doctor_chip"):
                    if fails > 0 or warns > 0:
                        parts = []
                        if fails > 0:
                            parts.append(f"{fails} fail" if fails == 1 else f"{fails} fails")
                        if warns > 0:
                            parts.append(f"{warns} warn" if warns == 1 else f"{warns} warns")
                        self.doctor_chip.configure(
                            text=f"Doctor: {', '.join(parts)}",
                            fg="#c0392b" if fails > 0 else "#d35400",
                        )
                        if self.doctor_chip.winfo_manager() != "pack":
                            self.doctor_chip.pack(side="left", padx=(0, 10))
                    else:
                        if self.doctor_chip.winfo_manager() == "pack":
                            self.doctor_chip.pack_forget()
            except Exception:
                pass

        elif kind == "doctor_report_ready":
            try:
                report = str(data.get("report", "No report available."))
                DoctorWindow(self.root, report)
            except Exception as exc:
                print(f"Error opening doctor window: {exc}", file=sys.stderr)

    def _update_busy_ui(self, is_busy: bool) -> None:
        vc = getattr(self, "voice_controller", None)
        voice_active = vc is not None and (vc.state in ("listening", "transcribing") or vc.is_speaking())
        if is_busy or voice_active:
            if not self._plan_total_steps and not voice_active:
                self.busy_lbl.configure(text="● Busy", fg="#1976d2")
            if is_busy:
                self.send_btn.configure(state="disabled")
                self.input_text.configure(state="disabled")
            self.stop_btn.configure(state="normal")
        else:
            if not self._plan_total_steps:
                self.busy_lbl.configure(text="● Idle", fg="gray")
                if hasattr(self, "plan_status_lbl"):
                    self.plan_status_lbl.configure(text="")
            self.send_btn.configure(state="normal")
            self.input_text.configure(state="normal")
            self.stop_btn.configure(state="disabled")
        self._update_voice_ui()

    def _append_chat(self, text: str, tags: tuple) -> None:
        self.history.configure(state="normal")
        self.history.insert(tk.END, text, tags)
        self.history.see(tk.END)
        self.history.configure(state="disabled")

    # --- User Actions ---

    def _on_enter(self, event: tk.Event) -> str:
        self._on_send()
        return "break"

    def _on_shift_enter(self, event: tk.Event) -> None:
        # Default behavior: insert newline
        return None

    def _on_send(self) -> None:
        text = self.input_text.get("1.0", tk.END).strip()
        if not text:
            return

        source = "voice-reviewed" if getattr(self, "_pending_voice_reviewed", False) else "ui"
        self._pending_voice_reviewed = False

        self._append_chat(f"User: {text}\n", ("user",))
        self.input_text.delete("1.0", tk.END)

        try:
            submitted = self.controller.submit(text, source=source)
        except TypeError:
            submitted = self.controller.submit(text)
        if not submitted:
            pending_tool = getattr(self.controller, "pending_tool", None)
            if not pending_tool and self._confirm_dialog and self._confirm_dialog.winfo_exists():
                pending_tool = getattr(getattr(self._confirm_dialog, "request", None), "tool", None)
            if not pending_tool and self._plan_dialog and self._plan_dialog.winfo_exists():
                pending_tool = "plan"

            if pending_tool:
                self._append_chat(
                    f"System: ORBIT is still working on your previous request; it is waiting for your approval. Waiting for your approval of {pending_tool}.\n\n",
                    ("system",),
                )
                if self._confirm_dialog and self._confirm_dialog.winfo_exists():
                    try:
                        self._confirm_dialog.attributes("-topmost", True)
                        self._confirm_dialog.lift()
                        self._confirm_dialog.focus_force()
                    except Exception:
                        pass
                if self._plan_dialog and self._plan_dialog.winfo_exists():
                    try:
                        self._plan_dialog.attributes("-topmost", True)
                        self._plan_dialog.lift()
                        self._plan_dialog.focus_force()
                    except Exception:
                        pass
            else:
                self._append_chat(
                    "System: ORBIT is still working on your previous request.\n\n",
                    ("system",),
                )

    def _on_stop(self) -> None:
        if getattr(self, "voice_controller", None) is not None:
            self.voice_controller.cancel()
        self.controller.cancel()

    # --- Tab Handlers ---

    def _refresh_memories(self) -> None:
        for item in self.memory_tree.get_children():
            self.memory_tree.delete(item)

        app = getattr(self.controller, "app", None)
        store = getattr(app, "memory_store", None)
        if store is not None and hasattr(store, "list_all"):
            try:
                seen_ids: set[str] = set()
                for mem in store.list_all():
                    mem_id = getattr(mem, "id", None)
                    iid = f"mem_{mem_id}" if mem_id is not None else None
                    if iid and (iid in seen_ids or self.memory_tree.exists(iid)):
                        iid = f"{iid}_{len(seen_ids)}"
                    if iid:
                        seen_ids.add(iid)
                        self.memory_tree.insert(
                            "",
                            "end",
                            iid=iid,
                            values=(mem.id, mem.category, mem.content),
                        )
                    else:
                        self.memory_tree.insert(
                            "",
                            "end",
                            values=(mem.id, mem.category, mem.content),
                        )
            except Exception:
                pass

    def _on_delete_memory(self) -> None:
        selected = self.memory_tree.selection()
        if not selected:
            return

        sel_iid = selected[0]
        vals = self.memory_tree.item(sel_iid, "values")
        mem_id = vals[0] if vals else sel_iid.replace("mem_", "")

        confirmed = messagebox.askyesno(
            "Delete Memory",
            f"Are you sure you want to delete memory #{mem_id}?",
            parent=self.root,
        )
        if not confirmed:
            return

        app = getattr(self.controller, "app", None)
        store = getattr(app, "memory_store", None)
        if store is not None and hasattr(store, "delete"):
            try:
                store.delete(int(mem_id))
            except Exception as exc:
                messagebox.showerror("Error", f"Failed to delete memory: {exc}", parent=self.root)
            self._refresh_memories()

    def _check_index_migrated(self) -> None:
        if self._migration_notified:
            return
        app = getattr(self.controller, "app", None)
        index = getattr(app, "doc_index", None) if app else None
        if index and getattr(index, "migrated", False):
            self._migration_notified = True
            print("Document index was rebuilt; run /index again.")
            self._append_chat("System: Document index was rebuilt; run /index again.\n\n", ("system",))

    def _refresh_documents(self) -> None:
        self._check_index_migrated()
        for item in self.docs_tree.get_children():
            self.docs_tree.delete(item)

        app = getattr(self.controller, "app", None)
        index = getattr(app, "doc_index", None)
        if index is not None and hasattr(index, "list_documents"):
            try:
                docs = index.list_documents()
                for i, doc in enumerate(docs):
                    path, chunks, flagged, _ = doc
                    self.docs_tree.insert(
                        "",
                        "end",
                        iid=f"doc_{i}",
                        values=(path, chunks, flagged),
                    )
            except Exception:
                pass

    def _on_index_documents(self) -> None:
        confirmed = messagebox.askyesno(
            "Index Documents",
            "Re-index all documents in the workspace docs folder now?",
            parent=self.root,
        )
        if not confirmed:
            return

        def _worker() -> None:
            try:
                app = getattr(self.controller, "app", None)
                registry = getattr(app, "registry", None)
                if registry and registry.has("index_documents"):
                    summary = registry.execute("index_documents", {})
                else:
                    summary = "Tool 'index_documents' not found in registry."
            except Exception as exc:
                summary = f"Indexing failed: {exc}"

            def _on_done() -> None:
                messagebox.showinfo("Index Complete", str(summary), parent=self.root)
                self._refresh_documents()

            if not self._destroyed:
                self.root.after(0, _on_done)

        threading.Thread(target=_worker, daemon=True, name="DocIndexerThread").start()

    def _refresh_last_run(self) -> None:
        summary = "No runs executed yet."
        app = getattr(self.controller, "app", None)
        agent = getattr(app, "agent", None)
        if agent is not None and getattr(agent, "last_run", None) is not None:
            last_run = agent.last_run
            if hasattr(last_run, "summary") and callable(last_run.summary):
                summary = last_run.summary() or "No steps recorded."

        self.last_run_text.configure(state="normal")
        self.last_run_text.delete("1.0", tk.END)
        self.last_run_text.insert("1.0", summary)
        self.last_run_text.configure(state="disabled")

    def _on_new_conversation(self) -> None:
        confirmed = messagebox.askyesno(
            "New Conversation",
            "Clear conversation history and session state to start a new conversation?",
            parent=self.root,
        )
        if not confirmed:
            return

        self.history.configure(state="normal")
        self.history.delete("1.0", tk.END)
        self.history.configure(state="disabled")

        if hasattr(self.controller, "reset"):
            self.controller.reset()
        else:
            app = getattr(self.controller, "app", None)
            agent = getattr(app, "agent", None) if app else None
            if agent is not None:
                if hasattr(agent, "reset"):
                    agent.reset()
                elif hasattr(agent, "session_state") and agent.session_state is not None:
                    agent.session_state.clear()

        self._refresh_context_tab()

    def _on_copy_diagnostics(self) -> None:
        try:
            from core.diagnostics import generate_diagnostics

            raw_text = self.history.get("1.0", tk.END).strip()
            chat_lines = [l.strip() for l in raw_text.splitlines() if l.strip()]

            activity_events = []
            for item_id in self.activity_tree.get_children():
                vals = self.activity_tree.item(item_id, "values")
                if len(vals) >= 5 and vals[0] != "—":
                    activity_events.append({
                        "task": vals[0],
                        "step": vals[1],
                        "tool": vals[2],
                        "status": vals[3],
                        "detail": vals[4],
                    })

            app = getattr(self.controller, "app", None)
            agent = getattr(app, "agent", None) if app else None

            file_path, diag_text = generate_diagnostics(
                app=app,
                agent=agent,
                chat_controller=self.controller,
                voice_controller=getattr(self, "voice_controller", None),
                provider_label=self.mode_label,
                recent_chat_lines=chat_lines,
                activity_events=activity_events,
            )

            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(diag_text)
            except Exception:
                pass

            messagebox.showinfo(
                "Diagnostics Saved",
                f"Diagnostics written to:\n{file_path}\n\nCopied to clipboard.\nThis file is safe to share after a quick look.",
                parent=self.root,
            )
        except Exception as exc:
            messagebox.showerror(
                "Diagnostics Error",
                f"Failed to generate diagnostics: {exc}",
                parent=self.root,
            )

    def _start_doctor_startup_check(self) -> None:
        def _worker():
            try:
                from core.doctor import run_checks
                probes = getattr(self.controller, "doctor_probes", None)
                checks = run_checks(probes=probes, fast=True)
                fails = sum(1 for c in checks if getattr(c, "status", "").upper() == "FAIL")
                warns = sum(1 for c in checks if getattr(c, "status", "").upper() == "WARN")
                oks = sum(1 for c in checks if getattr(c, "status", "").upper() == "OK")
                emit_fn = getattr(self.controller, "_emit", None) or getattr(self.controller, "emit", None)
                if emit_fn:
                    emit_fn("doctor_fast_done", {"fails": fails, "warns": warns, "oks": oks})
            except Exception:
                pass

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def _on_doctor_click(self) -> None:
        def _worker():
            try:
                from core.doctor import format_report, run_checks
                probes = getattr(self.controller, "doctor_probes", None)
                checks = run_checks(probes=probes, fast=False)
                report_text = format_report(checks)
                emit_fn = getattr(self.controller, "_emit", None) or getattr(self.controller, "emit", None)
                if emit_fn:
                    emit_fn("doctor_report_ready", {"report": report_text})
            except Exception as exc:
                emit_fn = getattr(self.controller, "_emit", None) or getattr(self.controller, "emit", None)
                if emit_fn:
                    emit_fn("doctor_report_ready", {"report": f"Error running doctor checks: {exc}"})

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def _on_settings_click(self) -> None:
        try:
            from ui.settings_window import SettingsWindow

            SettingsWindow(parent=self.root, controller=self.controller)
        except Exception as exc:
            messagebox.showerror("Settings Error", f"Failed to open settings: {exc}", parent=self.root)

    def _refresh_context_tab(self) -> None:
        app = getattr(self.controller, "app", None)
        agent = getattr(app, "agent", None) if app else None
        state = getattr(agent, "session_state", None) if agent else None

        if state is None or (hasattr(state, "is_empty") and state.is_empty()):
            site = "—"
            query = "—"
            app_name = "—"
            folder = "—"
            last_tool = "—"
            age = "—"
            status_text = ""
        else:
            site = getattr(state, "last_site", None) or "—"
            query = getattr(state, "last_query", None) or "—"
            app_name = getattr(state, "last_app", None) or "—"
            folder = getattr(state, "last_folder", None) or "—"
            last_tool = getattr(state, "last_tool", None) or "—"
            age_s = getattr(state, "age_seconds", None)
            if age_s is not None and getattr(state, "updated_at", 0) > 0:
                age = f"{int(age_s)}s"
            else:
                age = "—"

            parts = []
            if getattr(state, "last_site", None):
                parts.append(str(state.last_site))
            if getattr(state, "last_query", None):
                parts.append(f"'{state.last_query}'")
            if getattr(state, "last_app", None):
                parts.append(str(state.last_app))
            if getattr(state, "last_folder", None):
                parts.append(str(state.last_folder))
            status_text = f"Context: {' · '.join(parts)}" if parts else ""

        browser_ctrl = getattr(app, "browser_controller", None) if app else None
        if browser_ctrl is None and agent:
            browser_ctrl = getattr(agent, "browser_controller", None)

        browser_text = "—"
        if browser_ctrl is not None:
            try:
                b_stat = browser_ctrl.status()
                if b_stat and b_stat.get("alive"):
                    from urllib.parse import urlparse
                    raw_url = b_stat.get("url", "")
                    parsed = urlparse(raw_url)
                    host = parsed.netloc or raw_url
                    title = b_stat.get("title", "")
                    browser_text = f"{host} | {title}" if (host or title) else "open"
            except Exception:
                pass

        if hasattr(self, "context_vars"):
            self.context_vars["site"].set(site)
            self.context_vars["query"].set(query)
            self.context_vars["app"].set(app_name)
            self.context_vars["folder"].set(folder)
            if "browser" in self.context_vars:
                self.context_vars["browser"].set(browser_text)
            self.context_vars["last_tool"].set(last_tool)
            self.context_vars["age"].set(age)

        if hasattr(self, "context_lbl"):
            if status_text:
                self.context_lbl.configure(text=status_text)
                if not self.context_lbl.winfo_ismapped():
                    if hasattr(self, "notice_lbl") and self.notice_lbl.winfo_ismapped():
                        self.context_lbl.pack(side="left", padx=(0, 10), before=self.notice_lbl)
                    else:
                        self.context_lbl.pack(side="left", padx=(0, 10))
            else:
                self.context_lbl.pack_forget()

    # --- Destruction Protocols ---

    def _on_close(self) -> None:
        for toast in list(getattr(self, "_active_toasts", [])):
            try:
                toast.destroy()
            except Exception:
                pass
        if hasattr(self, "_active_toasts"):
            self._active_toasts.clear()
        if not self._shutdown_called:
            self._shutdown_called = True
            try:
                if getattr(self, "voice_controller", None) is not None:
                    self.voice_controller.shutdown()
                self.controller.shutdown()
            finally:
                self._destroyed = True
                self.root.destroy()

    def _on_root_destroyed(self, event: Optional[tk.Event] = None) -> None:
        for toast in list(getattr(self, "_active_toasts", [])):
            try:
                toast.destroy()
            except Exception:
                pass
        if hasattr(self, "_active_toasts"):
            self._active_toasts.clear()
        if getattr(event, "widget", None) == self.root:
            if not self._shutdown_called:
                self._shutdown_called = True
                try:
                    if getattr(self, "voice_controller", None) is not None:
                        self.voice_controller.shutdown()
                except Exception:
                    pass
                self.controller.shutdown()
