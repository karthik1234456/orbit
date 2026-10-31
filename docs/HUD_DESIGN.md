# ORBIT Sci-Fi HUD Interface Design Specification

Version: 2.23.0  
Date: 2026-10-31  
Status: Approved Reference Implementation

## 1. Overview & Architectural Goals

The ORBIT Heads-Up Display (HUD) provides a sci-fi desktop visualization interface inspired by high-tech holographic telemetry displays. The system is designed to run efficiently on standard desktop hardware using a pure Tkinter Canvas rendering pipeline without external GPU shader dependencies.

### Core Principles
1. **Mathematical Single Source of Truth**: All layout coordinates, radii, and bounding envelopes are derived analytically in `layout(W, H) -> HudLayout`. Zero magic numbers exist in rendering routines.
2. **Zero Runtime Allocation & Churn**: All canvas items (polygons, lines, texts, ovals) are pre-allocated into static item pools during initialization. Per-frame rendering updates coordinates and state in-place without deleting or allocating items.
3. **Adaptive Quality & Motion Governance**: Frame rates and particle counts automatically scale across governor tiers (`high`, `medium`, `low`, `static`) and motion modes (`full`, `reduced`, `off`) to maintain 30 FPS responsiveness.
4. **Usable Desktop Ergonomics**: Panels never overlap the central telemetry orb at any window dimension or aspect ratio. If the window dimensions constrain the 260 px panel width floor, the orb scales down automatically.

---

## 2. Reference Geometry Table

All values are derived relative to window width $W$, window height $H$, and dynamic clearance scale factor $S = \text{scale} \le 1.0$:

Orb Centre:
$$\begin{aligned}
c_x &= 0.50 \cdot W \\
c_y &= 0.43 \cdot H
\end{aligned}$$

| Component / Metric | Formula | 1100 × 700 ($S = 1.0$) | 1280 × 800 ($S = 1.0$) | 1920 × 1080 ($S = 1.0$) | 1100 × 900 ($S \approx 0.8781$) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Clearance Scale Factor** $S$ | $\min\left(1.0, \frac{c_x - 12 - x_{\text{chat,left}} - 260}{0.31 \cdot H}\right)$ | 1.0000 | 1.0000 | 1.0000 | 0.8781 |
| **OrbLayer Box Size** | $0.74 \cdot H \cdot S$ | 518.0 px | 592.0 px | 799.2 px | 584.8 px |
| **Sphere Radius** $R_{\text{sph}}$ | $0.21 \cdot H \cdot S$ | 147.0 px | 168.0 px | 226.8 px | 166.0 px |
| **Core Radii (5 tiers)** | $[0.12, 0.20, 0.30, 0.42, 0.55] \cdot R_{\text{sph}}$ | 17.6, 29.4, 44.1, 61.7, 80.9 px | 20.2, 33.6, 50.4, 70.6, 92.4 px | 27.2, 45.4, 68.0, 95.3, 124.7 px | 19.9, 33.2, 49.8, 69.7, 91.3 px |
| **Inner Ring Radius** | $0.262 \cdot H \cdot S$ | 183.4 px | 209.6 px | 283.0 px | 207.1 px |
| **Outer Ring Radius** | $0.271 \cdot H \cdot S$ | 189.7 px | 216.8 px | 292.7 px | 214.2 px |
| **Minor Tick Length** | $0.006 \cdot H \cdot S$ (every 3°) | 4.2 px | 4.8 px | 6.5 px | 4.7 px |
| **Major Tick Length** | $0.012 \cdot H \cdot S$ (every 15°) | 8.4 px | 9.6 px | 13.0 px | 9.5 px |
| **Segmented Arc Band** | $[0.285, 0.310] \cdot H \cdot S$ | 199.5 – 217.0 px | 228.0 – 248.0 px | 307.8 – 334.8 px | 225.0 – 245.0 px |
| **Dust Ring Semi-Major** | $0.310 \cdot H \cdot S$ | 217.0 px | 248.0 px | 334.8 px | 245.0 px |
| **Dust Ring Semi-Minor** | $0.100 \cdot H \cdot S$ | 70.0 px | 80.0 px | 108.0 px | 79.0 px |
| **Title Y ("ORBIT")** | $0.045 \cdot H$ | 31.5 px | 36.0 px | 48.6 px | 40.5 px |
| **Title Rule Y & Width** | $y = 0.068 \cdot H, w = 0.24 \cdot W$ | $y = 47.6\text{ px}, w = 264.0\text{ px}$ | $y = 54.4\text{ px}, w = 307.2\text{ px}$ | $y = 73.4\text{ px}, w = 460.8\text{ px}$ | $y = 61.2\text{ px}, w = 264.0\text{ px}$ |
| **State Label Y** | $0.760 \cdot H$ | 532.0 px | 608.0 px | 820.8 px | 684.0 px |
| **Dock Y & X Positions** | $y = 0.880 \cdot H, x = [0.357, 0.422, 0.500, 0.578, 0.643] \cdot W$ | $y = 616.0\text{ px}$ | $y = 704.0\text{ px}$ | $y = 950.4\text{ px}$ | $y = 792.0\text{ px}$ |
| **Dock Side / Mic Radius** | $0.038 \cdot H\text{ (side)}, 0.060 \cdot H\text{ (mic)}$ | 26.6 / 42.0 px | 30.4 / 48.0 px | 41.0 / 64.8 px | 34.2 / 54.0 px |
| **Panel Width** | $\min(0.262 \cdot W, c_x - R_{\text{arc,out}} - 12\text{ px}), \ge 260\text{ px}$ | 288.0 px | 335.4 px | 503.0 px | 260.0 px |
| **Chat Panel X Span** | $[0.030 \cdot W, 0.030 \cdot W + \text{width}]$ | [33.0, 321.0] px | [38.4, 373.8] px | [57.6, 560.6] px | [33.0, 293.0] px |
| **Right Panel X Span** | $[0.970 \cdot W - \text{width}, 0.970 \cdot W]$ | [779.0, 1067.0] px | [906.2, 1241.6] px | [1359.4, 1862.4] px | [807.0, 1067.0] px |
| **Panel Y Span (Normal)** | $[0.665 \cdot H, 0.920 \cdot H]$ | [465.5, 644.0] px | [532.0, 736.0] px | [718.2, 993.6] px | [598.5, 828.0] px |
| **Chat Y Span (Expanded)** | $[0.300 \cdot H, 0.920 \cdot H]$ | [210.0, 644.0] px | [240.0, 736.0] px | [324.0, 993.6] px | [270.0, 828.0] px |
| **Bottom Chips Baseline Y** | $0.967 \cdot H$ | 676.9 px | 773.6 px | 1044.4 px | 870.3 px |

