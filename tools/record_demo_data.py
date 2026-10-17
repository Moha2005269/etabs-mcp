"""Drive the real MCP tools against a live ETABS to record the data the README visuals are rendered from.

Every number and coordinate in docs/assets comes from this run: geometry, wind deflections, D/C ratios.

    python tools/record_demo_data.py          # launches its own ETABS, closes it when done
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

from etabs_mcp.server import create_server
from etabs_mcp.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "live"

XS = [0.0, 7.5, 15.0, 22.5, 30.0]
YS = [0.0, 6.0, 12.0, 18.0]
STORIES = 6
H1, HT = 4.2, 3.5
LEVELS = [0.0] + [H1 + HT * i for i in range(STORIES)]


class Recorder:
    def __init__(self) -> None:
        self.mcp = create_server(Settings(connect_mode="start", allow_destructive=True))
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, tool: str, **args: Any) -> Any:
        t0 = time.monotonic()
        result = await self.mcp.call_tool(tool, args)
        dt = time.monotonic() - t0
        content, structured = result if isinstance(result, tuple) else (result, None)
        self.calls.append({"tool": tool, "seconds": round(dt, 2)})
        print(f"  {tool:30s} {dt:6.2f}s", file=sys.stderr)
        if structured is not None:
            return structured.get("result", structured) if set(structured) == {"result"} else structured
        return content


def members() -> tuple[list[dict], list[dict]]:
    frames: list[dict] = []
    for s in range(STORIES):
        z0, z1 = LEVELS[s], LEVELS[s + 1]
        col = "W14X132" if s < 3 else "W14X90"
        for x in XS:
            for y in YS:
                frames.append({"start": [x, y, z0], "end": [x, y, z1], "section": col})
        for y in YS:
            for k in range(len(XS) - 1):
                frames.append({"start": [XS[k], y, z1], "end": [XS[k + 1], y, z1], "section": "W21X50"})
        for x in XS:
            for k in range(len(YS) - 1):
                frames.append({"start": [x, YS[k], z1], "end": [x, YS[k + 1], z1], "section": "W18X35"})
        # Chevron-free X bracing in the end bays of the long faces.
        for y in (YS[0], YS[-1]):
            for a, b in ((XS[0], XS[1]), (XS[-2], XS[-1])):
                frames.append({"start": [a, y, z0], "end": [b, y, z1], "section": "W10X33"})
                frames.append({"start": [b, y, z0], "end": [a, y, z1], "section": "W10X33"})
    slabs = [
        {"points": [[XS[i], YS[j], z], [XS[i + 1], YS[j], z], [XS[i + 1], YS[j + 1], z], [XS[i], YS[j + 1], z]],
         "property": "SLAB200"}
        for z in LEVELS[1:] for i in range(len(XS) - 1) for j in range(len(YS) - 1)
    ]
    return frames, slabs


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    call = Recorder()
    session = call.mcp._etabs_session  # type: ignore[attr-defined]
    try:
        await call("etabs_connect", mode="start")
        await call("etabs_new_model", template="grid_only", units="kN_m_C", number_of_stories=STORIES,
                   typical_story_height=HT, bottom_story_height=H1, grid_lines_x=len(XS), grid_lines_y=len(YS),
                   spacing_x=7.5, spacing_y=6.0, confirm=True)
        await call("etabs_define_material", name="A992Fy50", material_type="Steel", region="United States",
                   standard="ASTM A992", grade="Grade 50")
        await call("etabs_define_material", name="C30", material_type="Concrete", elastic_modulus=2.7e7,
                   unit_weight=25.0, fc=30000.0)
        for sec in ("W14X132", "W14X90", "W21X50", "W18X35", "W10X33"):
            await call("etabs_define_frame_section", name=sec, shape="from_library", material="A992Fy50",
                       library_file="AISC16.xml", library_section=sec)
        await call("etabs_define_area_section", name="SLAB200", kind="slab", material="C30", thickness=0.2,
                   shell_type="Membrane")

        frames_in, slabs_in = members()
        made = await call("etabs_add_frames", frames=frames_in)
        assert not made["errors"], made["errors"][:3]
        await call("etabs_add_areas", areas=slabs_in)
        await call("etabs_set_restraints", restraint="fixed", at_elevation=0.0)

        await call("etabs_define_load_patterns", patterns=[{"name": "SDL", "type": "SuperDead"},
                                                          {"name": "LL", "type": "Live"},
                                                          {"name": "WX", "type": "Wind"}])
        areas = (await call("etabs_list_objects", kind="area", limit=5000, include_labels=False))["objects"]
        points = (await call("etabs_list_objects", kind="point", limit=5000, include_labels=False))["objects"]
        windward = [p["name"] for p in points if p["x"] == 0.0 and p["z"] > 0]
        slab_names = [a["name"] for a in areas]
        await call("etabs_assign_loads", loads=[
            {"kind": "area_uniform", "pattern": "SDL", "names": slab_names, "value": 1.5},
            {"kind": "area_uniform", "pattern": "LL", "names": slab_names, "value": 3.0},
            {"kind": "point_force", "pattern": "WX", "names": windward, "forces": [18, 0, 0, 0, 0, 0]},
        ])
        await call("etabs_define_combinations", add_default_design_combos=["steel"])

        run = await call("etabs_run_analysis", save_as=str(OUT / "DemoTower.EDB"))
        disp = await call("etabs_get_results", result="joint_displacements", load_cases=["WX"],
                          load_combinations=[], limit=20000)
        modal = await call("etabs_get_results", result="modal_periods", load_cases=["Modal"], load_combinations=[])
        drifts = await call("etabs_get_results", result="story_drifts", load_cases=["WX"], load_combinations=[])
        base = await call("etabs_get_results", result="base_reactions", load_cases=["SDL", "LL", "WX"],
                          load_combinations=[])
        design = await call("etabs_run_design", kind="steel", summary_limit=5000)
        frames = (await call("etabs_list_objects", kind="frame", limit=5000, include_labels=False))["objects"]

        # A large, maximised ETABS window gives a crisp 3D view for the README.
        import win32con  # type: ignore[import-not-found]
        import win32gui  # type: ignore[import-not-found]

        from etabs_mcp.discovery import main_window_for_pid

        proc = main_window_for_pid(session.backend.pid)
        if proc:
            win32gui.ShowWindow(proc.hwnd, win32con.SW_MAXIMIZE)
            await asyncio.sleep(1.5)

        async def snapshot(name: str) -> None:
            shot = await call.mcp.call_tool("etabs_screenshot", {"zoom_to_fit": True, "max_width": 3840})
            content = shot[0] if isinstance(shot, tuple) else shot
            img = next(c for c in content if getattr(c, "type", "") == "image")
            (OUT / name).write_bytes(base64.b64decode(img.data))

        await snapshot("tower_design_v1.png")
        ratios_v1 = {m["frame"]: m["ratio"] for m in design["members"]}

        # The agent's redesign loop: upsize every overstressed member, re-analyse, re-design.
        failing = [m["frame"] for m in design["members"] if (m["ratio"] or 0) > 1.0]
        upsized = {}
        if failing:
            await call("etabs_unlock_model", confirm=True)
            await call("etabs_define_frame_section", name="W24X76", shape="from_library", material="A992Fy50",
                       library_file="AISC16.xml", library_section="W24X76")
            await call("etabs_assign_section", kind="frame", section="W24X76", names=failing)
            upsized = {n: "W24X76" for n in failing}
            run2 = await call("etabs_run_analysis")
            design2 = await call("etabs_run_design", kind="steel", summary_limit=5000)
            await snapshot("tower_design_v2.png")
        else:
            run2, design2 = run, design
        ratios_v2 = {m["frame"]: m["ratio"] for m in design2["members"]}

        data = {
            "frames": [
                {"name": f["name"], "start": f["start"], "end": f["end"], "section": f["section"],
                 "section_v2": upsized.get(f["name"], f["section"]),
                 "ratio": ratios_v1.get(f["name"]), "ratio_v2": ratios_v2.get(f["name"])}
                for f in frames
            ],
            "slabs": [a["coordinates"] for a in areas],
            "displacement_wx": {r["UniqueName"]: [r["Ux"], r["Uy"], r["Uz"]] for r in disp["rows"]},
            "points": {p["name"]: [p["x"], p["y"], p["z"]] for p in points},
            "stats": {
                "calls": len(call.calls),
                "frames": len(frames),
                "slabs": len(areas),
                "analysis_seconds": run["seconds"],
                "t1": modal["rows"][0]["Period"],
                "max_drift": max(r["Drift"] for r in drifts["rows"]),
                "max_drift_ratio": max(drifts["rows"], key=lambda r: r["Drift"])["Drift/"],
                "base_fz_ll": next(r["FZ"] for r in base["rows"] if r["OutputCase"] == "LL"),
                "base_fx_wx": next(r["FX"] for r in base["rows"] if r["OutputCase"] == "WX"),
                "designed": design["members_designed"],
                "max_dc": design["max_ratio"],
                "failing": design["failing"],
                "upsized": len(upsized),
                "analysis2_seconds": run2["seconds"],
                "max_dc_v2": design2["max_ratio"],
                "failing_v2": design2["failing"],
                "roof_ux_mm": max(abs(v[0]) for v in
                                  ({r["UniqueName"]: [r["Ux"]] for r in disp["rows"]}).values()) * 1000,
            },
            "calls": call.calls,
        }
        (OUT / "demo_data.json").write_text(json.dumps(data, indent=1))
        print(json.dumps(data["stats"], indent=1))
    finally:
        await call.mcp.call_tool("etabs_disconnect", {"close_etabs": True, "confirm": True})


if __name__ == "__main__":
    asyncio.run(main())
