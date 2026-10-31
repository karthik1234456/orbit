"""Procedural cinematic realism bitmaps and sprites for ORBIT HUD.

Implements:
- Background bitmap generation (radial vignette, elliptical haze, floor grid, grain, dock reflection)
- Background crop helper for OrbLayer seamless border matching
- Pre-rendered smoothstep radial falloff core bloom sprites over up to 256 levels
- Smoky glass body sprite with Fresnel rim and soft specular highlight
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
    - Radial vignette derived from palette.bg
    - Soft elliptical haze behind orb tinted with palette.accent
    - Faint perspective floor grid converging to horizon at y = 0.80 H, accent at ~4%
    - Soft reflection haze under dock (y ~ 0.88 H)
    - Gentle grain (+/- 1.5 levels) preventing color banding
    """
    pal = palette or get_palette("gold")
    W = max(200, min(1920, int(width)))
    H = max(180, min(1080, int(height)))

    cx = 0.50 * W
    cy = 0.43 * H

    bg_edge_rgb = np.array(_hex_to_rgb_tuple(pal.bg), dtype=np.float32)
    # Brighten center subtly for vignette using active palette edge
    if pal.name == "gold":
        bg_center_rgb = np.clip(bg_edge_rgb * 1.5 + np.array([4.0, 4.0, 5.0], dtype=np.float32), 0.0, 255.0)
    elif pal.name == "cyan":
        bg_center_rgb = np.clip(bg_edge_rgb * 1.4 + np.array([2.0, 5.0, 7.0], dtype=np.float32), 0.0, 255.0)
    else:
        bg_center_rgb = np.clip(bg_edge_rgb * 1.35 + 4.0, 0.0, 255.0)

    accent_rgb = np.array(_hex_to_rgb_tuple(pal.accent), dtype=np.float32)

    Y, X = np.ogrid[:H, :W]

    # 1. Radial vignette
    max_diag = math.hypot(cx, cy)
    dist_center = np.hypot(X - cx, Y - cy) / max_diag
    vignette = np.clip(dist_center, 0.0, 1.0)
    vignette = 0.5 * (1.0 - np.cos(vignette * math.pi))
    img = bg_center_rgb[None, None, :] * (1.0 - vignette[:, :, None]) + bg_edge_rgb[None, None, :] * vignette[:, :, None]

    # 2. Soft elliptical haze behind orb (radius 0.55 H)
    haze_rx = 0.55 * H
    haze_ry = 0.48 * H
    haze_dist = np.hypot((X - cx) / haze_rx, (Y - cy) / haze_ry)
    haze_mask = np.clip(1.0 - haze_dist, 0.0, 1.0)
    haze_intensity = (haze_mask ** 2.2) * 0.075
    img += accent_rgb[None, None, :] * haze_intensity[:, :, None]

    # 3. Faint perspective floor grid converging to horizon at y = 0.80 H
    horizon_y = 0.80 * H
    floor_mask = Y > horizon_y
    v = np.maximum(0.0, (Y - horizon_y) / (H - horizon_y))
    depth = v ** 1.6

    dx = X - cx
    dy = np.maximum(1.0, Y - horizon_y)
    angles = np.arctan2(dy, dx)
    ray_freq = 24.0
    ray_pattern = np.cos(angles * ray_freq)
    ray_lines = np.clip((ray_pattern - 0.85) / 0.15, 0.0, 1.0)

    rung_pattern = np.cos(depth * 32.0 * math.pi)
    rung_lines = np.clip((rung_pattern - 0.80) / 0.20, 0.0, 1.0)

    grid_val = np.maximum(ray_lines, rung_lines) * depth * 0.040
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

    img_u8 = np.clip(img, 0.0, 255.0).astype(np.uint8)
    ppm_data = build_ppm_bytes(img_u8)
    return img_u8, ppm_data


def crop_background_bitmap(
    bg_array: np.ndarray,
    cx: float,
    cy: float,
    box_size: int,
) -> tuple[np.ndarray, bytes]:
    """Crop a square region of size box_size centered at (cx, cy) from bg_array.

    Guarantees that border pixels of the crop are 100% identical to the corresponding
    region of the background bitmap for seamless layer composition.
    """
    H, W, _ = bg_array.shape
    S = max(2, int(box_size))
    half_S = S // 2
    x0 = int(round(cx)) - half_S
    y0 = int(round(cy)) - half_S

    crop = np.zeros((S, S, 3), dtype=np.uint8)
    src_x1 = max(0, x0)
    src_y1 = max(0, y0)
    src_x2 = min(W, x0 + S)
    src_y2 = min(H, y0 + S)

    dst_x1 = src_x1 - x0
    dst_y1 = src_y1 - y0
    dst_x2 = dst_x1 + (src_x2 - src_x1)
    dst_y2 = dst_y1 + (src_y2 - src_y1)

    if src_x2 > src_x1 and src_y2 > src_y1:
        crop[dst_y1:dst_y2, dst_x1:dst_x2] = bg_array[src_y1:src_y2, src_x1:src_x2]

    return crop, build_ppm_bytes(crop)


