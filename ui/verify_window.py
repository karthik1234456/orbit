"""Guided verification window for ORBIT desktop GUI.

Non-modal inspection and verification window driven by checklist items and live evidence polling.

Guarantees:
- Non-modal top-level window (never blocks parent interaction or confirmation dialogs).
- Live evidence status badge refreshed every 2 seconds.
- Automatically flags blocked items with exact setting or dependency commands.
- Records tester notes (<= 200 chars) locally; never leaks them to exports.
- Clean theme styling adhering to ORBIT Palette.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Optional, Sequence

from config.paths import REPO_ROOT
from core.version import VERSION
from ui.theme import Palette, apply_theme, get_palette
from verify.export import export_markdown
from verify.runner import VerificationSession
from verify.schema import load_items_from_json


class VerificationWindow(tk.Toplevel):
    """Non-modal verification window for executing guided checks and viewing evidence."""

    def __init__(
        self,
        parent: tk.Widget,
        session: Optional[VerificationSession] = None,
        palette: Optional[Palette] = None,
        items: Optional[Sequence[Any]] = None,
    ) -> None:
        super().__init__(parent)
        self.parent_root = parent
        self.palette = palette or getattr(parent, "palette", None) or get_palette()

        if session is not None:
            self.session = session
        else:
            loaded_items = items
            if loaded_items is None:
                items_file = REPO_ROOT / "verify" / "items.json"
                if items_file.is_file():
                    loaded_items = load_items_from_json(items_file)
                else:
                    loaded_items = []
            self.session = VerificationSession(items=loaded_items or [])

        self.title(f"ORBIT Verification Suite (v{VERSION})")
        self.transient(parent)
        self.geometry("780x620")
        self.minsize(680, 500)

        self._poll_timer_id: Optional[str] = None
        self._is_active = True

        apply_theme(self, self.palette)
        self._build_ui()
        self._render_current_item()
        self._schedule_poll()

        self.bind("<Destroy>", self._on_destroy)

    def _build_ui(self) -> None:
        main_frame = ttk.Frame(self, padding=14)
        main_frame.pack(fill="both", expand=True)

        # Header Frame
        hdr_frame = ttk.Frame(main_frame)
        hdr_frame.pack(fill="x", pady=(0, 10))

        lbl_title = ttk.Label(
            hdr_frame,
            text=f"ORBIT Verification Suite (v{VERSION})",
            font=("Segoe UI", 12, "bold"),
        )
        lbl_title.pack(side="left")

        self.lbl_progress = ttk.Label(
            hdr_frame,
            text="Item 0 of 0",
            font=("Segoe UI", 9),
            foreground="#6b7280",
        )
        self.lbl_progress.pack(side="right")

        # Summary chip bar
        self.summary_var = tk.StringVar(value="")
        lbl_sum = ttk.Label(
            main_frame,
            textvariable=self.summary_var,
            font=("Segoe UI", 9, "italic"),
            foreground="#4b5563",
        )
        lbl_sum.pack(anchor="w", pady=(0, 8))

        # Item Details Container Frame
        self.card_frame = ttk.LabelFrame(main_frame, text="Current Verification Item", padding=12)
        self.card_frame.pack(fill="both", expand=True, pady=(0, 10))

        # ID, Area & Risk
        meta_frame = ttk.Frame(self.card_frame)
        meta_frame.pack(fill="x", pady=(0, 6))

        self.lbl_id = ttk.Label(meta_frame, text="", font=("Segoe UI", 11, "bold"))
        self.lbl_id.pack(side="left")

        self.lbl_risk = ttk.Label(
            meta_frame,
            text="",
            font=("Segoe UI", 9, "bold"),
            padding=(6, 2),
        )
        self.lbl_risk.pack(side="right")

        # Title
        self.lbl_item_title = ttk.Label(
            self.card_frame,
            text="",
            font=("Segoe UI", 11),
            wraplength=700,
        )
        self.lbl_item_title.pack(anchor="w", pady=(0, 8))

        # Blocking Alert Banner
        self.block_banner = ttk.Label(
            self.card_frame,
            text="",
            foreground="#b91c1c",
            font=("Segoe UI", 9, "bold"),
            wraplength=700,
            padding=(8, 6),
        )

        # Steps
        ttk.Label(self.card_frame, text="Steps to Perform:", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(4, 2))
        self.txt_steps = tk.Text(
            self.card_frame,
            height=4,
            wrap="word",
            font=("Segoe UI", 9),
            relief="flat",
            borderwidth=1,
        )
        self.txt_steps.pack(fill="x", pady=(0, 8))
        self.txt_steps.configure(state="disabled")

        # Expected
        ttk.Label(self.card_frame, text="Expected Behavior:", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(4, 2))
        self.lbl_expected = ttk.Label(
            self.card_frame,
            text="",
            font=("Segoe UI", 9),
            wraplength=700,
        )
        self.lbl_expected.pack(anchor="w", pady=(0, 8))

        # Evidence Box
        ev_box = ttk.LabelFrame(self.card_frame, text="Audit Evidence Telemetry", padding=8)
        ev_box.pack(fill="x", pady=(4, 8))

        self.lbl_evidence_status = ttk.Label(
            ev_box,
            text="Evidence: Checking...",
            font=("Segoe UI", 9, "bold"),
        )
        self.lbl_evidence_status.pack(anchor="w")

        self.lbl_evidence_reason = ttk.Label(
            ev_box,
            text="",
            font=("Segoe UI", 8),
            wraplength=680,
            foreground="#4b5563",
        )
        self.lbl_evidence_reason.pack(anchor="w")

        # Tester Notes Frame
        notes_frame = ttk.Frame(main_frame)
        notes_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(notes_frame, text="Tester Note (optional, max 200 chars):").pack(side="left", padx=(0, 6))
        self.note_var = tk.StringVar(value="")
        self.entry_note = ttk.Entry(notes_frame, textvariable=self.note_var)
        self.entry_note.pack(side="left", fill="x", expand=True)

        # Control Buttons Bar
        btn_bar = ttk.Frame(main_frame)
        btn_bar.pack(fill="x", side="bottom")

        self.btn_back = ttk.Button(btn_bar, text="Back", command=self._on_back)
        self.btn_back.pack(side="left", padx=(0, 6))

        self.btn_retry = ttk.Button(btn_bar, text="Retry", command=self._on_retry)
        self.btn_retry.pack(side="left", padx=(0, 6))

        self.btn_skip = ttk.Button(btn_bar, text="Skip", command=self._on_skip)
        self.btn_skip.pack(side="left", padx=(0, 6))

        close_btn = ttk.Button(btn_bar, text="Close", command=self.destroy)
        close_btn.pack(side="right", padx=(6, 0))

        export_btn = ttk.Button(btn_bar, text="Export Markdown", command=self._on_export)
        export_btn.pack(side="right", padx=(6, 0))

        self.btn_fail = ttk.Button(btn_bar, text="Fail", command=self._on_fail)
        self.btn_fail.pack(side="right", padx=(6, 0))

        self.btn_pass = ttk.Button(btn_bar, text="Pass", command=self._on_pass)
        self.btn_pass.pack(side="right")

    def _render_current_item(self) -> None:
        idx = self.session.current_index
        total = self.session.total_items
        summary = self.session.get_summary()

        self.lbl_progress.configure(text=f"Item {idx + 1} of {total}")
        self.summary_var.set(
            f"Pass: {summary['pass']} | Fail: {summary['fail']} | Skip: {summary['skip']} | "
            f"Blocked: {summary['blocked']} | Pending: {summary['pending']}"
        )

        item = self.session.current_item
        if not item:
            self.lbl_id.configure(text="Verification Complete")
            self.lbl_risk.configure(text="")
            self.lbl_item_title.configure(text="All verification items have been evaluated.")
            self.txt_steps.configure(state="normal")
            self.txt_steps.delete("1.0", "end")
            self.txt_steps.insert("end", "You can close this window or export status markdown.")
            self.txt_steps.configure(state="disabled")
            self.lbl_expected.configure(text="")
            self.lbl_evidence_status.configure(text="")
            self.lbl_evidence_reason.configure(text="")
            self.block_banner.pack_forget()
            return

        # Meta info
        self.lbl_id.configure(text=f"[{item.id}] Area: {item.area.upper()}")
        risk_color = "#16a34a" if item.risk == "safe" else ("#d97706" if item.risk == "touches_apps" else "#dc2626")
        self.lbl_risk.configure(text=f"Risk: {item.risk.upper()}", foreground=risk_color)
        self.lbl_item_title.configure(text=item.title)

        # Steps text
        self.txt_steps.configure(state="normal")
        self.txt_steps.delete("1.0", "end")
        for i, s in enumerate(item.steps, 1):
            self.txt_steps.insert("end", f"{i}. {s}\n")
        self.txt_steps.configure(state="disabled")

        # Expected
        self.lbl_expected.configure(text=item.expected)

        # Check blocking
        block_reason = self.session.check_blocking(item)
        if block_reason is not None:
            self.block_banner.configure(
                text=f"⚠ BLOCKED: {block_reason.message}\nSuggested Fix: {block_reason.suggested_fix}"
            )
            self.block_banner.pack(fill="x", pady=(0, 6), before=self.txt_steps)
        else:
            self.block_banner.pack_forget()

        # Existing notes if previously entered
        prev_res = self.session.results.get(item.id)
        self.note_var.set(prev_res.notes if prev_res and prev_res.notes else "")

        # Poll evidence immediately on item change
        self._update_evidence_badge()

    def _update_evidence_badge(self) -> None:
        item = self.session.current_item
        if not item or not item.evidence:
            self.lbl_evidence_status.configure(text="Evidence: None configured (manual observation)", foreground="#6b7280")
            self.lbl_evidence_reason.configure(text="")
            return

        res = self.session.poll_evidence()
        if res.status == "found":
            self.lbl_evidence_status.configure(text="Evidence: FOUND (supported by audit log)", foreground="#16a34a")
        elif res.status == "not_found":
            self.lbl_evidence_status.configure(text="Evidence: NOT FOUND (action not seen in audit)", foreground="#d97706")
        else:
            self.lbl_evidence_status.configure(text="Evidence: UNAVAILABLE", foreground="#6b7280")

        self.lbl_evidence_reason.configure(text=res.reason)

    def _schedule_poll(self) -> None:
        if not self._is_active:
            return
        self._update_evidence_badge()
        self._poll_timer_id = self.after(2000, self._schedule_poll)

    def _on_pass(self) -> None:
        note = self.note_var.get()
        ev_st = self.lbl_evidence_status.cget("text")
        self.session.pass_current(notes=note, evidence_status=ev_st)
        self._render_current_item()

    def _on_fail(self) -> None:
        note = self.note_var.get()
        ev_st = self.lbl_evidence_status.cget("text")
        self.session.fail_current(notes=note, evidence_status=ev_st)
        self._render_current_item()

    def _on_skip(self) -> None:
        note = self.note_var.get()
        self.session.skip_current(notes=note)
        self._render_current_item()

    def _on_back(self) -> None:
        if self.session.back():
            self._render_current_item()

    def _on_retry(self) -> None:
        self.session.retry_current()
        self._render_current_item()

    def _on_export(self) -> None:
        self.session.save()
        out_file = REPO_ROOT / "docs" / "VERIFICATION_STATUS.md"
        out_file.parent.mkdir(parents=True, exist_ok=True)

        md = export_markdown(
            results=self.session.results,
            items=self.session.items,
        )
        with open(out_file, "w", encoding="utf-8", newline="") as f:
            f.write(md)

        messagebox.showinfo(
            "Export Complete",
            f"Verification status report exported to:\n{out_file}",
            parent=self,
        )

    def _on_destroy(self, event: Any) -> None:
        if event.widget == self:
            self._is_active = False
            if self._poll_timer_id:
                try:
                    self.after_cancel(self._poll_timer_id)
                except Exception:
                    pass
            self.session.save()
