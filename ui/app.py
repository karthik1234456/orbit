"""Graphical user interface for ORBIT assistant built with Tkinter and TTK.

This module provides the primary window layout, streaming conversation view,
multi-tab activity/memory/docs inspector, and secure modal confirmation dialog.
Worker threads communicate strictly via ChatController; all widget updates
execute exclusively on the Tkinter main thread.
"""

import json
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import Any, Optional

from core.version import VERSION
from ui.controller import ChatController, ConfirmRequest, UiEvent


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

        # Build modal content
        self._build_ui()

        # Modal grab
        self.grab_set()

        # Keybindings and window protocol
        self.protocol("WM_DELETE_WINDOW", lambda: self._resolve(False))
        self.bind("<Escape>", lambda e: self._resolve(False))
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

    def _resolve(self, allowed: bool) -> None:
        if self._resolved:
            return
        self._resolved = True

        if self._countdown_timer is not None:
            self.after_cancel(self._countdown_timer)
            self._countdown_timer = None

        try:
            self.controller.resolve_confirmation(self.request_id, allowed)
        finally:
            try:
                self.grab_release()
            except Exception:
                pass
            self.destroy()


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

        self.root.title(f"ORBIT v{VERSION} Desktop Assistant")
        self.root.geometry("1020x680")
        self.root.minsize(800, 500)

        # Wire shutdown protocols
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Destroy>", self._on_root_destroyed)

        # Build UI layout
        self._build_layout()

        # Populate initial tab contents
        self._refresh_memories()
        self._refresh_documents()
        self._refresh_last_run()

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
        self._build_memories_tab()
        self._build_documents_tab()
        self._build_last_run_tab()

        # --- Status Bar ---
        self._build_status_bar(main_container)

    def _build_activity_tab(self) -> None:
        self.activity_frame = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(self.activity_frame, text="Activity")

        columns = ("step", "tool", "status", "info")
        self.activity_tree = ttk.Treeview(
            self.activity_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        self.activity_tree.heading("step", text="#")
        self.activity_tree.heading("tool", text="Tool")
        self.activity_tree.heading("status", text="Status")
        self.activity_tree.heading("info", text="Detail")

        self.activity_tree.column("step", width=35, stretch=False)
        self.activity_tree.column("tool", width=110, stretch=False)
        self.activity_tree.column("status", width=80, stretch=False)
        self.activity_tree.column("info", width=180, stretch=True)

        tree_scroll = ttk.Scrollbar(self.activity_frame, orient="vertical", command=self.activity_tree.yview)
        self.activity_tree.configure(yscrollcommand=tree_scroll.set)

        self.activity_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

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

    def _poll(self) -> None:
        if self._destroyed:
            return

        events = self.controller.drain(max_items=100)
        for event in events:
            self._handle_event(event)

        # Synchronize busy UI state
        self._update_busy_ui(self.controller.is_busy)

        # Dynamically attach Documents tab if doc_index became available
        if "Documents" not in [self.notebook.tab(i, "text") for i in self.notebook.tabs()]:
            if (
                self.controller.app
                and getattr(self.controller.app, "doc_index", None) is not None
            ):
                self.notebook.add(self.docs_frame, text="Documents")
                self._refresh_documents()

        if not self._destroyed:
            self.root.after(50, self._poll)

    def _handle_event(self, event: UiEvent) -> None:
        kind = event.kind
        data = event.data

        if kind == "delta":
            delta_text = data.get("delta") or data.get("text", "")
            if not self._streaming:
                self._streaming = True
                self._append_chat("ORBIT: ", ("orbit_prefix",))
            self._append_chat(delta_text, ("orbit",))

        elif kind == "reply":
            full_text = data.get("text") or data.get("reply", "")
            streamed = data.get("streamed", False)
            if not streamed and not self._streaming:
                self._append_chat("ORBIT: ", ("orbit_prefix",))
                self._append_chat(f"{full_text}\n\n", ("orbit",))
            else:
                self._append_chat("\n\n", ("orbit",))
            self._streaming = False

        elif kind == "error":
            msg = data.get("message") or data.get("error", "An error occurred.")
            self._append_chat(f"Error: {msg}\n\n", ("error",))
            self._streaming = False

        elif kind == "busy":
            self._append_chat("System: ORBIT is currently busy with another task.\n\n", ("system",))

        elif kind == "task_done":
            self._refresh_last_run()

        elif kind == "step_start":
            idx = data.get("index", "")
            tool = data.get("tool", "")
            self.activity_tree.insert("", "end", iid=f"step_{idx}", values=(idx, tool, "running...", ""))
            self.activity_tree.yview_moveto(1.0)

        elif kind == "step_end":
            idx = data.get("index", "")
            tool = data.get("tool", "")
            status = data.get("status", "")
            result = str(data.get("result", ""))[:60]
            symbol = "✓" if status == "success" else "✗"
            item_id = f"step_{idx}"
            display_tool = f"{symbol} {tool}"
            if self.activity_tree.exists(item_id):
                self.activity_tree.item(item_id, values=(idx, display_tool, status, result))
            else:
                self.activity_tree.insert("", "end", iid=item_id, values=(idx, display_tool, status, result))
            self.activity_tree.yview_moveto(1.0)

        elif kind == "auto_retrieval":
            injected = int(data.get("injected", 0) or 0)
            if injected > 0:
                self.activity_tree.insert(
                    "",
                    "end",
                    values=("", "documents", "retrieved", f"Searched your documents: {injected} passages"),
                )
                self.activity_tree.yview_moveto(1.0)

        elif kind == "confirm_request":
            req = data.get("request")
            if req is not None:
                self._confirm_dialog = ConfirmDialog(self.root, self.controller, req)

    def _update_busy_ui(self, is_busy: bool) -> None:
        if is_busy:
            self.busy_lbl.configure(text="● Busy", fg="#1976d2")
            self.send_btn.configure(state="disabled")
            self.input_text.configure(state="disabled")
            self.stop_btn.configure(state="normal")
        else:
            self.busy_lbl.configure(text="● Idle", fg="gray")
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
            self._append_chat("System: Submission rejected (busy or unattached).\n\n", ("system",))

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
                for mem in store.list_all():
                    self.memory_tree.insert(
                        "",
                        "end",
                        iid=str(mem.id),
                        values=(mem.id, mem.category, mem.content),
                    )
            except Exception:
                pass

    def _on_delete_memory(self) -> None:
        selected = self.memory_tree.selection()
        if not selected:
            return

        item_id = selected[0]
        confirmed = messagebox.askyesno(
            "Delete Memory",
            f"Are you sure you want to delete memory #{item_id}?",
            parent=self.root,
        )
        if not confirmed:
            return

        app = getattr(self.controller, "app", None)
        store = getattr(app, "memory_store", None)
        if store is not None and hasattr(store, "delete"):
            try:
                store.delete(int(item_id))
            except Exception as exc:
                messagebox.showerror("Error", f"Failed to delete memory: {exc}", parent=self.root)
            self._refresh_memories()

    def _refresh_documents(self) -> None:
        for item in self.docs_tree.get_children():
            self.docs_tree.delete(item)

        app = getattr(self.controller, "app", None)
        index = getattr(app, "doc_index", None)
        if index is not None and hasattr(index, "list_documents"):
            try:
                docs = index.list_documents()
                for doc in docs:
                    path, chunks, flagged, _ = doc
                    self.docs_tree.insert(
                        "",
                        "end",
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