def build_bloom_sprites(
    sphere_radius: float,
    cx: float,
    cy: float,
    bg_array: np.ndarray,
    palette: Optional[Palette] = None,
    num_levels: int = 8,
) -> list[tuple[np.ndarray, bytes]]:
    """Pre-render core bloom sprites with smoothstep radial falloff over num_levels.

    Features (per B2):
    - Bright core radius ~ 0.16 sphere radius (opaque core radius <= 0.20 r_s)
    - Smoothstep monotonic falloff fading strictly to 0.0 at 0.60 sphere radius
    - Zero border leakage: outer sprite border pixels are 100% identical to background crop
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

    # Core profile: bright opaque core up to r_s * 0.16, smoothstep falloff to 0 at r_s * 0.60
    r_core = 0.16 * r_s
    r_halo = 0.60 * r_s

    # Normalized distance in transition band [r_core, r_halo]
    # For dist <= r_core: 1.0
    # For dist >= r_halo: 0.0
    # Between: smoothstep 3*t^2 - 2*t^3 where t = (r_halo - dist) / (r_halo - r_core)
    t = np.clip((r_halo - dist) / max(1e-5, r_halo - r_core), 0.0, 1.0)
    smoothstep = t * t * (3.0 - 2.0 * t)
    total_glow_profile = np.where(dist <= r_core, 1.0, smoothstep)
    total_glow_profile = np.where(dist < r_halo, total_glow_profile, 0.0)

    accent_rgb = np.array(_hex_to_rgb_tuple(pal.accent), dtype=np.float32)
    # Bright center tinted with accent and text color
    text_rgb = np.array(_hex_to_rgb_tuple(pal.text), dtype=np.float32)

    levels = max(1, int(num_levels))
    sprites: list[tuple[np.ndarray, bytes]] = []

    for level_idx in range(levels):
        frac = level_idx / max(1, levels - 1) if levels > 1 else 0.5
        level_intensity = 0.35 + 0.65 * frac
        # Core blend: inner 0.16 core mixes toward white/text_rgb
        inner_t = np.clip(1.0 - (dist / max(1e-5, r_core)), 0.0, 1.0)
        glow_color = (
            accent_rgb[None, None, :] * (1.0 - 0.45 * inner_t[:, :, None])
            + text_rgb[None, None, :] * (0.45 * inner_t[:, :, None])
        )

        level_glow = total_glow_profile * level_intensity
        sprite_f = bg_crop * (1.0 - level_glow[:, :, None]) + glow_color * level_glow[:, :, None]
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
    """Pre-render translucent smoky glass sphere body.

    Features (per B2):
    - Smoky glass body (gold theme: dark amber, darkest in center; cyan theme: dark teal)
    - Faint Fresnel rim brightening near limb (r -> r_s)
    - Soft specular highlight at upper left
    - Reaches strictly 0 outside r_s with smooth anti-aliasing; border is identical to background
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

    edge_width = 1.5
    inside_mask = np.clip((r_s - dist) / edge_width, 0.0, 1.0)

    # Theme-pure smoky glass tint
    if pal.name == "gold":
        # Dark amber, darkest in center
        smoke_center_rgb = np.array([28.0, 22.0, 14.0], dtype=np.float32)
        smoke_rim_rgb = np.array([45.0, 36.0, 22.0], dtype=np.float32)
    elif pal.name == "cyan":
        # Dark teal, darkest in center
        smoke_center_rgb = np.array([8.0, 22.0, 28.0], dtype=np.float32)
        smoke_rim_rgb = np.array([14.0, 36.0, 46.0], dtype=np.float32)
    else:
        smoke_center_rgb = np.array([18.0, 20.0, 24.0], dtype=np.float32)
        smoke_rim_rgb = np.array([32.0, 35.0, 42.0], dtype=np.float32)

    # 1. Internal absorption / smoky body (darkest in center)
    norm_dist = np.clip(dist / r_s, 0.0, 1.0)
    center_weight = (1.0 - norm_dist ** 2.0)[:, :, None]
    smoky_body = smoke_center_rgb[None, None, :] * center_weight + smoke_rim_rgb[None, None, :] * (1.0 - center_weight)
    absorption = (1.0 - norm_dist ** 1.8) * 0.45
    darkened = bg_crop * (1.0 - absorption[:, :, None]) + smoky_body * (absorption[:, :, None] * 0.6)

    # 2. Fresnel rim brightening near limb (r ~ 0.82 r_s to r_s)
    fresnel = np.clip((dist - 0.80 * r_s) / (0.20 * r_s), 0.0, 1.0) ** 2.2 * 0.28
    accent_rgb = np.array(_hex_to_rgb_tuple(pal.accent), dtype=np.float32)

    # 3. Specular highlight at upper left (offset by (-0.35 r_s, -0.35 r_s))
    spec_cx = half_S - 0.35 * r_s
    spec_cy = half_S - 0.35 * r_s
    spec_dist = np.hypot(X - spec_cx, Y - spec_cy) / (0.28 * r_s)
    specular = np.clip(1.0 - spec_dist, 0.0, 1.0) ** 3.0 * 0.38
    # Highlight tinted by theme text color
    spec_rgb = np.array(_hex_to_rgb_tuple(pal.text), dtype=np.float32)

    glass = (
        darkened
        + accent_rgb[None, None, :] * fresnel[:, :, None]
        + spec_rgb[None, None, :] * specular[:, :, None]
    )

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

    total = bg_bytes + (num_levels + 1) * sprite_bytes
    return total


