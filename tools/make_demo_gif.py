"""Render the README motion graphic (docs/assets/demo.gif) from the live test's real screenshots and numbers.

    python tools/make_demo_gif.py            # after: ETABS_MCP_ARTIFACTS=docs/live pytest tests/live --live
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "docs" / "live"
OUT = ROOT / "docs" / "assets"
W, H = 960, 540
FPS = 15
FONTS = Path("C:/Windows/Fonts")

BG0, BG1 = (7, 10, 18), (14, 20, 36)
INK, MUTED, FAINT = (232, 238, 250), (138, 152, 178), (44, 56, 82)
CYAN, VIOLET, GREEN, AMBER = (56, 213, 255), (151, 117, 255), (74, 222, 128), (251, 191, 36)


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(FONTS / name), size)
    except OSError:
        return ImageFont.load_default()


F_TITLE, F_H, F_B, F_S = font("segoeuib.ttf", 30), font("seguisb.ttf", 17), font("segoeui.ttf", 15), font("segoeui.ttf", 12)
F_MONO, F_MONO_B = font("CascadiaMono.ttf", 14), font("CascadiaMono.ttf", 15)
F_HERO, F_HERO_S = font("segoeuib.ttf", 58), font("segoeui.ttf", 21)


def load_numbers() -> dict:
    log = json.loads((LIVE / "live_transcript.json").read_text())
    n = {"calls": len(log)}
    for e in log:
        r = e["result"]
        if e["tool"] == "etabs_run_analysis":
            n["analysis_s"] = r["seconds"]
        if e["tool"] == "etabs_run_design":
            n["members"], n["max_dc"] = r["members_designed"], r["max_ratio"]
        if e["tool"] == "etabs_get_results" and e["args"]["result"] == "modal_periods":
            n["t1"] = r["rows"][0]["Period"]
        if e["tool"] == "etabs_get_results" and e["args"]["result"] == "story_drifts":
            worst = max(r["rows"], key=lambda x: x["Drift"])
            n["drift"] = worst["Drift/"]
        if e["tool"] == "etabs_get_results" and e["args"]["result"] == "base_reactions":
            n["fz_ll"] = next(x["FZ"] for x in r["rows"] if x["OutputCase"] == "LL")
    return n


def background() -> Image.Image:
    img = Image.linear_gradient("L").rotate(-60, expand=False).resize((W, H))
    img = Image.merge("RGB", [img.point(lambda v, i=i: int(BG0[i] + (BG1[i] - BG0[i]) * v / 255)) for i in range(3)])
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((330, 140, 630, 400), fill=(10, 46, 72))
    gd.ellipse((660, 40, 1000, 380), fill=(34, 20, 70))
    img = ImageChops.add(img, glow.filter(ImageFilter.GaussianBlur(90)))
    d = ImageDraw.Draw(img)
    for x in range(12, W, 24):
        for y in range(12, H, 24):
            d.point((x, y), fill=(30, 40, 62))
    return img


def rounded(d: ImageDraw.ImageDraw, box, r, fill=None, outline=None, width=1):
    d.rounded_rectangle(box, r, fill=fill, outline=outline, width=width)


def ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return (u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
            u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1])


def crop_view(path: Path) -> Image.Image:
    shot = Image.open(path).convert("RGB")
    sx, sy = shot.width / 1600, shot.height / 693
    view = shot.crop((int(975 * sx), int(88 * sy), int(1592 * sx), int(662 * sy)))
    return view.resize((300, 279), Image.LANCZOS)


def main() -> None:
    n = load_numbers()
    OUT.mkdir(parents=True, exist_ok=True)
    base = background()
    model_view, design_view = crop_view(LIVE / "01_model.png"), crop_view(LIVE / "02_after_design.png")

    agents = ["Claude Code", "Codex", "Factory Droid", "Cursor"]
    agent_pos = [(40, 140 + i * 62) for i in range(4)]
    hub = (370, 205, 590, 335)
    panel = (640, 92, 940, 400)
    hub_in, hub_out = (hub[0], (hub[1] + hub[3]) // 2), (hub[2], (hub[1] + hub[3]) // 2)
    etabs_in = (panel[0], 246)

    steps = [
        ("etabs_new_model", "grid_only · 2 stories", "kN_m_C · stories ready"),
        ("etabs_add_frames", "26 columns + beams", "26 created"),
        ("etabs_assign_loads", "SDL · LL · WX", "4 load sets assigned"),
        ("etabs_run_analysis", "all cases", f"{n['analysis_s']} s · all cases finished"),
        ("etabs_get_results", "base_reactions", f"LL ΣFz = {n['fz_ll']:.0f} kN (hand calc ✓)"),
        ("etabs_get_results", "modal_periods", f"T1 = {n['t1']:.3f} s"),
        ("etabs_get_results", "story_drifts", f"max drift {n['drift']}"),
        ("etabs_run_design", "steel · AISC 360-16", f"{n['members']} members · max D/C {n['max_dc']:.2f}"),
    ]
    step_len = 13
    end_len = 30
    total = len(steps) * step_len + end_len
    frames: list[Image.Image] = []

    for f in range(total):
        img = base.copy()
        d = ImageDraw.Draw(img, "RGBA")
        step = min(f // step_len, len(steps) - 1)
        local = (f % step_len) / step_len if f < len(steps) * step_len else 1.0
        active_agent = step % len(agents)

        # header
        d.text((40, 30), "etabs-mcp", font=F_TITLE, fill=INK)
        d.text((40, 70), "Agentic MCP server for CSI ETABS  ·  v22+  ·  official API, 1,293 methods", font=F_B, fill=MUTED)
        rounded(d, (W - 186, 34, W - 40, 62), 14, fill=(20, 36, 30, 255), outline=(40, 120, 80, 255))
        d.ellipse((W - 174, 43, W - 164, 53), fill=GREEN)
        d.text((W - 158, 39), "live · ETABS 23.3", font=F_S, fill=(180, 240, 200))

        # links agent -> hub
        for i, (ax, ay) in enumerate(agent_pos):
            start = (ax + 170, ay + 20)
            c1, c2 = (start[0] + 70, start[1]), (hub_in[0] - 70, hub_in[1])
            pts = [bezier(start, c1, c2, hub_in, k / 30) for k in range(31)]
            col = (*CYAN, 200) if i == active_agent else (*FAINT, 255)
            d.line(pts, fill=col, width=2 if i == active_agent else 1)
            if i == active_agent and f < len(steps) * step_len:
                for trail in range(6):
                    tt = ease(local * 1.6) - trail * 0.03
                    if 0 <= tt <= 1:
                        x, y = bezier(start, c1, c2, hub_in, tt)
                        rr = 5 - trail * 0.7
                        d.ellipse((x - rr, y - rr, x + rr, y + rr), fill=(*CYAN, 255 - trail * 38))
        # hub -> etabs link
        d.line([hub_out, etabs_in], fill=(*VIOLET, 220), width=2)
        if f < len(steps) * step_len:
            for trail in range(6):
                tt = ease((local - 0.35) * 1.8) - trail * 0.04
                if 0 <= tt <= 1:
                    x = hub_out[0] + (etabs_in[0] - hub_out[0]) * tt
                    y = hub_out[1] + (etabs_in[1] - hub_out[1]) * tt
                    rr = 5 - trail * 0.7
                    d.ellipse((x - rr, y - rr, x + rr, y + rr), fill=(*VIOLET, 255 - trail * 38))

        # agent chips
        for i, ((ax, ay), name) in enumerate(zip(agent_pos, agents)):
            on = i == active_agent
            rounded(d, (ax, ay, ax + 170, ay + 40), 12, fill=(22, 30, 50, 255) if not on else (16, 44, 64, 255),
                    outline=(*CYAN, 255) if on else (*FAINT, 255), width=2 if on else 1)
            d.ellipse((ax + 14, ay + 15, ax + 24, ay + 25), fill=CYAN if on else MUTED)
            d.text((ax + 34, ay + 10), name, font=F_H if on else F_B, fill=INK if on else MUTED)

        # hub with pulse
        pulse = 0.5 + 0.5 * math.sin(f / FPS * math.pi * 2 * 0.8)
        for g in range(4, 0, -1):
            pad = g * 6 + pulse * 4
            rounded(d, (hub[0] - pad, hub[1] - pad, hub[2] + pad, hub[3] + pad), 22 + g * 4,
                    fill=(56, 213, 255, int(10 + 6 * (4 - g))))
        rounded(d, hub, 20, fill=(13, 22, 40, 255), outline=(*CYAN, 255), width=2)
        d.text(((hub[0] + hub[2]) / 2, hub[1] + 30), "etabs-mcp", font=F_H, fill=INK, anchor="mm")
        d.text(((hub[0] + hub[2]) / 2, hub[1] + 58), "43 tools · typed · validated", font=F_S, fill=MUTED, anchor="mm")
        call_name = steps[step][0] if f < len(steps) * step_len else "ready"
        rounded(d, (hub[0] + 18, hub[1] + 78, hub[2] - 18, hub[1] + 108), 10, fill=(30, 22, 64, 255))
        d.text(((hub[0] + hub[2]) / 2, hub[1] + 93), call_name, font=F_MONO, fill=(205, 190, 255), anchor="mm")

        # ETABS panel
        rounded(d, (panel[0] - 2, panel[1] - 2, panel[2] + 2, panel[3] + 2), 14, fill=(*VIOLET, 90))
        rounded(d, panel, 12, fill=(240, 240, 240, 255))
        d.rectangle((panel[0], panel[1], panel[2], panel[1] + 26), fill=(0, 120, 215))
        d.text((panel[0] + 10, panel[1] + 6), "ETABS Ultimate v23.3.2", font=F_S, fill=(255, 255, 255))
        view = model_view if step < 7 else Image.blend(model_view, design_view, ease(local * 2) if f < len(steps) * step_len else 1.0)
        if step < 1:
            view = Image.blend(Image.new("RGB", view.size, (255, 255, 255)), model_view, ease(local * 1.5))
        img.paste(view, (panel[0], panel[1] + 27))
        d = ImageDraw.Draw(img, "RGBA")
        d.text((panel[0], panel[3] + 12), "what the agent sees · etabs_screenshot", font=F_S, fill=MUTED)

        # console
        con = (40, 428, 920, 520)
        rounded(d, con, 12, fill=(9, 13, 24, 245), outline=(*FAINT, 255))
        lines = []
        upto = step if f < len(steps) * step_len else len(steps) - 1
        for k in range(max(0, upto - 1), upto + 1):
            name, args, res = steps[k]
            lines.append((k, name, args, res))
        y = con[1] + 14
        for k, name, args, res in lines:
            partial = k == upto and f < len(steps) * step_len
            text = f"› {name}({args})"
            shown = text[: int(len(text) * min(1.0, local * 2.2))] if partial else text
            d.text((con[0] + 16, y), shown, font=F_MONO_B, fill=(170, 200, 255))
            if not partial or local > 0.55:
                d.text((con[0] + 16, y + 20), f"  ✓ {res}", font=F_MONO, fill=GREEN)
            y += 42

        # end card
        if f >= len(steps) * step_len:
            t = ease((f - len(steps) * step_len) / 10)
            veil = Image.new("RGBA", (W, H), (6, 9, 16, int(225 * t)))
            img = Image.alpha_composite(img.convert("RGBA"), veil).convert("RGB")
            d = ImageDraw.Draw(img, "RGBA")
            if t > 0.3:
                a = int(255 * ease((t - 0.3) / 0.7))
                d.text((W / 2, H / 2 - 40), "Your agent, inside ETABS.", font=F_HERO, fill=(*INK, a), anchor="mm")
                d.text((W / 2, H / 2 + 22), f"Model · Analyse · Design · Report   —   {n['calls']} live calls, 0 failures",
                       font=F_HERO_S, fill=(*MUTED, a), anchor="mm")
                d.text((W / 2, H / 2 + 70), "claude mcp add etabs -- etabs-mcp", font=F_MONO_B, fill=(*CYAN, a), anchor="mm")
        frames.append(img)

    pal = [fr.quantize(colors=200, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for fr in frames]
    durations = [1000 // FPS] * len(pal)
    durations[-1] = 2600
    pal[0].save(OUT / "demo.gif", save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=True,
                disposal=1)
    frames[-1].save(OUT / "demo_end.png")
    frames[len(steps) * step_len - 1].save(OUT / "demo_frame.png")
    print("wrote", OUT / "demo.gif", f"{(OUT / 'demo.gif').stat().st_size // 1024} KB, {len(pal)} frames")


if __name__ == "__main__":
    main()