---

## 3. Visual Components & Styling Specifications

### 3.1 Wireframe Sphere & Axial Tilt
- **Geometry**: 16 meridians (longitude arcs every 22.5°) and 11 parallels of latitude (every 15° between -75° and +75°).
- **Orientation**: Projected with an 18° axial tilt toward the viewer ($\text{tilt}_x = 18^\circ$, $\text{tilt}_y = 0^\circ$). This orientation reveals the north polar meridian convergence fan clearly in the upper hemisphere.
- **Stroke & Shading**: 1 px champagne hairline strokes. Depth cues are achieved by dimming segments behind the projection plane ($z < 0$) toward the deep background palette.
- **Limb**: The outer circumference has a crisp 2 px bright ring surrounded by 3 stepped glow rings with wider strokes and darker amber shades.

### 3.2 Concentric Core Radii & Gradient
The center core features 5 concentric circles with stepped radial falls:
1. $r = 0.12 \cdot R_{\text{sph}}$: `#FADEA4` (bright warm center)
2. $r = 0.20 \cdot R_{\text{sph}}$: `#F2D8A1`
3. $r = 0.30 \cdot R_{\text{sph}}$: `#E6CC9A`
4. $r = 0.42 \cdot R_{\text{sph}}$: `#C3A668`
5. $r = 0.55 \cdot R_{\text{sph}}$: `#B29C71` (outer halo transitioning into `#0E0F13`)

### 3.3 Segmented Arc Bands
- **Band**: Outer radii $0.285 \cdot H$ to $0.310 \cdot H$.
- **Blocks & Spacing**: 3.2° solid block width separated by 0.8° angular gaps (4.0° block pitch).
  - **Arc A**: 92° to 188° (24 blocks).
  - **Arc B**: +8° to -72° (20 blocks).
- **Brightness Profile**: Bell-shaped brightness peaking at 55% of the angular arc length, interpolating between bright champagne `#F2D8A1` and dim bronze `#7E6C48`.
- **Idle Drift**: Arc A drifts counter-clockwise at $+3^\circ/\text{s}$, Arc B drifts clockwise at $-2^\circ/\text{s}$.
- **Unlit Blocks**: Always rendered as visible ghosts blended with 8% accent color.
- **Faint Rim**: Faint outer circular rim line rendered underneath the arc band.

