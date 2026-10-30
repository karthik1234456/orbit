"""Command palette window for ORBIT (Ctrl+K).

Provides a focused search interface for invoking application commands, running routines,
and submitting queries without modal blocking.

Privacy & Security Guarantees:
- Strictly does NOT import or read memory stores, document indexes, reminder stores,
  or chat history.
- Routine entries expose sanitized routine names only from RoutineStore.
- All submissions and routine executions flow through standard controller paths
  preserving plan previews, taint tracking, and confirmation dialogs.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
from typing import Callable, Optional
import tkinter as tk

from ui.theme import Palette, apply_theme, get_palette


@dataclass
class PaletteItem:
    """Action entry displayed in the command palette."""

    title: str
    category: str
    handler: Callable[[], None]
    score: float = 0.0


# In-memory history of recently executed palette command titles (max 5)
_RECENT_COMMAND_TITLES: list[str] = []


def fuzzy_match(query: str, target: str) -> Optional[tuple[int, int]]:
    """Check if query is a case-insensitive subsequence of target.

    Returns:
        (first_pos, span_length) if matched, or None.
    """
    q = query.lower().strip()
    t = target.lower()

    if not q:
        return (0, 0)

    q_idx = 0
    first_pos = -1
    last_pos = -1

    for t_idx, char in enumerate(t):
        if char == q[q_idx]:
            if first_pos == -1:
                first_pos = t_idx
            last_pos = t_idx
            q_idx += 1
            if q_idx == len(q):
                break

    if q_idx == len(q):
        span = (last_pos - first_pos) + 1
        return (first_pos, span)
    return None


def score_item(query: str, title: str) -> Optional[float]:
    """Compute fuzzy match score (lower score = higher rank).

    Scores prioritize:
    1. Earliest match start index
    2. Tightest matching span
    3. Minimum length delta
    """
    match = fuzzy_match(query, title)
    if match is None:
        return None
    first_pos, span = match
    # Weight: start pos * 10.0 + span * 2.0 + length difference
    return (first_pos * 10.0) + (span * 2.0) + float(len(title) - len(query))


def filter_and_rank_items(query: str, all_items: list[PaletteItem], max_results: int = 12) -> list[PaletteItem]:
    """Filter and rank candidate palette items based on fuzzy matching."""
    q = query.strip()
    if not q:
        # When query is empty, sort by recent history first, then original order
        history_map = {title: idx for idx, title in enumerate(_RECENT_COMMAND_TITLES)}
        ordered: list[PaletteItem] = []
        rest: list[PaletteItem] = []
        for item in all_items:
            if item.title in history_map:
                ordered.append(item)
            else:
                rest.append(item)
        ordered.sort(key=lambda x: history_map[x.title])
        return (ordered + rest)[:max_results]

    scored: list[tuple[float, PaletteItem]] = []
    for item in all_items:
        s = score_item(q, item.title)
        if s is not None:
            scored.append((s, item))

    scored.sort(key=lambda pair: pair[0])
    results = [item for _, item in scored]
    return results[:max_results]


class CommandPalette(tk.Toplevel):
    """Small centered, non-modal command palette dialog."""

    def __init__(
        self,
        parent: tk.Widget,
        static_items: list[PaletteItem],
        routine_names: list[str],
        on_run_routine: Callable[[str], None],
        on_submit_query: Callable[[str], None],
        palette: Optional[Palette] = None,
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(parent)
        self.title("Command palette")
        self.geometry("520x400")
        self.minsize(420, 300)
        self.resizable(False, False)

        self.on_close_cb = on_close
        self.static_items = list(static_items)
        self.routine_names = list(routine_names)
        self.on_run_routine = on_run_routine
        self.on_submit_query = on_submit_query

        self.palette_theme = palette or get_palette("gold")
        apply_theme(self, self.palette_theme)
        self.configure(bg=self.palette_theme.bg)

        self._current_results: list[PaletteItem] = []
        self._selected_index: int = 0

        self._build_ui()
        self._bind_events()
        self._update_results()

        # Center over parent
        try:
            self.transient(parent.winfo_toplevel())
            px = parent.winfo_rootx() + (parent.winfo_width() // 2) - 260
            py = parent.winfo_rooty() + (parent.winfo_height() // 2) - 200
            self.geometry(f"+{max(10, px)}+{max(10, py)}")
        except Exception:
            pass

        self.entry.focus_set()

    def _build_ui(self) -> None:
        pal = self.palette_theme

        # Container
        self.main_frame = tk.Frame(self, bg=pal.bg, padx=12, pady=12)
        self.main_frame.pack(fill="both", expand=True)

        # Header title
        header_lbl = tk.Label(
            self.main_frame,
            text="COMMAND PALETTE",
            font=("Segoe UI", 10, "bold"),
            fg=pal.accent,
            bg=pal.bg,
        )
        header_lbl.pack(anchor="w", pady=(0, 6))

        # Search Entry box
        self.entry_var = tk.StringVar()
        self.entry_var.trace_add("write", lambda *args: self._update_results())

        self.entry = tk.Entry(
            self.main_frame,
            textvariable=self.entry_var,
            bg=pal.panel,
            fg=pal.text,
            insertbackground=pal.text,
            relief="solid",
            bd=1,
            font=("Segoe UI", 11),
        )
        self.entry.pack(fill="x", ipady=4, pady=(0, 10))

        # Results Listbox
        list_container = tk.Frame(self.main_frame, bg=pal.panel, bd=1, relief="solid")
        list_container.pack(fill="both", expand=True)

        self.listbox = tk.Listbox(
            list_container,
            bg=pal.panel,
            fg=pal.text,
            selectbackground=pal.accent,
            selectforeground=pal.bg,
            highlightthickness=0,
            relief="flat",
            font=("Segoe UI", 10),
            activestyle="none",
        )
        self.listbox.pack(side="left", fill="both", expand=True, padx=4, pady=4)

        # Bottom hint bar
        hint_lbl = tk.Label(
            self.main_frame,
            text="Up/Down: Navigate | Enter: Execute | Esc: Close",
            font=("Segoe UI", 8),
            fg=pal.text_dim,
            bg=pal.bg,
        )
        hint_lbl.pack(anchor="w", pady=(8, 0))

    def _bind_events(self) -> None:
        self.bind("<Escape>", lambda e: self.close())
        self.entry.bind("<Down>", lambda e: self._move_selection(1))
        self.entry.bind("<Up>", lambda e: self._move_selection(-1))
        self.entry.bind("<Return>", lambda e: self._on_enter())
        self.listbox.bind("<Return>", lambda e: self._on_enter())
        self.listbox.bind("<Double-Button-1>", lambda e: self._on_enter())
        self.listbox.bind("<<ListboxSelect>>", self._on_listbox_click)

    def _get_candidate_items(self) -> list[PaletteItem]:
        candidates = list(self.static_items)
        for r_name in self.routine_names:
            clean_name = html.escape(r_name).strip()
            item = PaletteItem(
                title=f"Run routine {clean_name}",
                category="Routines",
                handler=lambda name=clean_name: self.on_run_routine(name),
            )
            candidates.append(item)
        return candidates

    def _update_results(self) -> None:
        query = self.entry_var.get()
        candidates = self._get_candidate_items()

        results = filter_and_rank_items(query, candidates, max_results=11)

        # If query is non-empty, append "Ask ORBIT: <query>" as the final entry
        q_clean = query.strip()
        if q_clean:
            ask_item = PaletteItem(
                title=f"Ask ORBIT: {q_clean}",
                category="Prompt",
                handler=lambda q=q_clean: self.on_submit_query(q),
            )
            results.append(ask_item)

        self._current_results = results
        self.listbox.delete(0, "end")

        for item in self._current_results:
            prefix = f"[{item.category}] " if item.category else ""
            self.listbox.insert("end", f"{prefix}{item.title}")

        if self._current_results:
            self._selected_index = max(0, min(self._selected_index, len(self._current_results) - 1))
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(self._selected_index)
            self.listbox.activate(self._selected_index)

    def _move_selection(self, delta: int) -> str:
        if not self._current_results:
            return "break"
        n = len(self._current_results)
        self._selected_index = (self._selected_index + delta) % n
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(self._selected_index)
        self.listbox.activate(self._selected_index)
        self.listbox.see(self._selected_index)
        return "break"

    def _on_listbox_click(self, event: tk.Event) -> None:
        sel = self.listbox.curselection()
        if sel:
            self._selected_index = sel[0]

    def _on_enter(self) -> str:
        if not self._current_results:
            return "break"
        idx = max(0, min(self._selected_index, len(self._current_results) - 1))
        item = self._current_results[idx]

        # Remember action title in memory history (except dynamic Ask ORBIT queries)
        if not item.title.startswith("Ask ORBIT:"):
            global _RECENT_COMMAND_TITLES
            if item.title in _RECENT_COMMAND_TITLES:
                _RECENT_COMMAND_TITLES.remove(item.title)
            _RECENT_COMMAND_TITLES.insert(0, item.title)
            _RECENT_COMMAND_TITLES = _RECENT_COMMAND_TITLES[:5]

        self.close()
        item.handler()
        return "break"

    def close(self) -> None:
        try:
            self.destroy()
        except Exception:
            pass
        if self.on_close_cb is not None:
            try:
                self.on_close_cb()
            except Exception:
                pass
