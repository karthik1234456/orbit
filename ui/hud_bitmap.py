"""Procedural cinematic realism bitmaps and sprites for ORBIT HUD.

Implements:
- Background bitmap generation (radial vignette, elliptical haze, floor grid, grain, dock reflection)
- Pre-rendered bloom sprites for core halo and limb glow
- Translucent sphere glass body with Fresnel rim and specular highlight
- Pure numpy array computations converted to standard PPM ("P6") byte format for Tkinter PhotoImage
- Deterministic rendering (fixed seed)
- Strict boundary matching: sprite border pixels exactly match background pixels
- Zero new runtime dependencies (uses numpy and standard library)
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

from ui.theme import Palette, get_palette


def _hex_to_rgb_tuple(hex_code: str) -> tuple[float, float, float]:
    """Convert hex string '#RRGGBB' to (R, G, B) float tuple in [0.0, 255.0]."""
    hex_code = hex_code.strip().lstrip("#")
    if len(hex_code) == 6:
        r = int(hex_code[0:2], 16)
        g = int(hex_code[2:4], 16)
        b = int(hex_code[4:6], 16)
        return float(r), float(g), float(b)
    return 0.0, 0.0, 0.0


def build_ppm_bytes(rgb_array: np.ndarray) -> bytes:
    """Pack an (H, W, 3) uint8 numpy array into PPM P6 binary format."""
    h, w, c = rgb_array.shape
    header = f"P6\n{w} {h}\n255\n".encode("ascii")
    return header + rgb_array.tobytes()


def build_background_bitmap(
    width: int,
    height: int,
    palette: Optional[Palette] = None,
    seed: int = 42,
) -> tuple[np.ndarray, bytes]:
    """Generate deterministic background bitmap.

    Features:
    - Radial vignette from center #0E0F13 to edges #08080D (or theme equivalent)
    - Soft elliptical haze behind orb (radius ~0.55 H) tinted with accent at 6-8%
    - Faint perspective floor grid converging to horizon at y = 0.80 H, accent at ~4%
    - Soft reflection haze under dock (y ~ 0.88 H)
    - Gentle grain (+/- 1.5 levels) preventing color banding
    """
    pal = palette or get_palette("gold")
    W = max(200, min(1920, int(width)))
    H = max(180, min(1080, int(height)))

    cx = 0.50 * W
    cy = 0.43 * H

    # Colors
    bg_edge_rgb = np.array(_hex_to_rgb_tuple(pal.bg), dtype=np.float32)
    # Brighten center slightly for vignette
    if pal.name == "gold":
        bg_center_rgb = np.array([14.0, 15.0, 19.0], dtype=np.float32)
    else:
        bg_center_rgb = np.clip(bg_edge_rgb * 1.35 + 4.0, 0.0, 255.0)

    accent_rgb = np.array(_hex_to_rgb_tuple(pal.accent), dtype=np.float32)

    Y, X = np.ogrid[:H, :W]

    # 1. Radial vignette
    max_diag = math.hypot(cx, cy)
    dist_center = np.hypot(X - cx, Y - cy) / max_diag
    vignette = np.clip(dist_center, 0.0, 1.0)
    # Cosine easing for smooth falloff
    vignette = 0.5 * (1.0 - np.cos(vignette * math.pi))
    # Shape (H, W, 3)
    img = bg_center_rgb[None, None, :] * (1.0 - vignette[:, :, None]) + bg_edge_rgb[None, None, :] * vignette[:, :, None]

    # 2. Soft elliptical haze behind orb (radius 0.55 H)
    haze_rx = 0.55 * H
    haze_ry = 0.48 * H
    haze_dist = np.hypot((X - cx) / haze_rx, (Y - cy) / haze_ry)
    haze_mask = np.clip(1.0 - haze_dist, 0.0, 1.0)
    # Smooth bell curve
    haze_intensity = (haze_mask ** 2.2) * 0.075  # ~7.5% intensity
    img += accent_rgb[None, None, :] * haze_intensity[:, :, None]

    # 3. Faint perspective floor grid converging to horizon at y = 0.80 H
    horizon_y = 0.80 * H
    floor_mask = Y > horizon_y
    # Distance factor v in [0.0, 1.0] from horizon to bottom
    v = np.maximum(0.0, (Y - horizon_y) / (H - horizon_y))
    # Non-linear depth factor (lines compress near horizon)
    depth = v ** 1.6

    # Radial perspective rays from vanishing point (cx, horizon_y)
    dx = X - cx
    dy = np.maximum(1.0, Y - horizon_y)
    angles = np.arctan2(dy, dx)  # in radians
    # Ray lines
    ray_freq = 24.0
    ray_pattern = np.cos(angles * ray_freq)
    ray_lines = np.clip((ray_pattern - 0.85) / 0.15, 0.0, 1.0)

    # Horizontal distance grid rungs
    rung_pattern = np.cos(depth * 32.0 * math.pi)
    rung_lines = np.clip((rung_pattern - 0.80) / 0.20, 0.0, 1.0)

    grid_val = np.maximum(ray_lines, rung_lines) * depth * 0.040  # 4% accent
    grid_val = np.where(floor_mask, grid_val, 0.0)
    img += accent_rgb[None, None, :] * grid_val[:, :, None]

    # 4. Soft reflection haze under dock (centered at y = 0.88 H)
    dock_y = 0.88 * H
    dock_rx = 0.22 * W
    dock_ry = 0.06 * H
    dock_dist = np.hypot((X - cx) / dock_rx, (Y - dock_y) / dock_ry)
    dock_mask = np.clip(1.0 - dock_dist, 0.0, 1.0)
    dock_haze = (dock_mask ** 2.0) * 0.045
    img += accent_rgb[None, None, :] * dock_haze[:, :, None]

    # 5. Deterministic gentle grain (+/- 1.5 levels)
    rng = np.random.default_rng(seed)
    grain = rng.uniform(-1.5, 1.5, size=(H, W, 3)).astype(np.float32)
    img += grain

    # Clamp and convert to uint8
    img_u8 = np.clip(img, 0.0, 255.0).astype(np.uint8)
    ppm_data = build_ppm_bytes(img_u8)
    return img_u8, ppm_data


def build_bloom_sprites(
    sphere_radius: float,
    cx: float,
    cy: float,
    bg_array: np.ndarray,
    palette: Optional[Palette] = None,
    num_levels: int = 8,
) -> list[tuple[np.ndarray, bytes]]:
    """Pre-render bloom intensity levels matching the exact background pixels.

    The glow strictly drops to 0.0 before reaching the sprite boundary, ensuring
    border pixels are 100% identical to the background.
    """
    pal = palette or get_palette("gold")
    H, W, _ = bg_array.shape
    r_s = max(10.0, float(sphere_radius))

    # Sprite square size: 2.4 * sphere_radius (even integer)
    S = int(round(2.4 * r_s))
    if S % 2 != 0:
        S += 1

    half_S = S // 2
    x0 = int(round(cx)) - half_S
    y0 = int(round(cy)) - half_S

    # Boundary-safe background crop
    bg_crop = np.zeros((S, S, 3), dtype=np.float32)
    src_x1 = max(0, x0)
    src_y1 = max(0, y0)
    src_x2 = min(W, x0 + S)
    src_y2 = min(H, y0 + S)

    dst_x1 = src_x1 - x0
    dst_y1 = src_y1 - y0
    dst_x2 = dst_x1 + (src_x2 - src_x1)
    dst_y2 = dst_y1 + (src_y2 - src_y1)

    if src_x2 > src_x1 and src_y2 > src_y1:
        bg_crop[dst_y1:dst_y2, dst_x1:dst_x2] = bg_array[src_y1:src_y2, src_x1:src_x2].astype(np.float32)

    Y, X = np.ogrid[:S, :S]
    dist = np.hypot(X - half_S, Y - half_S)

    # Core halo profile: smooth peak at center, decaying outward to r_s * 0.8
    core_mask = np.clip(1.0 - (dist / (r_s * 0.85)), 0.0, 1.0)
    core_glow = (core_mask ** 2.0) * 0.45

    # Limb rim glow profile: peaks at dist == r_s, falls to 0 at r_s * 1.15
    # Strict zero cutoff at max_glow_r = 1.15 * r_s (< half_S = 1.20 * r_s)
    max_glow_r = 1.15 * r_s
    rim_dist_from_limb = np.abs(dist - r_s)
    rim_mask = np.clip(1.0 - (rim_dist_from_limb / (0.22 * r_s)), 0.0, 1.0)
    rim_glow = (rim_mask ** 2.5) * 0.60
    # Zero outside max_glow_r
    rim_glow = np.where(dist < max_glow_r, rim_glow, 0.0)

    total_glow_profile = core_glow + rim_glow
    # Force zero at and beyond max_glow_r
    total_glow_profile = np.where(dist < max_glow_r, total_glow_profile, 0.0)

    accent_rgb = np.array(_hex_to_rgb_tuple(pal.accent), dtype=np.float32)

    levels = max(1, int(num_levels))
    sprites: list[tuple[np.ndarray, bytes]] = []

    for level_idx in range(levels):
        frac = level_idx / max(1, levels - 1) if levels > 1 else 0.5
        # Modulate glow by level
        level_glow = total_glow_profile * (0.3 + 0.7 * frac)
        sprite_f = bg_crop + accent_rgb[None, None, :] * level_glow[:, :, None]
        sprite_u8 = np.clip(sprite_f, 0.0, 255.0).astype(np.uint8)
        ppm = build_ppm_bytes(sprite_u8)
        sprites.append((sprite_u8, ppm))

    return sprites


def build_sphere_body_sprite(
    sphere_radius: float,
    cx: float,
    cy: float,
    bg_array: np.ndarray,
    palette: Optional[Palette] = None,
) -> tuple[np.ndarray, bytes]:
    """Pre-render translucent glass sphere body.

    Features:
    - Subtle internal absorption (darker toward center)
    - Fresnel rim brightening near limb (r -> r_s)
    - Soft specular highlight at upper left
    - Reaches 0 outside r_s with anti-aliasing, leaving border identical to background
    """
    pal = palette or get_palette("gold")
    H, W, _ = bg_array.shape
    r_s = max(10.0, float(sphere_radius))

    S = int(round(2.4 * r_s))
    if S % 2 != 0:
        S += 1

    half_S = S // 2
    x0 = int(round(cx)) - half_S
    y0 = int(round(cy)) - half_S

    bg_crop = np.zeros((S, S, 3), dtype=np.float32)
    src_x1 = max(0, x0)
    src_y1 = max(0, y0)
    src_x2 = min(W, x0 + S)
    src_y2 = min(H, y0 + S)

    dst_x1 = src_x1 - x0
    dst_y1 = src_y1 - y0
    dst_x2 = dst_x1 + (src_x2 - src_x1)
    dst_y2 = dst_y1 + (src_y2 - src_y1)

    if src_x2 > src_x1 and src_y2 > src_y1:
        bg_crop[dst_y1:dst_y2, dst_x1:dst_x2] = bg_array[src_y1:src_y2, src_x1:src_x2].astype(np.float32)

    Y, X = np.ogrid[:S, :S]
    dist = np.hypot(X - half_S, Y - half_S)

    # Sphere mask with 1.5 px smooth anti-aliased edge
    edge_width = 1.5
    inside_mask = np.clip((r_s - dist) / edge_width, 0.0, 1.0)

    # 1. Internal absorption / darkening toward center
    absorption = (1.0 - (dist / r_s) ** 2) * 0.35
    darkened = bg_crop * (1.0 - absorption[:, :, None])

    # 2. Fresnel rim brightening near limb (r ~ 0.85 r_s to r_s)
    fresnel = np.clip((dist - 0.70 * r_s) / (0.30 * r_s), 0.0, 1.0) ** 2.5 * 0.25
    accent_rgb = np.array(_hex_to_rgb_tuple(pal.accent), dtype=np.float32)

    # 3. Specular highlight at upper left (offset by (-0.35 r_s, -0.35 r_s))
    spec_cx = half_S - 0.35 * r_s
    spec_cy = half_S - 0.35 * r_s
    spec_dist = np.hypot(X - spec_cx, Y - spec_cy) / (0.28 * r_s)
    specular = np.clip(1.0 - spec_dist, 0.0, 1.0) ** 3.0 * 0.40
    spec_rgb = np.array([255.0, 250.0, 240.0], dtype=np.float32)

    # Combine effects inside sphere
    glass = (
        darkened
        + accent_rgb[None, None, :] * fresnel[:, :, None]
        + spec_rgb[None, None, :] * specular[:, :, None]
    )

    # Blend with original background via inside_mask
    result = bg_crop * (1.0 - inside_mask[:, :, None]) + glass * inside_mask[:, :, None]

    result_u8 = np.clip(result, 0.0, 255.0).astype(np.uint8)
    ppm = build_ppm_bytes(result_u8)
    return result_u8, ppm


def calculate_image_memory_bytes(width: int, height: int, sphere_radius: float, num_levels: int = 8) -> int:
    """Calculate total memory usage in bytes for background and pre-rendered sprites.

    Budget: At 1920x1080 <= 48 MB.
    """
    W = max(200, min(1920, int(width)))
    H = max(180, min(1080, int(height)))
    bg_bytes = W * H * 3

    S = int(round(2.4 * float(sphere_radius)))
    if S % 2 != 0:
        S += 1
    sprite_bytes = S * S * 3

    # Total: 1 background + num_levels bloom sprites + 1 sphere body sprite
    total = bg_bytes + (num_levels + 1) * sprite_bytes
    return total