### 3.4 Tilted Dust Ring & Depth Occlusion
- **Geometry**: Ellipse tilted $-22^\circ$ in the screen plane with an orbital plane inclination of approximately $70^\circ$ (semi-major axis $0.31 \cdot H$, semi-minor axis $0.10 \cdot H$).
- **Particles**: 600 particles on tier high, 300 on medium, 150 on low. Dual elliptical rings (primary and secondary faint ring at half density). 1 px size.
- **Depth-Correct Z-Ordering**:
  - Particles and ellipse points with $z \ge 0$ (front half) are drawn in front of the sphere wireframe.
  - Particles and ellipse points with $z < 0$ (back half) are drawn behind the sphere core and wireframe, and are geometrically occluded when their projected $(x, y)$ coordinates fall within $R_{\text{sph}}$.
- **Orbit Speed**: $6^\circ/\text{s}$.

### 3.5 Typography & Bottom Pills
- **Title**: "ORBIT" positioned at $y = 0.045 \cdot H$, rendered per-character with symmetric horizontal letter-spacing (approx. 0.5 em tracking) using light geometric typography (`Bahnschrift Light` / `Segoe UI Light`). Hairline rule underneath at $y = 0.068 \cdot H$, width $0.24 \cdot W$.
- **State Label**: Located at $y = 0.760 \cdot H$ below the orb (`STANDBY`, `LISTENING`, `PROCESSING`, `SPEAKING`, `AWAITING APPROVAL`, `FAULT`) with 0.4 em tracking.
- **Bottom Status Line**: Hairline rule at $y = 0.967 \cdot H$ with two centered pill chips:
  - Left pill: Mode identifier (e.g. `LOCAL qwen3:8b`) with `#F4DDAA` background and `#181510` dark text.
  - Right pill: System health / doctor status with `#836F47` background and `#181510` dark text.

---

## 4. Animation & Pulse Dynamics

### 4.1 Pulse Envelope Function
A normalized envelope evaluated as a function of time $t$ from trigger:
$$\text{Envelope}(t) = \begin{cases}
0.0 & t \le 0 \\
\frac{t}{t_{\text{attack}}} & 0 < t \le t_{\text{attack}} \\
1.0 - \frac{t - t_{\text{attack}}}{t_{\text{decay}}} & t_{\text{attack}} < t \le t_{\text{attack}} + t_{\text{decay}} \\
0.0 & t > t_{\text{attack}} + t_{\text{decay}}
\end{cases}$$
Where $t_{\text{attack}} = 0.25\text{ s}$ and $t_{\text{decay}} = 0.80\text{ s}$.

Key evaluation checkpoints:
- $t = 0.00\text{ s} \implies 0.0$
- $t = 0.25\text{ s} \implies 1.0$ (exact peak)
- $t = 0.50\text{ s} \implies 0.6875$
- $t = 1.00\text{ s} \implies 0.0625$
- $t \ge 1.05\text{ s} \implies 0.0$

### 4.2 Pulse Visual Effects
- **Core & Halo**: Concentric circles step up in brightness by 20% during peak pulse.
- **Equaliser Bar Stretching**: Segmented arc blocks stretch radially:
  $$\text{length}_i = \text{base} + \text{pulse} \cdot \text{gain} \cdot w_i$$
  where $w_i \in [0.4, 1.0]$ is a fixed per-block deterministic weight, producing an equaliser-like flare.
- **Dust Ring Expansion**: Dust ring radius dilates smoothly during peak pulse, then relaxes back to the elliptical orbit.
- **Pulse Drivers**:
  - Model reply: Soft pulse (amplitude 0.50).
  - Task completion: Mild pulse (amplitude 0.45, blocks thicken rather than stretch).
  - Microphone level: Drives listening state arcs with smoothed audio level ($80\text{ ms}$ attack, $400\text{ ms}$ decay).
  - Speaking: Continuous gentle 3 Hz pulsation.
  - Idle heartbeat: Optional 25% pulse every 8 s when `UI_MOTION == "full"`.

---

## 5. Deliberately Uncopied Reference Artifacts

In translating the artistic visual concept into a clean, professional, and accessible production desktop application, the following items from the visual concept art were deliberately excluded:

1. **Gemini Sparkle Watermark**: The promotional AI generation watermark icon in the lower-right corner is omitted.
2. **Garbled Decorative Glyphs ("U" / "UI")**: Non-semantic random lettering and garbled UI runes that appear as decorative filler in concept art are omitted in favor of genuine semantic indicators (`ORBIT`, status labels, system telemetry).
3. **Static Dust Specks & Compression Noise**: Video compression artifacts, static speckling, and non-orbital visual noise are excluded. The dust ring consists strictly of clean, mathematically tracked orbital particles.
4. **Trademarked Logos and Characters**: No trademarked helmets, fictional AI monikers, or proprietary character assets are used. The interface is branded strictly and consistently as **ORBIT**.
5. **Low Contrast Text Elements**: Unreadable low-contrast gold-on-gray decorative text is upgraded to high-contrast palette values ensuring strict WCAG 2.1 AA (≥ 4.5:1) compliance.

