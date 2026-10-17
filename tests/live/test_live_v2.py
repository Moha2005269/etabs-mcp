"""Live end-to-end test of the v0.2 tools against a real ETABS, through MCP over stdio.

    pytest tests/live/test_live_v2.py --live -s
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .test_live_e2e import ARTIFACTS, Client

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


async def test_v2_tools(tmp_path: Path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "etabs_mcp", "--connect-mode", "start"],
                                   env={**os.environ, "PYTHONUNBUFFERED": "1"})
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        call = Client(session)
        try:
            await _workflow(call, tmp_path)
        finally:
            await session.call_tool("etabs_disconnect", {"close_etabs": True, "confirm": True})


async def _render(call: Client, name: str, **args) -> dict:
    res = await call.session.call_tool("etabs_render_view", args)
    assert not res.isError, res.content
    img = next(c for c in res.content if c.type == "image")
    png = base64.b64decode(img.data)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 20_000
    if ARTIFACTS:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS / f"render_{name}.png").write_bytes(png)
    text = next(c.text for c in res.content if c.type == "text")
    print(f"  etabs_render_view -> {name} {text[:120]}")
    return eval(text)  # noqa: S307 - our own repr of a plain dict


async def _workflow(call: Client, tmp: Path) -> None:
    tools = {t.name for t in (await call.session.list_tools()).tools}
    assert {"etabs_render_view", "etabs_job_wait", "etabs_find_objects", "etabs_define_seismic_load"} <= tools

    await call("etabs_connect", mode="start")
    # Unit-aware inputs: a kip-ft model driven with metric strings.
    await call("etabs_new_model", template="grid_only", units="kN_m_C", number_of_stories=3,
               typical_story_height="3.5 m", bottom_story_height="4200 mm", grid_lines_x=3, grid_lines_y=3,
               spacing_x="8 m", spacing_y="6 m", confirm=True)
    st = await call("etabs_get_stories")
    assert st["stories"][0]["elevation"] == pytest.approx(11.2)
    await call("etabs_define_material", name="A992Fy50", material_type="Steel", region="United States",
               standard="ASTM A992", grade="Grade 50")
    await call("etabs_define_material", name="C30", material_type="Concrete", elastic_modulus=2.7e7, unit_weight=25.0, fc=30000.0)
    for sec in ("W14X90", "W18X35"):
        await call("etabs_define_frame_section", name=sec, shape="from_library", material="A992Fy50",
                   library_file="AISC16.xml", library_section=sec)
    await call("etabs_define_frame_section", name="CONC-COL", shape="rectangle", material="C30", depth="500 mm", width="0.5")
    await call("etabs_define_area_section", name="SLAB", kind="slab", material="C30", thickness="200 mm",
               shell_type="Membrane")

    xs, ys, zs = [0, 8, 16], [0, 6, 12], [0, 4.2, 7.7, 11.2]
    frames = [{"start": [x, y, zs[i]], "end": [x, y, zs[i + 1]], "section": "W14X90"} for i in range(3) for x in xs for y in ys]
    for z in zs[1:]:
        frames += [{"start": [xs[k], y, z], "end": [xs[k + 1], y, z], "section": "W18X35"} for y in ys for k in range(2)]
        frames += [{"start": [x, ys[k], z], "end": [x, ys[k + 1], z], "section": "W18X35"} for x in xs for k in range(2)]
    made = await call("etabs_add_frames", frames=frames)
    assert made["count"] == len(frames) and not made["errors"]
    slabs = [{"points": [[xs[i], ys[j], z], [xs[i + 1], ys[j], z], [xs[i + 1], ys[j + 1], z], [xs[i], ys[j + 1], z]],
              "property": "SLAB"} for z in zs[1:] for i in range(2) for j in range(2)]
    await call("etabs_add_areas", areas=slabs)
    await call("etabs_set_restraints", restraint="fixed", at_elevation="0 m")

    # --- find objects ---------------------------------------------------------------------------
    cols = await call("etabs_find_objects", role="column")
    assert cols["count"] == 27
    beams_s2 = await call("etabs_find_objects", role="beam", stories=["Story2"])
    assert beams_s2["count"] == 12
    long_beams = await call("etabs_find_objects", role="beam", min_length="7.5 m", select=True)
    assert long_beams["count"] == 18 and all(o["length"] == pytest.approx(8.0) for o in long_beams["objects"])
    boxed = await call("etabs_find_objects", kind="area", box={"zmin": "11 m"})
    assert boxed["count"] == 4

    # --- labels ----------------------------------------------------------------------------------
    await call("etabs_assign_label", label_type="group", label="LONG_BEAMS", object_kind="frame",
               names=long_beams["names"])
    grp = await call("etabs_find_objects", group="LONG_BEAMS")
    assert grp["count"] == 18

    # --- lateral system --------------------------------------------------------------------------
    await call("etabs_define_load_patterns", patterns=[{"name": "SDL", "type": "SuperDead"}, {"name": "LL", "type": "Live"}])
    areas = (await call("etabs_list_objects", kind="area", include_labels=False))["objects"]
    await call("etabs_assign_loads", loads=[
        {"kind": "area_uniform", "pattern": "SDL", "names": [a["name"] for a in areas], "value": 1.5},
        {"kind": "area_uniform", "pattern": "LL", "names": [a["name"] for a in areas], "value": 3.0}])
    dia = await call("etabs_assign_diaphragm", name="D1")
    assert dia["areas_assigned"] == 12 and not dia["errors"]
    await call("etabs_set_mass_source", include_self_weight=True, loads=[{"pattern": "SDL"}, {"pattern": "LL", "multiplier": 0.25}])
    await call("etabs_set_modal_case", max_modes=9)
    eq = await call("etabs_define_seismic_load", name="EQX", directions=["X", "X+E", "X-E"], r=8, omega=3, cd=5.5,
                    ss=1.5, s1=0.6, site_class="D")
    assert eq["stories"] == ["Base", "Story3"]
    wind = await call("etabs_define_wind_load", name="WX", wind_speed=115, exposure="C", angle=0)
    assert wind["stories"] == ["Base", "Story3"]
    rs = await call("etabs_define_response_spectrum", case_name="RSX", function_name="RS-ASCE716", r=8)
    assert rs["scale_factor"] == pytest.approx(9.80665 / 8)
    seismic_tbl = await call("etabs_get_table", table_key="Load Pattern Definitions - Auto Seismic - ASCE 7-16")
    row = seismic_tbl["rows"][0]
    assert row["TopStory"] == "Story3" and row["SiteClass"] == "D"
    flags = {k: row[k] for k in ("XDir", "XDirPlusE", "XDirMinusE", "YDir", "YDirPlusE", "YDirMinusE")}
    assert flags == {"XDir": "Yes", "XDirPlusE": "Yes", "XDirMinusE": "Yes", "YDir": "No", "YDirPlusE": "No",
                     "YDirMinusE": "No"}, flags
    await call("etabs_define_combinations", add_default_design_combos=["steel"])

    # --- checkpoint, change tracking ---------------------------------------------------------------
    await call("etabs_save_model", path=str(tmp / "V2.EDB"))
    base = await call("etabs_what_changed")
    assert base["baseline_recorded"]
    cp = await call("etabs_checkpoint", label="pre-edit")
    # Simulate an engineer's manual edit in the GUI (a raw API call the agent did not make through a tool).
    await call("etabs_call_api", path="FrameObj.SetSection", args={"Name": long_beams["names"][0], "PropName": "W14X90"})
    await call("etabs_add_frames", frames=[{"start": [16, 12, 11.2], "end": [24, 12, 11.2], "section": "W18X35"}])
    diff = await call("etabs_what_changed")
    assert diff["changed"] and diff["changes"]["frames"]["counts"]["added"] == 1
    assert diff["changes"]["frames"]["modified"][0]["property"] == {"from": "W18X35", "to": "W14X90"}
    await call("etabs_restore_checkpoint", checkpoint="pre-edit", confirm=True)
    after = await call("etabs_model_overview")
    assert after["counts"]["frames"] == len(frames) and after["file"].lower().endswith("v2.edb")
    assert (await call("etabs_list_checkpoints"))["checkpoints"][0]["name"] == cp["checkpoint"]

    # --- background analysis -----------------------------------------------------------------------
    job = await call("etabs_run_analysis", background=True)
    busy = await call.session.call_tool("etabs_model_overview", {})
    assert busy.isError and "busy" in busy.content[0].text
    print("  etabs_model_overview              (expected busy while the job runs)")
    status = await call("etabs_job_status", job_id=job["job_id"])
    assert status["status"] in ("running", "done")
    done = await call("etabs_job_wait", job_id=job["job_id"], timeout_seconds=300)
    assert done["status"] == "done" and done["result"]["all_requested_finished"], done
    eqx = await call("etabs_get_results", result="base_reactions", load_cases=["EQX", "RSX", "WX"], load_combinations=[])
    by_case = {}
    for r in eqx["rows"]:
        by_case.setdefault(r["OutputCase"], r)
    assert abs(by_case["EQX"]["FX"]) > 1 and abs(by_case["WX"]["FX"]) > 1 and "RSX" in by_case
    assert all(abs(r["FY"]) < 1e-6 for r in eqx["rows"] if r["OutputCase"] == "EQX"), "X-only seismic leaked into Y"
    modal = await call("etabs_get_results", result="modal_periods", load_cases=["Modal"], load_combinations=[])
    assert len(modal["rows"]) == 9

    design_job = await call("etabs_run_design", kind="steel", background=True)
    design = await call("etabs_job_wait", job_id=design_job["job_id"], timeout_seconds=300)
    assert design["status"] == "done" and design["result"]["members_designed"] == len(frames)

    # --- seeing the results ------------------------------------------------------------------------
    # With seismic loads, ETABS applies AISC 341: default special-moment-frame members that are not seismically
    # compact get a design error (ratio 0). They must be reported, and drawn as failing, never as lightly loaded.
    r1 = await _render(call, "design", color_by="design_ratio", view="iso")
    errored = design["result"]["failing"]
    if r1.get("design_errors"):
        assert r1["design_errors"] == errored and "seismically compact" in r1["example_error"]
    else:
        assert r1["max_ratio"] > 0
    r2 = await _render(call, "deformed_eqx", deformed_case="EQX", color_by="displacement", view="front")
    assert r2["deformed_scale"] > 0
    await _render(call, "sections_light", color_by="section", theme="light", view="iso_back")
    await _render(call, "story2", color_by="story", stories=["Story2"], view="top")
    hot = await call("etabs_find_objects", min_ratio=0.3)
    assert all(o["ratio"] >= 0.3 or o.get("design_error") for o in hot["objects"])
    assert hot["count"] >= errored

    # --- export & report -----------------------------------------------------------------------------
    exp = await call("etabs_export_model", sections=["STORIES", "LOAD PATTERNS"])
    assert any(x.startswith("STORIES") for x in exp["sections"]) and "EQX" in exp["text"]
    assert not any(x.startswith("File ") for x in exp["sections"])
    rep = await call("etabs_design_report", path=str(tmp / "V2_report.md"))
    md = rep["markdown"]
    assert "## Steel design" in md and "## Base reactions" in md and "## Maximum story drifts" in md
    if ARTIFACTS:
        (ARTIFACTS / "V2_report.md").write_text(md, encoding="utf-8")
