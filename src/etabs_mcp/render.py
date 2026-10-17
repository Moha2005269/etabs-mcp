"""Offscreen renderer for ETABS models.

Draws the structure from model data (not from the ETABS window), so it works with ETABS hidden, never takes
focus, and can show what the ETABS API cannot switch the GUI to: deformed shapes for any case, members coloured
by design ratio, displacement, section or storey, from any camera angle.
"""

from __future__ import annotations

import colorsys
import io
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# D/C palette shared with the README film: teal -> sky -> amber -> coral -> crimson.
DC_STOPS = [(0.0, (34, 211, 197)), (0.45, (56, 189, 248)), (0.75, (250, 204, 21)),
            (0.95, (251, 146, 60)), (1.05, (244, 63, 94))]
SEQ_STOPS = [(0.0, (49, 54, 149)), (0.35, (69, 117, 180)), (0.6, (116, 173, 209)),
             (0.8, (253, 174, 97)), (1.0, (215, 48, 39))]
THEMES = {
    "dark": {"bg": (6, 9, 16), "ink": (232, 238, 248), "muted": (130, 142, 164), "grid": (40, 52, 78),
             "plain": (168, 186, 255), "slab": (120, 150, 230, 22), "ghost": (90, 100, 125)},
    "light": {"bg": (250, 250, 252), "ink": (20, 24, 32), "muted": (100, 108, 122), "grid": (220, 224, 232),
              "plain": (60, 72, 110), "slab": (90, 110, 160, 26), "ghost": (190, 196, 208)},
}
VIEWS = {"iso": (-128.0, 24.0), "iso_back": (52.0, 24.0), "front": (-90.0, 0.5), "side": (0.0, 0.5),
         "top": (-90.0, 89.5)}
_FONT_DIR = Path("C:/Windows/Fonts")


def _font(name: str, size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(_FONT_DIR / name), size)
    except OSError:
        try:
            return ImageFont.load_default(size)
        except TypeError:  # Pillow < 10.1
            return ImageFont.load_default()


def _ramp(stops: list[tuple[float, tuple[int, int, int]]], v: float) -> tuple[int, int, int]:
    if v <= stops[0][0]:
        return stops[0][1]
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if v <= p1:
            k = (v - p0) / (p1 - p0)
            return tuple(int(c0[i] + (c1[i] - c0[i]) * k) for i in range(3))
    return stops[-1][1]


def dc_color(ratio: float | None) -> tuple[int, int, int] | None:
    return None if ratio is None else _ramp(DC_STOPS, ratio)


def categorical(i: int) -> tuple[int, int, int]:
    h = (i * 0.61803398875 + 0.08) % 1.0
    r, g, b = colorsys.hls_to_rgb(h, 0.62, 0.72)
    return int(r * 255), int(g * 255), int(b * 255)


class Camera:
    def __init__(self, target: np.ndarray, width: int, height: int):
        self.target = np.asarray(target, np.float64)
        self.width, self.height = width, height

    def setup(self, azimuth_deg: float, elevation_deg: float, distance: float, fov: float = 34.0) -> None:
        az, el = math.radians(azimuth_deg), math.radians(elevation_deg)
        eye = self.target + distance * np.array([math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)])
        fwd = self.target - eye
        fwd /= np.linalg.norm(fwd)
        up_hint = np.array([0, 0, 1.0]) if abs(fwd[2]) < 0.99 else np.array([0, 1.0, 0])
        right = np.cross(fwd, up_hint)
        right /= np.linalg.norm(right)
        self.eye, self.fwd, self.right, self.up = eye, fwd, right, np.cross(right, fwd)
        self.f = (self.height / 2) / math.tan(math.radians(fov) / 2)

    def project(self, p: np.ndarray) -> np.ndarray:
        d = np.asarray(p, np.float64) - self.eye
        z = d @ self.fwd
        return np.stack([self.width / 2 + self.f * (d @ self.right) / z, self.height / 2 - self.f * (d @ self.up) / z], -1)


@dataclass
class Member:
    name: str
    start: tuple[float, float, float]
    end: tuple[float, float, float]
    section: str = ""
    story: str = ""
    ratio: float | None = None


@dataclass
class ModelData:
    members: list[Member]
    slabs: list[list[tuple[float, float, float]]] = field(default_factory=list)
    displacements: dict[tuple[float, float, float], tuple[float, float, float]] = field(default_factory=dict)


@dataclass
class RenderOptions:
    color_by: str = "none"            # none | design_ratio | section | story | displacement
    view: str = "iso"
    azimuth: float | None = None
    elevation: float | None = None
    deformed_scale: float = 0.0       # multiplier on displacements (0 = undeformed)
    show_undeformed: bool = True
    highlight: set[str] = field(default_factory=set)
    width: int = 1400
    height: int = 900
    theme: str = "dark"
    title: str = ""
    subtitle: str = ""
    show_slabs: bool = True


