"""Tab view implementations for ORBIT: Today, System, Insights, Contacts, Files, Verify."""

from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import threading
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Optional

from core.insights import load_insights_summary, InsightsSummary
from core.procs import safe_name, SAFE_PROCESS_ERRORS
from contacts.store import ContactStore
from reminders.store import ReminderStore
from routines.store import RoutineStore
from security.audit import AuditLogger
from ui.theme import get_palette, Palette
import psutil


def format_audit_event_sentence(entry: dict) -> str:
    """Format an audit record as a plain sentence strictly using tool, decision, and outcome."""
    tool = str(entry.get("tool") or "action")
    decision = str(entry.get("decision") or "").strip()
    outcome = str(entry.get("outcome") or "").strip().lower()

    if outcome == "blocked":
        prefix = "Blocked"
    elif outcome in ("error", "failed"):
        prefix = "Failed"
    elif decision in ("denied", "cancelled", "user_denied"):
        prefix = "Denied"
    elif decision in ("confirmed", "user_approved"):
        prefix = "Approved"
    elif outcome == "success":
        prefix = "Success"
    else:
        prefix = outcome.capitalize() if outcome else "Event"

    if decision and decision not in ("allow", outcome, "success"):
        return f"{prefix}: {tool} ({decision})"
    return f"{prefix}: {tool}"


def mask_phone_number(phone: str) -> str:
    """Mask phone number preserving format and last 4 digits."""
    s = str(phone).strip()
    if not s:
        return ""
    if len(s) <= 4:
        return "****"
    if len(s) <= 7:
        return f"***-{s[-4:]}"
    return f"{s[:3]}-***-{s[-4:]}"


# --- 1. Today Tab View ---

