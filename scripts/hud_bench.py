"""Performance benchmark tool for the ORBIT sci-fi HUD rendering pipeline.

Measures frame rendering durations across:
- Tiers: high, medium, low
- Window sizes: 1280x800, 1920x1080
- Layer sets: all, no_dust, no_stars, no_bitmap

Collects average, p95, and worst-case frame times, compute_ms, and canvas_ms.
Outputs formatted benchmark tables matching docs/HUD_DESIGN.md targets.
"""

from __future__ import annotations

from pathlib import Path
import sys
import time
import tkinter as tk
from typing import Any

# Ensure project root is in sys.path when executed directly as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ui.hud import HudCanvas
from ui.hud_core import HudModel
from ui.theme import get_palette


def run_benchmark_matrix_cell(
    root: tk.Tk,
    tier_name: str,
    width: int,
    height: int,
    layer_set: str,
    duration_s: float = 2.0,
) -> dict[str, Any]:
    """Execute frame rendering loop for duration_s on specified tier, size, and layer set."""
    model = HudModel()
    tier_idx = HudModel().governor.TIERS.index(tier_name)
    model.governor._current_tier_idx = tier_idx

    palette = get_palette("cyan")
    realism_mode = "off" if layer_set == "no_bitmap" else "full"
    canvas = HudCanvas(
        root,
        model=model,
        palette=palette,
        fps=60,
        motion="full",
        width=width,
        height=height,
        realism=realism_mode,
    )
    canvas.pack()
    root.update()

    if layer_set == "no_dust":
        for pid in getattr(canvas, "_dust_back_items", []):
            canvas.itemconfigure(pid, state="hidden")
        for pid in getattr(canvas, "_dust_front_items", []):
            canvas.itemconfigure(pid, state="hidden")
    elif layer_set == "no_stars":
        for sid in getattr(canvas, "_static_star_items", []):
            canvas.itemconfigure(sid, state="hidden")

    frame_durations: list[float] = []
    compute_durations: list[float] = []
    canvas_durations: list[float] = []

    start_bench = time.monotonic()
    dt = 1.0 / 30.0

    while time.monotonic() - start_bench < duration_s:
        t0 = time.monotonic()
        canvas._draw_frame(dt)
        root.update_idletasks()
        t1 = time.monotonic()

        frame_durations.append(t1 - t0)
        compute_durations.append(getattr(canvas, "compute_ms", 0.0))
        canvas_durations.append(getattr(canvas, "canvas_ms", 0.0))

    canvas.destroy()

    if not frame_durations:
        return {
            "tier": tier_name,
            "size": f"{width}x{height}",
            "layer_set": layer_set,
            "frames": 0,
            "avg_ms": 0.0,
            "p95_ms": 0.0,
            "worst_ms": 0.0,
            "compute_avg_ms": 0.0,
            "canvas_avg_ms": 0.0,
            "fps": 0.0,
        }

    frame_durations.sort()
    n = len(frame_durations)
    avg_s = sum(frame_durations) / n
    p95_s = frame_durations[int(n * 0.95)] if n > 1 else frame_durations[-1]
    worst_s = frame_durations[-1]
    achieved_fps = n / duration_s

    comp_avg = sum(compute_durations) / max(1, len(compute_durations))
    canv_avg = sum(canvas_durations) / max(1, len(canvas_durations))

    return {
        "tier": tier_name,
        "size": f"{width}x{height}",
        "layer_set": layer_set,
        "frames": n,
        "avg_ms": avg_s * 1000.0,
        "p95_ms": p95_s * 1000.0,
        "worst_ms": worst_s * 1000.0,
        "compute_avg_ms": comp_avg,
        "canvas_avg_ms": canv_avg,
        "fps": achieved_fps,
    }


def main() -> None:
    print("=== ORBIT HUD RENDERING BENCHMARK MATRIX (A5) ===")
    print("Matrix: 3 Tiers x 2 Window Sizes x 4 Layer Sets\n")

    try:
        root = tk.Tk()
        root.withdraw()
    except Exception as exc:
        print(f"Error: Tkinter initialization failed: {exc}", file=sys.stderr)
        sys.exit(1)

    tiers = ("high", "medium", "low")
    sizes = ((1280, 800), (1920, 1080))
    layer_sets = ("all", "no_dust", "no_stars", "no_bitmap")

    results: list[dict[str, Any]] = []

    try:
        for t in tiers:
            for w, h in sizes:
                for ls in layer_sets:
                    desc = f"[{t:<6} | {w}x{h:<5} | {ls:<9}]"
                    print(f"Benchmarking {desc}... ", end="", flush=True)
                    res = run_benchmark_matrix_cell(root, t, w, h, ls, duration_s=1.5)
                    results.append(res)
                    print(f"avg {res['avg_ms']:.2f}ms | p95 {res['p95_ms']:.2f}ms | worst {res['worst_ms']:.2f}ms")
    finally:
        try:
            root.destroy()
        except Exception:
            pass

    print("\n" + "=" * 88)
    print(
        f"{'Tier':<7} | {'Size':<10} | {'Layers':<10} | {'Avg (ms)':<9} | "
        f"{'p95 (ms)':<9} | {'Worst (ms)':<10} | {'Compute':<8} | {'Canvas':<8} | {'FPS':<6}"
    )
    print("-" * 88)
    for r in results:
        print(
            f"{r['tier']:<7} | {r['size']:<10} | {r['layer_set']:<10} | "
            f"{r['avg_ms']:<9.2f} | {r['p95_ms']:<9.2f} | {r['worst_ms']:<10.2f} | "
            f"{r['compute_avg_ms']:<8.2f} | {r['canvas_avg_ms']:<8.2f} | {r['fps']:<6.1f}"
        )
    print("=" * 88)


if __name__ == "__main__":
    main()