def _key(p) -> tuple[float, float, float]:
    return (round(float(p[0]), 3), round(float(p[1]), 3), round(float(p[2]), 3))


def render(model: ModelData, opt: RenderOptions) -> tuple[bytes, dict]:
    if not model.members and not model.slabs:
        raise ValueError("Nothing to draw: the model has no frames or areas.")
    th = THEMES.get(opt.theme, THEMES["dark"])
    ss = 2
    w, h = opt.width * ss, opt.height * ss
    pts = [p for m in model.members for p in (m.start, m.end)] + [p for s in model.slabs for p in s]
    arr = np.array(pts, np.float64)
    lo, hi = arr.min(0), arr.max(0)
    center = (lo + hi) / 2
    radius = max(np.linalg.norm(hi - lo) / 2, 1e-6)
    az, el = VIEWS.get(opt.view, VIEWS["iso"])
    az = opt.azimuth if opt.azimuth is not None else az
    el = opt.elevation if opt.elevation is not None else el
    cam = Camera(center, w, h)
    cam.setup(az, el, radius * 3.1)
    # Fit: scale the focal length so the projected model fills ~78% of the frame (leaves room for the legend).
    proj = cam.project(arr)
    span = np.maximum(proj.max(0) - proj.min(0), 1)
    fit = min(w * 0.78 / span[0], h * 0.70 / span[1])
    cam.f *= fit
    proj = cam.project(arr)
    shift = np.array([w / 2, h * 0.54]) - (proj.max(0) + proj.min(0)) / 2

    def P(p) -> np.ndarray:
        return cam.project(np.asarray(p, np.float64)) + shift

    def displaced(p) -> np.ndarray:
        d = model.displacements.get(_key(p))
        base = np.asarray(p, np.float64)
        return base if d is None or not opt.deformed_scale else base + np.asarray(d) * opt.deformed_scale

    # colours
    colors: list[tuple[int, int, int]] = []
    legend: dict = {}
    if opt.color_by == "design_ratio":
        colors = [dc_color(m.ratio) or th["ghost"] for m in model.members]
        legend = {"kind": "ramp", "stops": DC_STOPS, "lo": 0.0, "hi": 1.05, "label": "DEMAND / CAPACITY"}
    elif opt.color_by in ("section", "story"):
        keys = sorted({(m.section if opt.color_by == "section" else m.story) for m in model.members})
        if opt.color_by == "story":
            def zkey(s: str) -> float:
                zs = [min(m.start[2], m.end[2]) for m in model.members if m.story == s]
                return min(zs) if zs else 0.0
            keys.sort(key=zkey)
        cmap = {k: categorical(i) for i, k in enumerate(keys)}
        colors = [cmap[m.section if opt.color_by == "section" else m.story] for m in model.members]
        legend = {"kind": "swatches", "items": list(cmap.items())[:14], "more": max(0, len(cmap) - 14),
                  "label": opt.color_by.upper()}
    elif opt.color_by == "displacement" and model.displacements:
        mags = {k: float(np.linalg.norm(v)) for k, v in model.displacements.items()}
        dmax = max(mags.values()) or 1.0
        colors = []
        for m in model.members:
            v = max(mags.get(_key(m.start), 0.0), mags.get(_key(m.end), 0.0)) / dmax
            colors.append(_ramp(SEQ_STOPS, v))
        legend = {"kind": "ramp", "stops": SEQ_STOPS, "lo": 0.0, "hi": dmax, "label": "DISPLACEMENT", "abs": True}
    else:
        colors = [th["plain"]] * len(model.members)

    img = Image.new("RGB", (w, h), th["bg"])
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d, dg = ImageDraw.Draw(layer, "RGBA"), ImageDraw.Draw(glow, "RGBA")

    # ground grid
    step = 10 ** math.floor(math.log10(max(hi[0] - lo[0], hi[1] - lo[1], 1) / 4))
    pad = (hi - lo).max() * 0.15
    gx = np.arange(math.floor((lo[0] - pad) / step) * step, hi[0] + pad + step, step)
    gy = np.arange(math.floor((lo[1] - pad) / step) * step, hi[1] + pad + step, step)
    if el < 85:
        for x in gx:
            a, b = P([x, gy[0], lo[2]]), P([x, gy[-1], lo[2]])
            d.line([tuple(a), tuple(b)], fill=(*th["grid"], 110), width=ss)
        for y in gy:
            a, b = P([gx[0], y, lo[2]]), P([gx[-1], y, lo[2]])
            d.line([tuple(a), tuple(b)], fill=(*th["grid"], 110), width=ss)

    if opt.show_slabs:
        for s in sorted(model.slabs, key=lambda s: -np.linalg.norm(np.mean(s, 0) - cam.eye)):
            poly = [tuple(P(displaced(p))) for p in s]
            d.polygon(poly, fill=th["slab"])

    if opt.deformed_scale and opt.show_undeformed:
        for m in model.members:
            d.line([tuple(P(m.start)), tuple(P(m.end))], fill=(*th["ghost"], 90), width=ss)

    depth = np.array([np.linalg.norm((np.array(m.start) + np.array(m.end)) / 2 - cam.eye) for m in model.members])
    dmin, dmax_ = (depth.min(), depth.max()) if len(depth) else (0, 1)
    for i in np.argsort(-depth):
        m = model.members[i]
        a, b = P(displaced(m.start)), P(displaced(m.end))
        fog = 1 - 0.45 * (depth[i] - dmin) / (dmax_ - dmin + 1e-9)
        hl = m.name in opt.highlight
        col = (255, 255, 255) if hl and opt.color_by == "none" else colors[i]
        vertical = abs(m.start[2] - m.end[2]) > 1e-6 and abs(m.start[0] - m.end[0]) < 1e-6 and abs(m.start[1] - m.end[1]) < 1e-6
        width = ss * (2.2 if vertical else 1.6) * (1.9 if hl else 1.0)
        d.line([tuple(a), tuple(b)], fill=(*col, int(255 * fog)), width=max(1, int(width)))
        if opt.theme == "dark":
            dg.line([tuple(a), tuple(b)], fill=(*col, int(110 * fog * (1.8 if hl else 1))), width=int(width * 2.6))

    if opt.theme == "dark":
        g = glow.resize((w // 4, h // 4), Image.BILINEAR).filter(ImageFilter.GaussianBlur(4)).resize((w, h), Image.BILINEAR)
        ga = np.asarray(g, np.float32)
        lit = np.asarray(img, np.float32) + ga[..., :3] * (ga[..., 3:4] / 255.0)
        img = Image.fromarray(np.clip(lit, 0, 255).astype(np.uint8))
    img = img.convert("RGBA")
    img.alpha_composite(layer)
    img = img.resize((opt.width, opt.height), Image.LANCZOS).convert("RGB")
    _annotate(img, opt, th, legend, (az, el))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), {"width": opt.width, "height": opt.height, "azimuth": az, "elevation": el,
                            "members": len(model.members), "slabs": len(model.slabs)}


def _annotate(img: Image.Image, opt: RenderOptions, th: dict, legend: dict, view: tuple[float, float]) -> None:
    d = ImageDraw.Draw(img)
    f_t, f_s, f_l = _font("seguisb.ttf", 20), _font("segoeui.ttf", 14), _font("segoeui.ttf", 12)
    x0, y0 = 28, 24
    if opt.title:
        d.text((x0, y0), opt.title, font=f_t, fill=th["ink"])
        y0 += 30
    if opt.subtitle:
        d.text((x0, y0), opt.subtitle, font=f_s, fill=th["muted"])
    if legend.get("kind") == "ramp":
        lw, lx, ly = 220, img.width - 28 - 220, img.height - 46
        for i in range(lw):
            v = legend["lo"] + (legend["hi"] - legend["lo"]) * i / lw
            norm = v / legend["hi"] if legend.get("abs") else v
            d.line((lx + i, ly, lx + i, ly + 6), fill=_ramp(legend["stops"], norm))
        d.text((lx, ly - 20), legend["label"], font=f_l, fill=th["muted"])
        lo_t = "0"
        hi_t = f"{legend['hi']:.3g}" if legend.get("abs") else "1.0"
        d.text((lx, ly + 10), lo_t, font=f_l, fill=th["muted"])
        hx = lx + lw if legend.get("abs") else lx + lw * (1.0 / 1.05)
        d.text((hx, ly + 10), hi_t, font=f_l, fill=th["muted"], anchor="ma")
    elif legend.get("kind") == "swatches":
        items = legend["items"]
        lx, ly = img.width - 28, 26
        d.text((lx, ly), legend["label"], font=f_l, fill=th["muted"], anchor="ra")
        for j, (name, col) in enumerate(items):
            y = ly + 22 + j * 20
            d.text((lx - 18, y), str(name) or "(none)", font=f_l, fill=th["ink"], anchor="ra")
            d.rectangle((lx - 10, y + 3, lx, y + 13), fill=col)
        if legend["more"]:
            d.text((lx, ly + 22 + len(items) * 20), f"+{legend['more']} more", font=f_l, fill=th["muted"], anchor="ra")
    d.text((28, img.height - 30), f"az {view[0]:.0f}°  el {view[1]:.0f}°" +
           (f"   deformed ×{opt.deformed_scale:.3g}" if opt.deformed_scale else ""), font=f_l, fill=th["muted"])