---

## 6. HUD Visual Defects Resolution (Part A)

### 6.1 Dock Canvas Sizing Formula
To prevent bottom clipping of the central microphone disc ($r_{\text{mic}} = 0.060 H$) and side action icons ($r_{\text{side}} = 0.038 H$) across high-DPI scaling (1.0, 1.25, 1.5) and window resolutions (1100x700, 1280x800, 1920x1080), the dock canvas height is calculated analytically from the layout:
$$dock\_height = 2 \cdot (r_{\text{mic}} + dock\_margin)$$
where $dock\_margin = 10 \cdot \text{scaling}$. Sizing from `layout(W, H, scaling)` ensures that every dock element's bounding box lies strictly within $[0, 0, dock\_width, dock\_height]$ with zero clipping.

### 6.2 Concentric Core Largest-First Z-Ordering
The 5 concentric core tiers are rendered in descending radius order (largest-first):
- Index 0 ($r = 0.55 \cdot r_s$, `#B29C71`) at the bottom
- Index 1 ($r = 0.42 \cdot r_s$)
- Index 2 ($r = 0.30 \cdot r_s$)
- Index 3 ($r = 0.20 \cdot r_s$)
- Index 4 ($r = 0.12 \cdot r_s$, `#FADEA4`) on top
This largest-first ordering preserves monotonic luminance stepping from the dark amber halo down to the bright cream core. On `low` and `static` governor tiers, the core preserves 3 concentric tiers rather than collapsing to a flat single disc.

### 6.3 Orthographic Wireframe Projection
To ensure meridian and latitude curves never protrude past the sphere limb ($r_s$), an orthographic projection is used:
$$\begin{aligned}
x_{\text{proj}} &= c_x + x \cdot r_s \\
y_{\text{proj}} &= c_y + y \cdot r_s \\
z_{\text{proj}} &= z
\end{aligned}$$
Because points on the unit sphere satisfy $x^2 + y^2 + z^2 = 1$, the projected radial distance $r_{\text{proj}} = \sqrt{(x_{\text{proj}} - c_x)^2 + (y_{\text{proj}} - c_y)^2} = r_s \sqrt{x^2 + y^2} \le r_s$. Projection never overshoots the limb.

### 6.4 Segmented Equaliser Arc Quads
Equaliser arc segments are rendered as true 4-vertex quads (`create_polygon`) spanning the radial band $[0.285 H, 0.310 H]$ with $3.2^\circ$ block width and $0.8^\circ$ gap ($4.0^\circ$ pitch). Arc A comprises 24 blocks ($92^\circ$ to $188^\circ$, drifting $+3^\circ/\text{s}$) and Arc B comprises 20 blocks ($+8^\circ$ to $-72^\circ$, drifting $-2^\circ/\text{s}$), with Gaussian brightness weighting centered at 55% along each arc.

---

## 7. Cinematic Realism & Pure Python Bitmaps (Part B)

### 7.1 Zero-Dependency PPM P6 Encoding
Cinematic gradients, blooms, and glass textures are generated using standard Python 3 and NumPy without PIL, Pillow, OpenCV, or external graphics libraries. Arrays of shape `(H, W, 3)` uint8 are formatted into binary Netpbm PPM (P6) bytes:
```
P6\n{W} {H}\n255\n<raw RGB bytes>
```
and loaded directly into native `tk.PhotoImage(data=ppm_bytes, format="PPM")`.

### 7.2 Layered Background Bitmap
A deterministic (seed 42) background image combines:
- **Radial Vignette**: Darkening from deep charcoal center toward pitch black borders.
- **Accent Haze**: Soft elliptical Gaussian glow centered at $y = 0.55 H$ (7.5% intensity).
- **Perspective Floor Grid**: Floor plane converging toward a horizon at $y = 0.80 H$ (4% intensity).
- **Dock Reflection**: Subtle horizontal glow accentuating the lower dock region.
- **Dither Grain**: High-frequency pseudorandom noise ($\pm 1.5$ levels) eliminating 8-bit banding artifacts.

