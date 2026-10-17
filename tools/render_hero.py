"""Render the README hero film from real ETABS data (docs/live/demo_data.json, see record_demo_data.py).

Every member, deflection and D/C ratio on screen comes from the live ETABS session; nothing is invented.

    python tools/render_hero.py            # -> docs/assets/hero.webp, hero.mp4, still_*.png
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "live" / "demo_data.json"
OUT = ROOT / "docs" / "assets"
FONTS = Path("C:/Windows/Fonts")
FFMPEG = shutil.which("ffmpeg") or "C:/ffmpeg/ffmpeg.exe"

W, H, SS = 1280, 720, 2          # output size, supersampling for the line work
FPS = 24
DURATION = 15.0

BG = np.array([4, 6, 12], np.float32)
INK = (236, 240, 248)
MUTED = (128, 140, 162)
DIM = (70, 80, 100)
STEEL = np.array([168, 186, 255], np.float32)     # unpainted structure
SCAN = (120, 200, 255)

# D/C ratio palette: calm teal -> sky -> amber -> coral -> crimson.
DC_STOPS = [(0.0, (34, 211, 197)), (0.45, (56, 189, 248)), (0.75, (250, 204, 21)),
            (0.95, (251, 146, 60)), (1.05, (244, 63, 94))]


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


F_EYEBROW = font("seguisb.ttf", 13)
F_TITLE = font("segoeuisl.ttf", 34)
F_BODY = font("segoeui.ttf", 15)
F_MONO = font("CascadiaMono.ttf", 14)
F_STAT_N = font("segoeuisl.ttf", 40)
F_STAT_L = font("segoeui.ttf", 12)
F_HERO = font("segoeuisl.ttf", 72)
F_HERO_S = font("segoeuil.ttf", 22)


# --------------------------------------------------------------------------- easing / colour

def clamp(v: float, a: float = 0.0, b: float = 1.0) -> float:
    return a if v < a else b if v > b else v


def ease(t: float) -> float:
    t = clamp(t)
    return t * t * t * (t * (6 * t - 15) + 10)          # smootherstep


def ease_out(t: float) -> float:
    t = clamp(t)
    return 1 - (1 - t) ** 3


def seg(t: float, a: float, b: float) -> float:
    return clamp((t - a) / (b - a))


def dc_color(r: float | None) -> np.ndarray:
    if r is None:
        return STEEL
    for (p0, c0), (p1, c1) in zip(DC_STOPS, DC_STOPS[1:]):
        if r <= p1:
            k = clamp((r - p0) / (p1 - p0))
            return np.array(c0, np.float32) * (1 - k) + np.array(c1, np.float32) * k
    return np.array(DC_STOPS[-1][1], np.float32)


def lerp(a, b, k):
    return a * (1 - k) + b * k


# --------------------------------------------------------------------------- camera

class Camera:
    def __init__(self, target: np.ndarray):
        self.target = target

    def setup(self, azimuth: float, elevation: float, distance: float, fov: float = 34.0) -> None:
        eye = self.target + distance * np.array([
            math.cos(elevation) * math.cos(azimuth),
            math.cos(elevation) * math.sin(azimuth),
            math.sin(elevation)])
        fwd = self.target - eye
        fwd /= np.linalg.norm(fwd)
        right = np.cross(fwd, [0, 0, 1.0])
        right /= np.linalg.norm(right)
        up = np.cross(right, fwd)
        self.eye, self.fwd, self.right, self.up = eye, fwd, right, up
        self.f = (H * SS / 2) / math.tan(math.radians(fov) / 2)

    def project(self, p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        d = p - self.eye
        z = d @ self.fwd
        x = d @ self.right
        y = d @ self.up
        sx = W * SS / 2 + self.f * x / z
        sy = H * SS / 2 - self.f * y / z
        return np.stack([sx, sy], -1), z


# --------------------------------------------------------------------------- scene

class Scene:
    def __init__(self, data: dict):
        self.data = data
        fr = data["frames"]
        self.a = np.array([f["start"] for f in fr], np.float32)
        self.b = np.array([f["end"] for f in fr], np.float32)
        self.r1 = [f["ratio"] for f in fr]
        self.r2 = [f["ratio_v2"] for f in fr]
        self.upsized = np.array([f["section_v2"] != f["section"] for f in fr])
        self.vertical = np.abs(self.a[:, 2] - self.b[:, 2]) > 1e-6
        self.is_col = self.vertical & (np.abs(self.a[:, 0] - self.b[:, 0]) < 1e-6) & \
            (np.abs(self.a[:, 1] - self.b[:, 1]) < 1e-6)
        self.levels = sorted({round(z, 3) for z in np.concatenate([self.a[:, 2], self.b[:, 2]])})
        self.top = max(self.levels)
        self.story = np.array([self.levels.index(round(max(p, q), 3)) - 1 for p, q in zip(self.a[:, 2], self.b[:, 2])])
        self.slabs = [np.array(s, np.float32) for s in data["slabs"]]
        self.slab_story = [self.levels.index(round(float(s[0, 2]), 3)) - 1 for s in self.slabs]
        self.n_story = len(self.levels) - 1
        # Wind displacement field (real joint results), interpolated by coordinate.
        pts = data["points"]
        disp = data["displacement_wx"]
        self.disp_lookup = {tuple(np.round(pts[k], 3)): np.array(v, np.float32) for k, v in disp.items() if k in pts}
        roof = max(abs(v[0]) for v in self.disp_lookup.values())
        self.disp_scale = 0.9 / roof            # roof sways ~0.9 m on screen: visible, still elegant
        self.da = np.array([self.disp_lookup.get(tuple(np.round(p, 3)), np.zeros(3)) for p in self.a])
        self.db = np.array([self.disp_lookup.get(tuple(np.round(p, 3)), np.zeros(3)) for p in self.b])
        lo = self.a.min(0)
        hi = self.b.max(0)
        self.center = (lo + hi) / 2
        self.center[2] = self.top * 0.62     # aim above mid-height: the building sits low, under the headline
        self.extent = hi - lo


def ground(draw: ImageDraw.ImageDraw, cam: Camera, scene: Scene, alpha: float) -> None:
    lo = scene.a.min(0) - 14
    hi = scene.b.max(0) + 14
    step = 3.0
    for x in np.arange(math.floor(lo[0] / step) * step, hi[0] + 0.1, step):
        _line_fade(draw, cam, np.array([x, lo[1], 0]), np.array([x, hi[1], 0]), alpha)
    for y in np.arange(math.floor(lo[1] / step) * step, hi[1] + 0.1, step):
        _line_fade(draw, cam, np.array([lo[0], y, 0]), np.array([hi[0], y, 0]), alpha)


def _line_fade(draw, cam, p, q, alpha):
    n = 12
    pts = np.linspace(p, q, n + 1)
    xy, z = cam.project(pts)
    c = cam.target[:2]
    for i in range(n):
        mid = (pts[i] + pts[i + 1]) / 2
        dist = np.linalg.norm(mid[:2] - c)
        a = alpha * clamp(1 - (dist - 10) / 28) * 46
        if a > 1:
            draw.line([tuple(xy[i]), tuple(xy[i + 1])], fill=(90, 120, 180, int(a)), width=SS)


def render_structure(scene: Scene, cam: Camera, t: float) -> tuple[Image.Image, Image.Image]:
    """Returns (line art at SS resolution, glow source at SS resolution)."""
    art = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    glow = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    da, dg = ImageDraw.Draw(art, "RGBA"), ImageDraw.Draw(glow, "RGBA")

    # --- timeline -----------------------------------------------------------------------------
    build = seg(t, 0.8, 5.2)
    sway = seg(t, 6.0, 8.0)
    paint = seg(t, 7.9, 9.2)
    fix = seg(t, 10.4, 12.0)
    outro = seg(t, 12.6, 13.6)

    story_t = build * (scene.n_story + 0.6)
    sway_amp = math.sin(sway * math.pi * 3) * (1 - sway) ** 1.2 * scene.disp_scale if 0 < sway < 1 else 0.0

    # --- slabs (behind), depth sorted ---------------------------------------------------------
    cam_eye = cam.eye
    order = sorted(range(len(scene.slabs)), key=lambda i: -np.linalg.norm(scene.slabs[i].mean(0) - cam_eye))
    for i in order:
        s = scene.slab_story[i]
        k = ease(clamp(story_t - s - 0.75))
        if k <= 0:
            continue
        poly = scene.slabs[i].copy()
        if sway_amp:
            for j in range(len(poly)):
                poly[j] += scene.disp_lookup.get(tuple(np.round(poly[j], 3)), np.zeros(3)) * sway_amp
        xy, _ = cam.project(poly)
        a = int(16 * k * (1 - 0.85 * outro))
        da.polygon([tuple(p) for p in xy], fill=(120, 150, 230, a))

    # --- members ------------------------------------------------------------------------------
    pa = scene.a + scene.da * sway_amp
    pb = scene.b + scene.db * sway_amp
    mid = (pa + pb) / 2
    depth = np.linalg.norm(mid - cam_eye, axis=1)
    dmin, dmax = depth.min(), depth.max()
    for i in np.argsort(-depth):
        s = scene.story[i]
        # Columns grow upward first, then beams and braces of the same story draw in.
        local = story_t - s
        k = clamp(local / 0.55) if scene.is_col[i] else clamp((local - 0.35) / 0.5)
        if k <= 0:
            continue
        k = ease_out(k)
        p, q = pa[i], pb[i]
        if scene.vertical[i] and p[2] > q[2]:
            p, q = q, p
        q = p + (q - p) * k

        col = STEEL.copy()
        if paint > 0:
            # Paint sweeps upward story by story with the real design ratios.
            pk = ease(clamp(paint * (scene.n_story + 2) - s))
            col = lerp(col, dc_color(scene.r1[i]), pk)
        if fix > 0 and scene.upsized[i]:
            col = lerp(col, dc_color(scene.r2[i]), ease(fix))
        elif fix > 0:
            col = lerp(col, dc_color(scene.r2[i]), ease(fix) * 0.6)

        fog = 1 - 0.55 * (depth[i] - dmin) / (dmax - dmin + 1e-6)
        width = SS * (2.1 if scene.is_col[i] else 1.5)
        failing = (scene.r1[i] or 0) > 1.0
        if failing and paint >= 1 and fix < 0.5:
            pulse = 0.5 + 0.5 * math.sin(t * 9)
            width += SS * 1.4 * pulse
        if scene.upsized[i] and fix > 0:
            width += SS * 0.8 * ease(fix)
        alpha = int(255 * fog * (1 - 0.82 * outro))
        c = tuple(int(v) for v in col) + (alpha,)
        xy, _ = cam.project(np.array([p, q]))
        da.line([tuple(xy[0]), tuple(xy[1])], fill=c, width=max(1, int(round(width))))
        glow_a = int((150 if failing and paint >= 1 and fix < 1 else 90) * fog * (1 - outro))
        dg.line([tuple(xy[0]), tuple(xy[1])], fill=tuple(int(v) for v in col) + (glow_a,),
                width=int(width * 2.4))

    # --- analysis scan plane ------------------------------------------------------------------
    scan = seg(t, 5.25, 6.4)
    if 0 < scan < 1:
        z = ease(scan) * (scene.top + 1.0)
        lo, hi = scene.a.min(0) - 2, scene.b.max(0) + 2
        quad = np.array([[lo[0], lo[1], z], [hi[0], lo[1], z], [hi[0], hi[1], z], [lo[0], hi[1], z]])
        xy, _ = cam.project(quad)
        fade = math.sin(scan * math.pi)
        dg.polygon([tuple(p) for p in xy], fill=(*SCAN, int(70 * fade)))
        da.line([tuple(p) for p in xy] + [tuple(xy[0])], fill=(*SCAN, int(220 * fade)), width=SS * 2)
    return art, glow


# --------------------------------------------------------------------------- overlay typography

CHAPTERS = [
    (0.6, 5.2, "01", "MODEL", "354 members · 72 slabs · 6 storeys"),
    (5.2, 7.9, "02", "ANALYSE", "Linear static + modal · wind and gravity"),
    (7.9, 10.3, "03", "DESIGN", "AISC 360-16 steel check, member by member"),
    (10.3, 12.6, "04", "ITERATE", "The agent upsizes what failed and re-runs"),
]


def ticker_lines(t: float, s: dict) -> list[tuple[str, str]]:
    seq = [
        (0.9, "etabs_new_model", "grid_only · 6 storeys"),
        (1.6, "etabs_define_frame_section", "W14X132 · W14X90 · W21X50 · W18X35 · W10X33"),
        (2.3, "etabs_add_frames", f"{s['frames']} created"),
        (3.3, "etabs_add_areas", f"{s['slabs']} slabs"),
        (4.2, "etabs_assign_loads", "SDL 1.5 kPa · LL 3.0 kPa · wind"),
        (5.3, "etabs_run_analysis", f"finished · {s['analysis_seconds']:.1f} s"),
        (6.6, "etabs_get_results", f"T₁ {s['t1']:.2f} s · ΣFz(LL) {s['base_fz_ll']:,.0f} kN"),
        (8.0, "etabs_run_design", f"{s['failing']} over capacity · max D/C {s['max_dc']:.2f}"),
        (10.4, "etabs_assign_section", f"W24X76 × {s['upsized']}"),
        (11.0, "etabs_run_analysis", f"finished · {s['analysis2_seconds']:.1f} s"),
        (11.6, "etabs_run_design", f"0 over capacity · max D/C {s['max_dc_v2']:.2f}"),
    ]
    return [(f"{n}", r) for (at, n, r) in seq if t >= at][-3:], [at for at, _, _ in seq if t >= at]


class FadeDraw:
    """ImageDraw whose text() honours the fill's alpha. Pillow ignores text alpha on RGB images, so text is
    rendered into a mask scaled by alpha and composited; shapes go through the normal blending draw."""

    def __init__(self, img: Image.Image):
        self.img = img
        self.draw = ImageDraw.Draw(img, "RGBA")

    def __getattr__(self, name: str):
        return getattr(self.draw, name)

    def text(self, xy, text, font, fill, anchor=None):
        alpha = fill[3] if len(fill) == 4 else 255
        if alpha <= 0:
            return
        mask = Image.new("L", self.img.size, 0)
        ImageDraw.Draw(mask).text(xy, text, font=font, fill=alpha, anchor=anchor)
        self.img.paste(Image.new("RGB", self.img.size, tuple(fill[:3])), (0, 0), mask)


def overlay(img: Image.Image, t: float, s: dict) -> Image.Image:
    d = FadeDraw(img)
    intro = ease(seg(t, 0.0, 0.9))
    outro = ease(seg(t, 12.6, 13.4))
    ui = intro * (1 - outro)

    # top bar
    d.text((48, 40), "etabs-mcp", font=F_EYEBROW, fill=(*INK, int(230 * ui)))
    d.text((48 + d.textlength("etabs-mcp  ", font=F_EYEBROW), 40), "recorded from a live ETABS 23.3 session",
           font=F_EYEBROW, fill=(*MUTED, int(200 * ui)))
    live = 0.55 + 0.45 * math.sin(t * 4)
    d.ellipse((W - 62, 44, W - 54, 52), fill=(74, 222, 128, int(255 * ui * live)))
    d.text((W - 72, 40), "LIVE", font=F_EYEBROW, fill=(*MUTED, int(220 * ui)), anchor="ra")

    # chapter heading
    for a, b, num, name, sub in CHAPTERS:
        if a - 0.2 <= t <= b + 0.2:
            k = ease(seg(t, a, a + 0.5)) * (1 - ease(seg(t, b - 0.3, b + 0.2)))
            dy = (1 - ease(seg(t, a, a + 0.6))) * 14
            d.text((48, 96 + dy), num, font=F_EYEBROW, fill=(120, 200, 255, int(255 * k)))
            d.line((76, 104 + dy, 76 + 28 * k, 104 + dy), fill=(120, 200, 255, int(160 * k)), width=1)
            d.text((112, 96 + dy), name, font=F_EYEBROW, fill=(*INK, int(255 * k)))
            d.text((46, 118 + dy), sub, font=F_TITLE, fill=(*INK, int(245 * k)))

    # tool-call ticker
    lines, _ = ticker_lines(t, s)
    y0 = H - 48 - 26 * len(lines)
    for i, (name, res) in enumerate(lines):
        age = 1.0 if i < len(lines) - 1 else 1.0
        a = (0.45 + 0.55 * (i == len(lines) - 1)) * ui * age
        x = 48
        d.text((x, y0 + 26 * i), "›", font=F_MONO, fill=(120, 200, 255, int(255 * a)))
        d.text((x + 16, y0 + 26 * i), name, font=F_MONO, fill=(*INK, int(255 * a)))
        d.text((x + 16 + d.textlength(name + "  ", font=F_MONO), y0 + 26 * i), res, font=F_MONO,
               fill=(*MUTED, int(255 * a)))

    # stat readouts (right column)
    stats = []
    if t >= 6.4:
        stats.append(("T₁", f"{s['t1']:.2f}", "s", 6.4))
        stats.append(("ROOF DRIFT · WIND", s["max_drift_ratio"], "", 6.7))
    if t >= 8.4:
        failing = s["failing"]
        if t >= 10.4:
            failing = round(lerp(s["failing"], s["failing_v2"], ease(seg(t, 11.2, 11.9))))
        dc = lerp(s["max_dc"], s["max_dc_v2"], ease(seg(t, 11.2, 11.9))) if t >= 10.4 else s["max_dc"]
        stats.append(("MAX D/C", f"{dc:.2f}", "", 8.4))
        stats.append(("OVER CAPACITY", f"{failing}", f"/ {s['designed']}", 8.7))
    x = W - 48
    for i, (label, num, unit, at) in enumerate(stats):
        k = ease(seg(t, at, at + 0.5)) * (1 - outro)
        y = 96 + i * 78 + (1 - k) * 10
        d.text((x, y), label, font=F_STAT_L, fill=(*MUTED, int(230 * k)), anchor="ra")
        col = INK
        if label in ("MAX D/C", "OVER CAPACITY"):
            val = float(num.split()[0]) if label == "MAX D/C" else float(num)
            hot = (val > 1.0) if label == "MAX D/C" else (val > 0)
            col = (251, 113, 133) if hot else (94, 234, 212)
        w_unit = d.textlength(" " + unit, font=F_STAT_L) if unit else 0
        d.text((x - w_unit, y + 16), num, font=F_STAT_N, fill=(*col, int(255 * k)), anchor="ra")
        if unit:
            d.text((x, y + 40), unit, font=F_STAT_L, fill=(*MUTED, int(230 * k)), anchor="ra")

    # D/C legend
    lg = ease(seg(t, 8.2, 8.8)) * (1 - outro)
    if lg > 0:
        lx, ly, lw = W - 48 - 180, H - 64, 180
        for i in range(lw):
            c = dc_color(1.1 * i / lw)
            d.line((lx + i, ly, lx + i, ly + 4), fill=(*(int(v) for v in c), int(255 * lg)))
        d.text((lx, ly + 10), "0", font=F_STAT_L, fill=(*MUTED, int(220 * lg)))
        d.text((lx + lw * (1.0 / 1.1), ly + 10), "1.0", font=F_STAT_L, fill=(*MUTED, int(220 * lg)), anchor="ma")
        d.text((lx, ly - 18), "DEMAND / CAPACITY", font=F_STAT_L, fill=(*MUTED, int(220 * lg)))

    # end title
    if t >= 12.9:
        k = ease(seg(t, 12.9, 13.8))
        dy = (1 - k) * 18
        d.text((W / 2, H / 2 - 30 + dy), "Your agent, inside ETABS.", font=F_HERO, fill=(*INK, int(255 * k)), anchor="mm")
        k2 = ease(seg(t, 13.3, 14.1))
        d.text((W / 2, H / 2 + 40), "Model  ·  Analyse  ·  Design  ·  Iterate  —  through MCP", font=F_HERO_S,
               fill=(*MUTED, int(255 * k2)), anchor="mm")
        k3 = ease(seg(t, 13.7, 14.4))
        d.text((W / 2, H / 2 + 92), "claude mcp add etabs -- etabs-mcp", font=F_MONO,
               fill=(120, 200, 255, int(255 * k3)), anchor="mm")
    return img


# --------------------------------------------------------------------------- compositing

def backdrop() -> Image.Image:
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    cx, cy = W * 0.5, H * 0.58
    r = np.sqrt(((xx - cx) / W) ** 2 + ((yy - cy) / H) ** 2)
    glow = np.clip(1 - r * 1.6, 0, 1) ** 2
    base = BG[None, None, :] + glow[..., None] * np.array([10, 18, 34], np.float32)
    noise = np.random.default_rng(7).normal(0, 1.4, (H, W, 1))
    return Image.fromarray(np.clip(base + noise, 0, 255).astype(np.uint8))


def frame(scene: Scene, cam: Camera, t: float, back: Image.Image, s: dict) -> Image.Image:
    # Camera: slow orbit and push-in, settling for the title.
    az = math.radians(-128 + 34 * ease(seg(t, 0, 13.0)))
    el = math.radians(lerp(13, 21, ease(seg(t, 0, 9))))
    dist = lerp(118, 100, ease(seg(t, 0, 11))) + 10 * ease(seg(t, 12.4, 14.5))
    cam.setup(az, el, dist)

    under = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    ground(ImageDraw.Draw(under, "RGBA"), cam, scene, ease(seg(t, 0.2, 1.4)) * (1 - 0.7 * ease(seg(t, 12.6, 13.6))))
    art, glow = render_structure(scene, cam, t)
    under.alpha_composite(art)

    g = glow.resize((W // 2, H // 2), Image.BILINEAR).filter(ImageFilter.GaussianBlur(9)).resize((W, H), Image.BILINEAR)
    ga = np.asarray(g, np.float32)
    lit = np.asarray(back, np.float32) + ga[..., :3] * (ga[..., 3:4] / 255.0) * 1.15
    img = Image.fromarray(np.clip(lit, 0, 255).astype(np.uint8)).convert("RGBA")
    img.alpha_composite(under.resize((W, H), Image.LANCZOS))
    # Overlay text must be drawn on RGB: on RGBA, ImageDraw writes alpha instead of blending it.
    img = overlay(img.convert("RGB"), t, s)
    fade_in = ease(seg(t, 0, 0.6))
    if fade_in < 1:
        img = Image.blend(Image.new("RGB", (W, H), tuple(int(v) for v in BG)), img, fade_in)
    return img.convert("RGB")


def framed_screenshot(path: Path, out: Path) -> None:
    """Present a real ETABS capture: the full window on the dark stage, rounded, with a soft shadow.
    The licensee name in the title bar is blurred."""
    shot = Image.open(path).convert("RGB")
    sx, sy = shot.width / 1936, shot.height / 1048
    box = (int(1730 * sx), int(26 * sy), int(1936 * sx), int(56 * sy))
    shot.paste(shot.crop(box).filter(ImageFilter.GaussianBlur(10)), box)
    width = 1160
    view = shot.resize((width, round(shot.height * width / shot.width)), Image.LANCZOS)
    canvas = backdrop().resize((1280, view.height + 120)).convert("RGBA")
    x, y = (canvas.width - view.width) // 2, 48
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((x + 10, y + 22, x + view.width - 10, y + view.height + 24), 14,
                                             fill=(0, 0, 0, 230))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(24)))
    mask = Image.new("L", view.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, view.width - 1, view.height - 1), 12, fill=255)
    canvas.paste(view, (x, y), mask)
    ImageDraw.Draw(canvas).rounded_rectangle((x, y, x + view.width - 1, y + view.height - 1), 12,
                                             outline=(58, 70, 96), width=1)
    canvas.convert("RGB").save(out, optimize=True)


def main() -> None:
    import sys

    data = json.loads(DATA.read_text())
    if len(sys.argv) > 1 and sys.argv[1] == "--preview":
        s, scene = data["stats"], Scene(data)
        cam, back = Camera(scene.center.copy()), backdrop()
        for t in map(float, sys.argv[2:]):
            frame(scene, cam, t, back, s).save(ROOT / "docs" / "live" / f"preview_{t:05.2f}.png")
        framed_screenshot(ROOT / "docs" / "live" / "tower_design_v2.png", OUT / "etabs_window.png")
        return
    s = data["stats"]
    scene = Scene(data)
    cam = Camera(scene.center.copy())
    back = backdrop()
    OUT.mkdir(parents=True, exist_ok=True)
    n = int(DURATION * FPS)
    stills = {int(4.9 * FPS): "model", int(6.9 * FPS): "analyse", int(9.6 * FPS): "design",
              int(12.2 * FPS): "iterate"}
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(n):
            t = i / FPS
            img = frame(scene, cam, t, back, s)
            img.save(Path(tmp) / f"f{i:04d}.png")
            if i in stills:
                img.save(OUT / f"still_{stills[i]}.png")
            if i % 48 == 0:
                print(f"frame {i}/{n}")
        pattern = str(Path(tmp) / "f%04d.png")
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", pattern,
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "slow",
                        "-movflags", "+faststart", str(OUT / "hero.mp4")], check=True)
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", pattern,
                        "-vf", "scale=1120:-1:flags=lanczos", "-c:v", "libwebp_anim", "-lossless", "0",
                        "-quality", "82", "-compression_level", "6", "-loop", "0", str(OUT / "hero.webp")], check=True)
    src = ROOT / "docs" / "live" / "tower_design_v2.png"
    if src.exists():
        framed_screenshot(src, OUT / "etabs_window.png")
    for f in ("hero.mp4", "hero.webp"):
        print(f, f"{(OUT / f).stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
