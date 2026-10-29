"""Performance benchmark tool for the ORBIT sci-fi HUD rendering pipeline.

Measures frame rendering durations across all 4 fidelity tiers:
- high (400 particles, full glow, full wireframe)
- medium (200 particles, glow, full wireframe)
- low (100 particles, no glow, reduced wireframe)
- static (0 particles, static frame)

Collects p95, average, and worst-case frame times over 5 seconds per tier
and outputs a performance summary with a recommended HUD_FPS setting.
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


def run_tier_benchmark(
    root: tk.Tk,
    tier_name: str,
    duration_s: float = 5.0,
    width: int = 400,
    height: int = 240,
) -> dict[str, Any]:
    """Execute frame rendering loop for duration_s and compute timing percentiles."""
    model = HudModel()
    # Force tier
    tier_idx = HudModel().governor.TIERS.index(tier_name)
    model.governor._current_tier_idx = tier_idx

    palette = get_palette("cyan")
    canvas = HudCanvas(root, model=model, palette=palette, fps=60, motion="full", width=width, height=height)
    canvas.pack()
    root.update()

    frame_durations: list[float] = []
    start_bench = time.monotonic()
    dt = 1.0 / 30.0

    while time.monotonic() - start_bench < duration_s:
        t0 = time.monotonic()
        model.update(dt)
        canvas._draw_frame(dt)
        root.update_idletasks()
        t1 = time.monotonic()

        frame_durations.append(t1 - t0)

    canvas.destroy()

    if not frame_durations:
        return {
            "tier": tier_name,
            "frames": 0,
            "avg_ms": 0.0,
            "p95_ms": 0.0,
            "worst_ms": 0.0,
            "fps": 0.0,
        }

    frame_durations.sort()
    n = len(frame_durations)
    avg_s = sum(frame_durations) / n
    p95_s = frame_durations[int(n * 0.95)] if n > 1 else frame_durations[-1]
    worst_s = frame_durations[-1]
    achieved_fps = n / duration_s

    return {
        "tier": tier_name,
        "frames": n,
        "avg_ms": avg_s * 1000.0,
        "p95_ms": p95_s * 1000.0,
        "worst_ms": worst_s * 1000.0,
        "fps": achieved_fps,
    }


def main() -> None:
    print("=== ORBIT HUD RENDERING BENCHMARK ===")
    print("Benchmarking 4 fidelity tiers (5.0s per tier)...\n")

    try:
        root = tk.Tk()
        root.withdraw()  # Off-screen hidden window
    except Exception as exc:
        print(f"Error: Tkinter initialization failed: {exc}", file=sys.stderr)
        sys.exit(1)

    tiers = ("high", "medium", "low", "static")
    results: list[dict[str, Any]] = []

    try:
        for t in tiers:
            print(f"Testing tier '{t}'... ", end="", flush=True)
            res = run_tier_benchmark(root, t, duration_s=5.0)
            results.append(res)
            print(f"done ({res['frames']} frames, avg {res['avg_ms']:.2f}ms, p95 {res['p95_ms']:.2f}ms)")
    finally:
        try:
            root.destroy()
        except Exception:
            pass

    print("\n" + "=" * 65)
    print(f"{'Tier':<10} | {'Frames':<8} | {'Avg (ms)':<10} | {'p95 (ms)':<10} | {'Worst (ms)':<10} | {'FPS':<6}")
    print("-" * 65)
    for r in results:
        print(
            f"{r['tier']:<10} | {r['frames']:<8} | {r['avg_ms']:<10.2f} | "
            f"{r['p95_ms']:<10.2f} | {r['worst_ms']:<10.2f} | {r['fps']:<6.1f}"
        )
    print("=" * 65)

    high_res = results[0] if results else None
    if high_res:
        p95 = high_res["p95_ms"]
        if p95 < 25.0:
            rec = "30 (Recommended default - performance excellent)"
        elif p95 < 35.0:
            rec = "24 (Recommended - smooth playback with lower CPU load)"
        else:
            rec = "15 (Recommended - resource-constrained host)"
        print(f"\nRecommended HUD_FPS setting: {rec}")
    print("=" * 65)


if __name__ == "__main__":
    main()
