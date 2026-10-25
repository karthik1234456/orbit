"""Graphical user interface for ORBIT assistant built with Tkinter and TTK.

This module provides the primary window layout, streaming conversation view,
multi-tab activity/memory/docs inspector, and secure modal confirmation dialog.
Worker threads communicate strictly via ChatController; all widget updates
execute exclusively on the Tkinter main thread.
"""

import json
import sys
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import Any, Optional

from config import settings
from core.version import VERSION
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

        self.args_text = tk.Text(frame, height=6, width=55, wrap=tk.WORD, font=("Consolas", 9))
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
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.plan_id = plan_id
        self.labels = list(labels or [])
        self.permissions = list(permissions or [])
        self._resolved = False
        self._countdown_timer: Optional[str] = None
        self._remaining_s = float(timeout_s)

        self.title("Plan Preview Required")
        self.transient(parent)
        self.resizable(False, False)
        try:
            self.attributes("-topmost", True)
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

        header_lbl = ttk.Label(
            frame,
            text=f"ORBIT requests permission to run a plan ({len(self.labels)} steps):",
            font=("Segoe UI", 11, "bold"),
        )
        header_lbl.pack(anchor="w", pady=(0, 8))

        steps_lines = []
        for idx, lbl in enumerate(self.labels, 1):
            perm = self.permissions[idx - 1] if idx - 1 < len(self.permissions) else "SAFE"
            note = " (asks for approval)" if perm in ("asks for approval", "confirm", True) else ""
            steps_lines.append(f"{idx}. {lbl}{note}")

        steps_lines.append("")
        steps_lines.append("Each step that opens apps or sites may still ask for its own approval")

        steps_text_content = "\n".join(steps_lines)

        self.steps_text = tk.Text(
            frame,
            height=min(8, max(3, len(self.labels) + 2)),
            width=55,
            wrap=tk.WORD,
            font=("Consolas", 9),
        )
        self.steps_text.pack(fill="both", expand=True, pady=(0, 6))
        self.steps_text.insert("1.0", steps_text_content)
        self.steps_text.configure(state="disabled")

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

        self.run_btn = ttk.Button(
            btn_bar,
            text="Run",
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


class OrbitWindow:
    """Main application window for ORBIT desktop assistant."""

    def __init__(
        self,
        root: tk.Tk,
        controller: ChatController,
        mode_label: str,
        notice: Optional[str] = None,
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

        self.root.title(f"ORBIT v{VERSION} Desktop Assistant")
        self.root.geometry("1020x680")
        self.root.minsize(800, 500)

        # Wire shutdown protocols
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Destroy>", self._on_root_destroyed)

        # Build UI layout
        self._build_layout()

        # Migration notice flag
        self._migration_notified = False

        # Populate initial tab contents
        self._refresh_memories()
        self._refresh_documents()
        self._refresh_last_run()
        self._refresh_context_tab()
        self._check_index_migrated()

        # Start timer polling for controller events (UI thread only)
        self.root.after(50, self._poll)

    def _build_layout(self) -> None:
        # Main vertical splitter: content area + bottom status bar
        main_container = ttk.Frame(self.root)
        main_container.pack(fill="both", expand=True)

        # Horizontal paned layout: Left (Chat + Input) | Right (Tabs)
        paned = ttk.PanedWindow(main_container, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        # --- Left Pane: Chat history & input ---
        left_frame = ttk.Frame(paned, padding=4)
        paned.add(left_frame, weight=3)

        # Read-only ScrolledText for chat history
        self.history = scrolledtext.ScrolledText(
            left_frame,
            wrap=tk.WORD,
            state="disabled",
            font=("Segoe UI", 10),
        )
        self.history.pack(fill="both", expand=True, pady=(0, 8))

        # Color and style tags
        self.history.tag_configure("user", foreground="#0d47a1", font=("Segoe UI", 10, "bold"))
        self.history.tag_configure("orbit_prefix", foreground="#1b5e20", font=("Segoe UI", 10, "bold"))
        self.history.tag_configure("orbit", foreground="#111111", font=("Segoe UI", 10))
        self.history.tag_configure("system", foreground="#616161", font=("Segoe UI", 9, "italic"))
        self.history.tag_configure("error", foreground="#b71c1c", font=("Segoe UI", 10, "bold"))

        # Input frame
        input_frame = ttk.Frame(left_frame)
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
        self.send_btn.pack(fill="x", pady=(0, 4))

        self.stop_btn = ttk.Button(btn_box, text="Stop", command=self._on_stop)
        self.stop_btn.pack(fill="x")
        self.stop_btn.configure(state="disabled")

        # --- Right Pane: Notebook with Tabs ---
        right_frame = ttk.Frame(paned, padding=4)
        paned.add(right_frame, weight=2)

        self.notebook = ttk.Notebook(right_frame)
        self.notebook.pack(fill="both", expand=True)

        self._build_activity_tab()
        self._build_context_tab()
        self._build_memories_tab()
        self._build_documents_tab()
        self._build_last_run_tab()

        # --- Status Bar ---
        self._build_status_bar(main_container)

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
            try:
                self._update_busy_ui(self.controller.is_busy)
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

        elif kind == "error":
            self._mark_running_steps_interrupted()
            msg = data.get("message") or data.get("error", "An error occurred.")
            self._append_chat(f"Error: {msg}\n\n", ("error",))
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
            plan_id = data.get("id")
            labels = data.get("labels", [])
            permissions = data.get("permissions", [])
            timeout_s = data.get("timeout_s", 60.0)
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

    def _update_busy_ui(self, is_busy: bool) -> None:
        if is_busy:
            if not self._plan_total_steps:
                self.busy_lbl.configure(text="● Busy", fg="#1976d2")
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

        self._append_chat(f"User: {text}\n", ("user",))
        self.input_text.delete("1.0", tk.END)

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
        if not self._shutdown_called:
            self._shutdown_called = True
            try:
                self.controller.shutdown()
            finally:
                self._destroyed = True
                self.root.destroy()

    def _on_root_destroyed(self, event: Optional[tk.Event] = None) -> None:
        if getattr(event, "widget", None) == self.root:
            if not self._shutdown_called:
                self._shutdown_called = True
                self.controller.shutdown()