### 7.3 Bloom Sprites & Translucent Glass Body
- **Bloom Sprites**: Pre-rendered $S = 2.4 \cdot r_s$ square sprites over up to 256 pulse levels containing inner core halo and limb rim bloom with smoothstep falloff. Sprite borders strictly match background pixels to prevent edge seams.
- **Glass Body Sprite**: Pre-rendered sphere disc featuring volumetric absorption darkening toward center, Fresnel rim lightening near limb, and an upper-left specular highlight.

### 7.4 Multi-Plane Cursor Parallax
Ambient dust particles track cursor movement relative to canvas center with smooth damping and continuous slow horizontal drift.

---

## 8. Two-Layer Architecture & Fidelity Overhaul (v2.23.0)

### 8.1 Two-Layer Rendering Architecture
Rendering is decoupled into two independent Tkinter Canvas layers:
1. **StaticLayer (Parent Canvas)**: Covers the full application window. Renders the baked background bitmap, vignette fallback, up to 40 static stars (1 px, $\le 35\%$ accent brightness), panel outlines, double rings, boundary graduation ticks, "ORBIT" title, state label, and bottom chips. It is redrawn **strictly on resize, theme change, or state label change**; it receives zero redraw calls during steady-state animation.
2. **OrbLayer (Child Canvas)**: Positioned via `place()` centered at $(c_x, c_y)$ with an analytical square bounding box of size $0.74 \cdot H \cdot \text{scale}$, with `highlightthickness=0` and `bd=0`. Renders all animated elements: sphere wireframe polylines, pre-rendered core bloom and smoky glass sprites, segmented equaliser arcs, dust ring particles, pulse ripples, and up to 12 foreground stars. Its background is a seamless crop of the parent background bitmap, guaranteed to match border pixels identically.

### 8.2 Frame Pacing & Idle Skipping
- **Dynamic Pacing**: When the state is `IDLE` with no active audio/task pulse and no dragging, the animation ceiling throttles automatically to 15 FPS ($\approx 66\text{ ms}$ delay). When active (`SPEAKING`, `LISTENING`, `THINKING`, or active pulse), it runs at full `HUD_FPS` (default 30 FPS).
- **Frame Skipping**: If angular displacement between successive frames is $< 0.05\text{ px}$ and no color or pulse changes occur, the frame computation returns early.

### 8.3 Depth Color Bucketing
Sphere wireframe polylines quantize depth $z \in [-1.0, 1.0]$ into 6 discrete buckets. Polylines are tagged by depth bucket, and `itemconfigure` is called **only when a polyline crosses into a different bucket**. During continuous rotation, only coordinate updates occur for steady polylines.

### 8.4 Struct-of-Arrays Dust Ring (`DustRingSoA`)
Dust particles are represented in a Struct-of-Arrays layout:
- 600 particles on tier `high`, 300 on `medium`, 150 on `low`.
- Vectorized 3D rotation and projection with NumPy when available, with a pure-Python fallback strictly verified to match within $0.01\text{ px}$.
- Coordinate updates to Tk canvas items are gated by displacement thresholding ($\ge 1.4\text{ px}$), ensuring that only particles crossing integer boundaries are touched. This bounds per-frame touched items to $\le 450$ on high tier.

### 8.5 Performance Telemetry Targets
Per-frame performance is tracked in rolling history buffers:
- `compute_ms`: Pure mathematical simulation and projection time.
- `canvas_ms`: Time spent in Tkinter widget and canvas calls.
- `items_touched`: Count of canvas items configured or repositioned in each frame.

Telemetry rolling averages are exported under `=== HUD ===` in `/diag`. Target benchmarks to be measured by the user in their hardware environment:
- **Tier High**: Target $\le 25\text{ ms}$ average frame time at $1280 \times 800$.
- **Tier Medium**: Target $\le 33\text{ ms}$ average frame time at $1920 \times 1080$.

### 8.6 Fidelity & Theme Purity
- **Palette Purity**: Every pixel drawn in a frame derives strictly from the active theme palette ramps. In the `gold` theme, all cyan and teal tones (hue $150^\circ$ to $210^\circ$ with saturation $> 0.20$) are eliminated. In `cyan`, all elements adhere to the cyan/teal ramp. All standard scrollbars use `ttk.Scrollbar` with thin arrowless troughs; zero `tk.Scrollbar` instances exist.
- **Dock Icons**: Thin 1.5 px outline circles with theme strokes (no filled discs, glow only on hover/active), with an 8-tooth gear icon for Settings.
- **Interim Tab Labels**: Tab labels use compact forms (`Act`, `Ctx`, `Mem`, `Docs`, `Rem`, `Rout`, `Run`) with 8pt Segoe UI font to ensure zero tab truncation or clipping at 1100 px window width, with full names displayed in tooltips.