class TodayTabView:
    """Today tab: Live clock, battery, active reminders with countdowns, routines, and recent events."""

    def __init__(
        self,
        container: ttk.Frame,
        app: Any,
        palette: Optional[Palette] = None,
        battery_probe: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.container = container
        self.app = app
        self.palette = palette or get_palette()
        self.battery_probe = battery_probe or getattr(psutil, "sensors_battery", lambda: None)
        self._timer_id: Optional[str] = None
        self._is_active = False

        self._build_ui()

    def _build_ui(self) -> None:
        # Top Header: Time, Date & Battery
        top_box = ttk.Frame(self.container, padding=4)
        top_box.pack(fill="x", pady=(0, 6))

        self.time_lbl = ttk.Label(
            top_box,
            text="--:--:--",
            font=("Segoe UI", 16, "bold"),
            foreground=self.palette.accent,
        )
        self.time_lbl.pack(side="left")

        self.date_lbl = ttk.Label(
            top_box,
            text="",
            font=("Segoe UI", 10),
            foreground=self.palette.text_dim,
        )
        self.date_lbl.pack(side="left", padx=(10, 0))

        self.battery_lbl = ttk.Label(
            top_box,
            text="",
            font=("Segoe UI", 9, "bold"),
            foreground=self.palette.ok,
        )
        self.battery_lbl.pack(side="right")

        # Section 1: Active Reminders & Next Due
        rem_sec = ttk.LabelFrame(self.container, text="Active Reminders & Timers", padding=6)
        rem_sec.pack(fill="x", pady=(0, 6))

        self.next_due_lbl = ttk.Label(
            rem_sec,
            text="Next due: None",
            font=("Segoe UI", 9, "bold"),
            foreground=self.palette.accent,
        )
        self.next_due_lbl.pack(anchor="w", pady=(0, 4))

        self.rem_tree = ttk.Treeview(
            rem_sec,
            columns=("text", "countdown"),
            show="headings",
            height=3,
            selectmode="browse",
        )
        self.rem_tree.heading("text", text="Reminder / Timer")
        self.rem_tree.heading("countdown", text="Due In")
        self.rem_tree.column("text", width=220, stretch=True)
        self.rem_tree.column("countdown", width=80, stretch=False)
        self.rem_tree.pack(fill="x")

        # Section 2: Quick Routines (up to 6 buttons)
        rout_sec = ttk.LabelFrame(self.container, text="Quick Routines", padding=6)
        rout_sec.pack(fill="x", pady=(0, 6))

        self.routine_btn_box = ttk.Frame(rout_sec)
        self.routine_btn_box.pack(fill="x")

        # Section 3: Recent Events (last 20 audit entries)
        events_sec = ttk.LabelFrame(self.container, text="Recent Events", padding=6)
        events_sec.pack(fill="both", expand=True)

        self.events_list = tk.Listbox(
            events_sec,
            bg=self.palette.panel,
            fg=self.palette.text,
            font=("Segoe UI", 9),
            bd=0,
            highlightthickness=0,
            selectbackground=self.palette.panel_edge,
        )
        events_scroll = ttk.Scrollbar(events_sec, orient="vertical", command=self.events_list.yview)
        self.events_list.configure(yscrollcommand=events_scroll.set)
        self.events_list.pack(side="left", fill="both", expand=True)
        events_scroll.pack(side="right", fill="y")

    def start(self) -> None:
        if self._is_active:
            return
        self._is_active = True
        self.refresh()
        self._schedule_tick()

    def stop(self) -> None:
        self._is_active = False
        if self._timer_id:
            try:
                self.container.after_cancel(self._timer_id)
            except Exception:
                pass
            self._timer_id = None

    def _schedule_tick(self) -> None:
        if not self._is_active:
            return
        self._timer_id = self.container.after(1000, self._on_tick)

    def _on_tick(self) -> None:
        if not self._is_active:
            return
        self.refresh_time()
        self.refresh_reminders()
        self._schedule_tick()

    def refresh(self) -> None:
        self.refresh_time()
        self.refresh_battery()
        self.refresh_reminders()
        self.refresh_routines()
        self.refresh_events()

    def refresh_time(self) -> None:
        now = datetime.now()
        self.time_lbl.configure(text=now.strftime("%H:%M:%S"))
        self.date_lbl.configure(text=now.strftime("%A, %b %d"))

    def refresh_battery(self) -> None:
        try:
            bat = self.battery_probe()
            if bat is not None and hasattr(bat, "percent"):
                charging = getattr(bat, "power_plugged", False)
                chg_str = " (Plugged in)" if charging else ""
                self.battery_lbl.configure(text=f"Battery: {int(bat.percent)}%{chg_str}")
                self.battery_lbl.pack(side="right")
            else:
                self.battery_lbl.pack_forget()
        except Exception:
            self.battery_lbl.pack_forget()

    def refresh_reminders(self) -> None:
        for item in self.rem_tree.get_children():
            self.rem_tree.delete(item)

        try:
            rstore = getattr(self.app, "reminder_store", None)
            if rstore is None:
                rstore = ReminderStore()
            active = rstore.list_active()
        except Exception:
            active = []

        now_utc = datetime.now(timezone.utc)
        earliest_due = None
        earliest_text = ""

        for rem in active:
            r_text = getattr(rem, "text", "") or getattr(rem, "message", "Reminder")
            due_at = getattr(rem, "due_at", None)
            cd_str = "Active"
            if due_at:
                try:
                    if isinstance(due_at, str):
                        clean_dt = due_at.replace("Z", "+00:00")
                        dt = datetime.fromisoformat(clean_dt)
                    else:
                        dt = due_at
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    diff = (dt - now_utc).total_seconds()
                    if diff <= 0:
                        cd_str = "Due now"
                    elif diff < 60:
                        cd_str = f"{int(diff)}s"
                    elif diff < 3600:
                        cd_str = f"{int(diff // 60)}m"
                    else:
                        cd_str = f"{int(diff // 3600)}h {int((diff % 3600) // 60)}m"

                    if earliest_due is None or dt < earliest_due:
                        earliest_due = dt
                        earliest_text = f"{r_text} ({cd_str})"
                except Exception:
                    pass

            self.rem_tree.insert("", "end", values=(r_text, cd_str))

        if earliest_text:
            self.next_due_lbl.configure(text=f"Next due: {earliest_text}")
        else:
            self.next_due_lbl.configure(text="Next due: None")

    def refresh_routines(self) -> None:
        for w in self.routine_btn_box.winfo_children():
            w.destroy()

        try:
            rstore = getattr(self.app.controller, "routine_store", None)
            if rstore is None:
                rstore = RoutineStore()
            routines = rstore.list()[:6]
        except Exception:
            routines = []

        if not routines:
            lbl = ttk.Label(self.routine_btn_box, text="No routines saved", font=("Segoe UI", 9, "italic"))
            lbl.pack(side="left")
            return

        for r in routines:
            r_name = getattr(r, "name", "")
            if not r_name:
                continue

            def _make_cmd(name=r_name):
                return lambda: self.app.controller.submit(f"run routine {name}", source="ui")

            btn = ttk.Button(self.routine_btn_box, text=r_name, command=_make_cmd(r_name))
            btn.pack(side="left", padx=2, pady=2)

    def refresh_events(self) -> None:
        self.events_list.delete(0, tk.END)
        try:
            logger = getattr(self.app, "audit_logger", None)
            if logger is None:
                logger = AuditLogger()
            entries = logger.read_all()[-20:]
        except Exception:
            entries = []

        if not entries:
            self.events_list.insert(tk.END, "No recent events recorded.")
            return

        for e in reversed(entries):
            sentence = format_audit_event_sentence(e)
            self.events_list.insert(tk.END, sentence)


# --- 2. System Tab View ---

class SystemTabView:
    """System tab: Sparklines canvas, top 5 procs by CPU/memory, GPU query."""

    def __init__(
        self,
        container: ttk.Frame,
        app: Any,
        palette: Optional[Palette] = None,
        cpu_probe: Optional[Callable[[], float]] = None,
        mem_probe: Optional[Callable[[], float]] = None,
        disk_probe: Optional[Callable[[], float]] = None,
        gpu_probe: Optional[Callable[[], dict]] = None,
        process_probe: Optional[Callable[[], dict]] = None,
    ) -> None:
        self.container = container
        self.app = app
        self.palette = palette or get_palette()
        self.cpu_probe = cpu_probe or getattr(psutil, "cpu_percent", lambda interval=None: 0.0)
        self.mem_probe = mem_probe or (lambda: getattr(psutil.virtual_memory(), "percent", 0.0) if hasattr(psutil, "virtual_memory") else 0.0)
        self.disk_probe = disk_probe or (lambda: (getattr(psutil.disk_usage("/"), "free", 0) / (1024**3)) if hasattr(psutil, "disk_usage") else 0.0)
        self.gpu_probe = gpu_probe or self._default_gpu_probe
        self.process_probe = process_probe or self._default_process_probe

        self._cpu_hist: deque = deque(maxlen=60)
        self._mem_hist: deque = deque(maxlen=60)
        self._timer_id: Optional[str] = None
        self._is_active = False
        self._gpu_info = "GPU: not available"

        self._build_ui()

    def _default_gpu_probe(self) -> dict:
        import os
        from config import settings
        if not getattr(settings, "SYSTEM_TAB_GPU", 1) or os.environ.get("PYTEST_CURRENT_TEST"):
            return {"available": False}
        try:
            import subprocess
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                parts = [p.strip() for p in res.stdout.strip().splitlines()[0].split(",")]
                if len(parts) >= 5:
                    return {
                        "available": True,
                        "name": parts[0],
                        "utilization": float(parts[1]),
                        "memory_used": float(parts[2]),
                        "memory_total": float(parts[3]),
                        "temperature": float(parts[4]),
                    }
        except Exception:
            pass
        return {"available": False}

    def _default_process_probe(self) -> dict:
        cpu_list: list[dict] = []
        mem_list: list[dict] = []
        try:
            for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
                try:
                    info = p.info
                    name = safe_name(p) or info.get("name") or "unknown"
                    pid = info.get("pid") or p.pid
                    cpu = info.get("cpu_percent") or 0.0
                    mem = info.get("memory_percent") or 0.0
                    item = {"pid": pid, "name": name, "cpu": cpu, "mem": mem}
                    cpu_list.append(item)
                    mem_list.append(item)
                except SAFE_PROCESS_ERRORS:
                    continue
            top_cpu = sorted(cpu_list, key=lambda x: x["cpu"], reverse=True)[:5]
            top_mem = sorted(mem_list, key=lambda x: x["mem"], reverse=True)[:5]
            return {"cpu": top_cpu, "mem": top_mem}
        except Exception:
            return {"cpu": [], "mem": []}

    def _build_ui(self) -> None:
        # Gauges & Sparklines
        gauge_box = ttk.LabelFrame(self.container, text="System Metrics", padding=6)
        gauge_box.pack(fill="x", pady=(0, 6))

        self.stats_lbl = ttk.Label(
            gauge_box,
            text="CPU: 0% | RAM: 0% | Disk Free: 0 GB",
            font=("Segoe UI", 9, "bold"),
            foreground=self.palette.accent,
        )
        self.stats_lbl.pack(anchor="w", pady=(0, 4))

        self.spark_canvas = tk.Canvas(
            gauge_box,
            height=60,
            bg=self.palette.panel,
            bd=0,
            highlightthickness=1,
            highlightbackground=self.palette.panel_edge,
        )
        self.spark_canvas.pack(fill="x", pady=(0, 4))

        self.gpu_lbl = ttk.Label(
            gauge_box,
            text=self._gpu_info,
            font=("Segoe UI", 9),
            foreground=self.palette.text_dim,
        )
        self.gpu_lbl.pack(anchor="w")

        # Top Processes
        procs_box = ttk.LabelFrame(self.container, text="Top Processes", padding=6)
        procs_box.pack(fill="both", expand=True)

        self.proc_tree = ttk.Treeview(
            procs_box,
            columns=("name", "cpu", "mem"),
            show="headings",
            selectmode="browse",
        )
        self.proc_tree.heading("name", text="Process")
        self.proc_tree.heading("cpu", text="CPU %")
        self.proc_tree.heading("mem", text="RAM %")
        self.proc_tree.column("name", width=160, stretch=True)
        self.proc_tree.column("cpu", width=70, stretch=False)
        self.proc_tree.column("mem", width=70, stretch=False)
        self.proc_tree.pack(fill="both", expand=True)

    def start(self) -> None:
        if self._is_active:
            return
        self._is_active = True
        self.sample()
        self._schedule_tick()

    def stop(self) -> None:
        self._is_active = False
        if self._timer_id:
            try:
                self.container.after_cancel(self._timer_id)
            except Exception:
                pass
            self._timer_id = None

    def _schedule_tick(self) -> None:
        if not self._is_active:
            return
        self._timer_id = self.container.after(3000, self._on_tick)

    def _on_tick(self) -> None:
        if not self._is_active:
            return
        self.sample()
        self._schedule_tick()

    def sample(self) -> None:
        try:
            cpu_val = float(self.cpu_probe())
            mem_val = float(self.mem_probe())
            disk_val = float(self.disk_probe())
        except Exception:
            cpu_val, mem_val, disk_val = 0.0, 0.0, 0.0

        self._cpu_hist.append(cpu_val)
        self._mem_hist.append(mem_val)

        self.stats_lbl.configure(
            text=f"CPU: {cpu_val:.1f}% | RAM: {mem_val:.1f}% | Disk Free: {disk_val:.1f} GB"
        )
        self._draw_sparklines()

        # Update GPU info in background if active
        if self._is_active:
            threading.Thread(target=self._query_gpu_worker, daemon=True).start()

        # Update top processes
        procs_data = self.process_probe()
        for item in self.proc_tree.get_children():
            self.proc_tree.delete(item)

        top_cpu = procs_data.get("cpu", [])
        for p in top_cpu:
            self.proc_tree.insert("", "end", values=(p.get("name", ""), f"{p.get('cpu', 0):.1f}%", f"{p.get('mem', 0):.1f}%"))

    def _query_gpu_worker(self) -> None:
        if not self._is_active:
            return
        try:
            info = self.gpu_probe()
            if info.get("available"):
                text = f"GPU: {info.get('name')} | Util: {info.get('utilization'):.0f}% | Temp: {info.get('temperature'):.0f}°C"
            else:
                text = "GPU: not available"
        except Exception:
            text = "GPU: not available"

        if not self._is_active:
            return

        self._gpu_info = text
        try:
            if hasattr(self, "container") and self.container.winfo_exists():
                self.container.after(0, lambda: self._safe_update_gpu_lbl(self._gpu_info))
        except Exception:
            pass

    def _safe_update_gpu_lbl(self, text: str) -> None:
        try:
            if hasattr(self, "gpu_lbl") and self.gpu_lbl.winfo_exists():
                self.gpu_lbl.configure(text=text)
        except Exception:
            pass

    def _draw_sparklines(self) -> None:
        self.spark_canvas.delete("all")
        w = self.spark_canvas.winfo_width() or 300
        h = self.spark_canvas.winfo_height() or 60

        if len(self._cpu_hist) < 2:
            return

        step = w / 60.0

        # Draw CPU line in accent color
        pts_cpu = []
        for i, val in enumerate(self._cpu_hist):
            x = i * step
            y = h - (val / 100.0 * (h - 6)) - 3
            pts_cpu.extend([x, y])
        if len(pts_cpu) >= 4:
            self.spark_canvas.create_line(pts_cpu, fill=self.palette.accent, width=1)

        # Draw Memory line in text_dim color
        pts_mem = []
        for i, val in enumerate(self._mem_hist):
            x = i * step
            y = h - (val / 100.0 * (h - 6)) - 3
            pts_mem.extend([x, y])
        if len(pts_mem) >= 4:
            self.spark_canvas.create_line(pts_mem, fill="#4ade80", width=1, dash=(2, 2))


# --- 3. Insights Tab View ---

class InsightsTabView:
    """Insights tab: Execution analytics, success rates, latency, and activity bar chart."""

    def __init__(self, container: ttk.Frame, app: Any, palette: Optional[Palette] = None) -> None:
        self.container = container
        self.app = app
        self.palette = palette or get_palette()
        self.period = "all"
        self._cached_summary: Optional[InsightsSummary] = None
        self._last_loaded_time: float = 0.0

        self._build_ui()

    def _build_ui(self) -> None:
        # Period Selector
        btn_bar = ttk.Frame(self.container, padding=4)
        btn_bar.pack(fill="x", pady=(0, 6))

        ttk.Label(btn_bar, text="Period: ", font=("Segoe UI", 9, "bold")).pack(side="left")

        self.btn_24h = ttk.Button(btn_bar, text="24 Hours", command=lambda: self.set_period("24h"))
        self.btn_24h.pack(side="left", padx=2)

        self.btn_7d = ttk.Button(btn_bar, text="7 Days", command=lambda: self.set_period("7d"))
        self.btn_7d.pack(side="left", padx=2)

        self.btn_all = ttk.Button(btn_bar, text="All", command=lambda: self.set_period("all"))
        self.btn_all.pack(side="left", padx=2)

        # Cards Frame
        cards_box = ttk.LabelFrame(self.container, text="Metrics Summary", padding=8)
        cards_box.pack(fill="x", pady=(0, 6))

        self.lbl_success = ttk.Label(cards_box, text="Tool Success Rate: --", font=("Segoe UI", 10, "bold"), foreground=self.palette.ok)
        self.lbl_success.pack(anchor="w", pady=1)

        self.lbl_confirms = ttk.Label(cards_box, text="Confirmations: Approved: 0 | Denied: 0", font=("Segoe UI", 9))
        self.lbl_confirms.pack(anchor="w", pady=1)

        self.lbl_blocked = ttk.Label(cards_box, text="Blocked Actions: None", font=("Segoe UI", 9))
        self.lbl_blocked.pack(anchor="w", pady=1)

        self.lbl_latency = ttk.Label(cards_box, text="Task Latency: Median: 0ms | p95: 0ms", font=("Segoe UI", 9))
        self.lbl_latency.pack(anchor="w", pady=1)

        self.lbl_voice = ttk.Label(cards_box, text="Voice Usage: Reviewed: 0 | Direct: 0", font=("Segoe UI", 9))
        self.lbl_voice.pack(anchor="w", pady=1)

        # Daily Activity Chart
        chart_box = ttk.LabelFrame(self.container, text="Daily Activity", padding=6)
        chart_box.pack(fill="both", expand=True)

        self.chart_canvas = tk.Canvas(
            chart_box,
            bg=self.palette.panel,
            bd=0,
            highlightthickness=0,
        )
        self.chart_canvas.pack(fill="both", expand=True)

    def set_period(self, period: str) -> None:
        self.period = period
        self.load_data(force=True)

    def load_data(self, force: bool = False) -> None:
        import time
        now = time.time()
        if not force and self._cached_summary and (now - self._last_loaded_time < 10.0):
            self._render_summary(self._cached_summary)
            return

        def _worker():
            summary = load_insights_summary(period=self.period)
            self._cached_summary = summary
            self._last_loaded_time = time.time()
            try:
                self.container.after(0, lambda: self._render_summary(summary))
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _render_summary(self, summary: InsightsSummary) -> None:
        if not summary.has_data:
            self.lbl_success.configure(text="No data yet", foreground=self.palette.text_dim)
            self.lbl_confirms.configure(text="Confirmations: No data yet")
            self.lbl_blocked.configure(text="Blocked Actions: None")
            self.lbl_latency.configure(text="Task Latency: No data yet")
            self.lbl_voice.configure(text="Voice Usage: No data yet")
            self.chart_canvas.delete("all")
            self.chart_canvas.create_text(
                150, 60, text="No activity data recorded yet.", fill=self.palette.text_dim, font=("Segoe UI", 9, "italic")
            )
            return

        self.lbl_success.configure(
            text=f"Tool Success Rate: {summary.tool_success_rate:.1f}% ({summary.successful_tool_calls}/{summary.total_tool_calls})",
            foreground=self.palette.ok,
        )
        self.lbl_confirms.configure(
            text=f"Confirmations: Approved: {summary.confirmations_approved} | Denied: {summary.confirmations_denied}"
        )
        blocked_str = ", ".join(f"{k}: {v}" for k, v in summary.blocked_by_type.items()) if summary.blocked_by_type else "None"
        self.lbl_blocked.configure(text=f"Blocked Actions: {blocked_str}")
        self.lbl_latency.configure(
            text=f"Task Latency: Median: {summary.median_latency_ms:.0f}ms | p95: {summary.p95_latency_ms:.0f}ms"
        )
        self.lbl_voice.configure(
            text=f"Voice Usage: Reviewed: {summary.voice_reviewed} | Direct: {summary.voice_direct}"
        )

        self._draw_daily_chart(summary.daily_activity)

    def _draw_daily_chart(self, daily: list[tuple[str, int]]) -> None:
        self.chart_canvas.delete("all")
        if not daily:
            return

        w = self.chart_canvas.winfo_width() or 300
        h = self.chart_canvas.winfo_height() or 100
        max_val = max(c for _, c in daily) or 1
        n = len(daily)
        bar_w = max(4, min(24, int(w / (n * 1.5))))

        for idx, (day_str, count) in enumerate(daily):
            x = int(idx * (w / max(1, n)) + 10)
            bar_h = int((count / max_val) * (h - 25))
            y0 = h - 20 - bar_h
            y1 = h - 20
            self.chart_canvas.create_rectangle(
                x, y0, x + bar_w, y1,
                fill=self.palette.accent, outline="",
            )
            short_day = day_str[-5:]
            self.chart_canvas.create_text(
                x + bar_w // 2, h - 10,
                text=short_day,
                fill=self.palette.text_dim,
                font=("Segoe UI", 7),
            )


# --- 4. Contacts Tab View ---

class ContactsTabView:
    """Contacts tab: Contact list with masked numbers, total count, add/edit buttons."""

    def __init__(self, container: ttk.Frame, app: Any, palette: Optional[Palette] = None) -> None:
        self.container = container
        self.app = app
        self.palette = palette or get_palette()

        self._build_ui()

    def _build_ui(self) -> None:
        top_box = ttk.Frame(self.container, padding=4)
        top_box.pack(fill="x", pady=(0, 6))

        self.count_lbl = ttk.Label(
            top_box,
            text="Total Contacts: 0",
            font=("Segoe UI", 9, "bold"),
            foreground=self.palette.accent,
        )
        self.count_lbl.pack(side="left")

        btn_box = ttk.Frame(self.container, padding=4)
        btn_box.pack(fill="x", side="bottom")

        add_btn = ttk.Button(btn_box, text="Add Contact", command=self.app._on_contacts_click)
        add_btn.pack(side="left", padx=(0, 6))

        edit_btn = ttk.Button(btn_box, text="Edit Contacts", command=self.app._on_contacts_click)
        edit_btn.pack(side="left")

        self.tree = ttk.Treeview(
            self.container,
            columns=("name", "phone", "notes"),
            show="headings",
            selectmode="browse",
        )
        self.tree.heading("name", text="Name")
        self.tree.heading("phone", text="Phone")
        self.tree.heading("notes", text="Notes")
        self.tree.column("name", width=120, stretch=False)
        self.tree.column("phone", width=110, stretch=False)
        self.tree.column("notes", width=150, stretch=True)

        scroll = ttk.Scrollbar(self.container, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)

        self.tree.pack(fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def refresh(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

        try:
            cstore = getattr(self.app, "contacts_store", None) or ContactStore()
            contacts = cstore.list_all()
        except Exception:
            contacts = []

        self.count_lbl.configure(text=f"Total Contacts: {len(contacts)}")
        for c in contacts:
            name = getattr(c, "name", "")
            raw_phone = getattr(c, "phone", "")
            notes = getattr(c, "notes", "") or ""
            masked = mask_phone_number(raw_phone)
            self.tree.insert("", "end", values=(name, masked, notes))


# --- 5. Files Tab View ---

class FilesTabView:
    """Files tab: Found candidate files, open/reveal buttons, and empty prompt."""

    def __init__(self, container: ttk.Frame, app: Any, palette: Optional[Palette] = None) -> None:
        self.container = container
        self.app = app
        self.palette = palette or get_palette()

        self._build_ui()

    def _build_ui(self) -> None:
        self.empty_lbl = ttk.Label(
            self.container,
            text="Say: find my latest download",
            font=("Segoe UI", 10, "italic"),
            foreground=self.palette.text_dim,
        )

        btn_box = ttk.Frame(self.container, padding=4)
        btn_box.pack(fill="x", side="bottom")

        self.open_btn = ttk.Button(btn_box, text="Open", command=self._on_open)
        self.open_btn.pack(side="left", padx=(0, 6))

        self.reveal_btn = ttk.Button(btn_box, text="Reveal in Folder", command=self._on_reveal)
        self.reveal_btn.pack(side="left")

        self.tree = ttk.Treeview(
            self.container,
            columns=("name", "folder"),
            show="headings",
            selectmode="browse",
        )
        self.tree.heading("name", text="File Name")
        self.tree.heading("folder", text="Folder")
        self.tree.column("name", width=140, stretch=False)
        self.tree.column("folder", width=200, stretch=True)

        self.scroll = ttk.Scrollbar(self.container, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.scroll.set)

    def refresh(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

        files = []
        try:
            s_state = getattr(self.app.controller, "session_state", None)
            if s_state:
                files = s_state.get_found_files()
        except Exception:
            files = []

        if not files:
            self.tree.pack_forget()
            self.scroll.pack_forget()
            self.empty_lbl.pack(pady=30)
            self.open_btn.configure(state="disabled")
            self.reveal_btn.configure(state="disabled")
        else:
            self.empty_lbl.pack_forget()
            self.tree.pack(fill="both", expand=True)
            self.scroll.pack(side="right", fill="y")
            self.open_btn.configure(state="normal")
            self.reveal_btn.configure(state="normal")

            for f in files:
                p = Path(f)
                self.tree.insert("", "end", values=(p.name, str(p.parent)), tags=(f,))

    def _get_selected_file(self) -> Optional[str]:
        sel = self.tree.selection()
        if not sel:
            return None
        tags = self.tree.item(sel[0], "tags")
        return tags[0] if tags else None

    def _on_open(self) -> None:
        target = self._get_selected_file()
        if target:
            self.app.controller.submit(f"open {target}", source="ui")

    def _on_reveal(self) -> None:
        target = self._get_selected_file()
        if target:
            self.app.controller.submit(f"reveal {target}", source="ui")


# --- 6. Verify Tab View ---

class VerifyTabView:
    """Verify tab: Verification progress summary and button to launch verification window."""

    def __init__(self, container: ttk.Frame, app: Any, palette: Optional[Palette] = None) -> None:
        self.container = container
        self.app = app
        self.palette = palette or get_palette()

        self._build_ui()

    def _build_ui(self) -> None:
        box = ttk.LabelFrame(self.container, text="Verification Status", padding=12)
        box.pack(fill="both", expand=True, pady=6)

        self.summary_lbl = ttk.Label(
            box,
            text="Verification checks are available to audit system readiness.",
            font=("Segoe UI", 9),
            wraplength=280,
        )
        self.summary_lbl.pack(anchor="w", pady=(0, 10))

        open_btn = ttk.Button(
            box,
            text="Open Verification Window",
            command=self.app._on_verify_click,
        )
        open_btn.pack(anchor="w")
