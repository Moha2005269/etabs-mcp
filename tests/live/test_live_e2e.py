"""End-to-end test against a real ETABS, through the real MCP protocol.

An MCP client launches `python -m etabs_mcp` over stdio (exactly as Claude Code, Codex or any MCP host does),
starts a fresh ETABS instance, models a two-storey steel frame with a slab, analyses it, designs it, reads
results and tables, edits a table, and closes ETABS again.

    pytest tests/live --live -s

Set ETABS_MCP_ARTIFACTS=<dir> to keep screenshots and the transcript of every call.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

ARTIFACTS = Path(os.environ.get("ETABS_MCP_ARTIFACTS", "")) if os.environ.get("ETABS_MCP_ARTIFACTS") else None


class Client:
    def __init__(self, session: ClientSession):
        self.session = session
        self.log: list[dict[str, Any]] = []

    async def __call__(self, tool: str, **args: Any) -> Any:
        t0 = time.monotonic()
        res = await self.session.call_tool(tool, args)
        dt = round(time.monotonic() - t0, 2)
        text = " ".join(c.text for c in res.content if getattr(c, "type", "") == "text")
        self.log.append({"tool": tool, "args": args, "seconds": dt, "error": res.isError,
                         "result": (res.structuredContent or text)})
        print(f"  {tool:32s} {dt:6.2f}s {'ERROR ' + text[:200] if res.isError else ''}")
        assert not res.isError, f"{tool} failed: {text}"
        if res.structuredContent is not None:
            sc = res.structuredContent
            return sc["result"] if set(sc) == {"result"} else sc
        return res.content

    async def expect_error(self, tool: str, needle: str, **args: Any) -> str:
        res = await self.session.call_tool(tool, args)
        text = " ".join(c.text for c in res.content if getattr(c, "type", "") == "text")
        assert res.isError and needle in text, text
        print(f"  {tool:32s}  (expected error: {needle})")
        return text

    async def screenshot(self, name: str) -> bytes:
        res = await self.session.call_tool("etabs_screenshot", {"zoom_to_fit": True})
        assert not res.isError, res.content
        img = next(c for c in res.content if c.type == "image")
        png = base64.b64decode(img.data)
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10_000
        if ARTIFACTS:
            ARTIFACTS.mkdir(parents=True, exist_ok=True)
            (ARTIFACTS / f"{name}.png").write_bytes(png)
        print(f"  etabs_screenshot -> {name}.png ({len(png) // 1024} KB)")
        return png


async def test_full_workflow(tmp_path: Path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "etabs_mcp", "--connect-mode", "start"],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    model_path = tmp_path / "LiveTest.EDB"
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.serverInfo.name == "etabs"
        call = Client(session)
        try:
            await _workflow(call, model_path)
        finally:
            if ARTIFACTS:
                ARTIFACTS.mkdir(parents=True, exist_ok=True)
                (ARTIFACTS / "live_transcript.json").write_text(json.dumps(call.log, indent=1, default=str))
            await session.call_tool("etabs_disconnect", {"close_etabs": True, "confirm": True})


async def _workflow(call: Client, model_path: Path) -> None:
    tools = await call.session.list_tools()
    assert len(tools.tools) >= 40

    info = await call("etabs_connect", mode="start")
    assert info["connected"] and info["started_by_server"]
    assert int(info["etabs_version"].split(".")[0]) >= 22

    # --- model -------------------------------------------------------------------------------
    await call("etabs_new_model", template="grid_only", units="kN_m_C", number_of_stories=2,
               typical_story_height=3.5, bottom_story_height=4.0, grid_lines_x=3, grid_lines_y=2,
               spacing_x=6.0, spacing_y=5.0, confirm=True)
    stories = await call("etabs_get_stories")
    assert [s["name"] for s in stories["stories"]] == ["Story2", "Story1"]
    assert stories["stories"][0]["elevation"] == pytest.approx(7.5)

    # --- properties --------------------------------------------------------------------------
    await call("etabs_define_material", name="A992Fy50", material_type="Steel",
               region="United States", standard="ASTM A992", grade="Grade 50")
    await call("etabs_define_material", name="C30", material_type="Concrete", elastic_modulus=2.7e7,
               poisson_ratio=0.2, unit_weight=25.0, fc=30000.0)
    await call("etabs_define_frame_section", name="W14X90", shape="from_library", material="A992Fy50",
               library_file="AISC16.xml", library_section="W14X90")
    await call("etabs_define_frame_section", name="W18X35", shape="from_library", material="A992Fy50",
               library_file="AISC16.xml", library_section="W18X35")
    await call("etabs_define_area_section", name="SLAB200", kind="slab", material="C30", thickness=0.2)
    props = await call("etabs_list_properties")
    assert {"W14X90", "W18X35"} <= {s["name"] for s in props["frame_sections"]}
    assert "SLAB200" in props["area_sections"]

    # --- geometry ----------------------------------------------------------------------------
    xs, ys, levels = [0.0, 6.0, 12.0], [0.0, 5.0], [0.0, 4.0, 7.5]
    columns = [{"start": [x, y, levels[i]], "end": [x, y, levels[i + 1]], "section": "W14X90"}
               for i in range(2) for x in xs for y in ys]
    beams = []
    for z in levels[1:]:
        for y in ys:
            beams += [{"start": [xs[k], y, z], "end": [xs[k + 1], y, z], "section": "W18X35"} for k in range(2)]
        for x in xs:
            beams.append({"start": [x, ys[0], z], "end": [x, ys[1], z], "section": "W18X35"})
    res = await call("etabs_add_frames", frames=columns + beams)
    assert res["count"] == len(columns) + len(beams) == 26 and not res["errors"]
    slabs = [{"points": [[xs[k], 0, z], [xs[k + 1], 0, z], [xs[k + 1], 5, z], [xs[k], 5, z]], "property": "SLAB200"}
             for z in levels[1:] for k in range(2)]
    assert (await call("etabs_add_areas", areas=slabs))["count"] == 4
    base = await call("etabs_set_restraints", restraint="fixed", at_elevation=0.0)
    assert len(base["assigned"]) == 6

    listed = await call("etabs_list_objects", kind="frame", limit=500)
    assert listed["total"] == 26
    assert {o["story"] for o in listed["objects"]} == {"Story1", "Story2"}
    areas = await call("etabs_list_objects", kind="area")
    assert areas["total"] == 4 and all(len(a["coordinates"]) == 4 for a in areas["objects"])
    assert {a["property"] for a in areas["objects"]} == {"SLAB200"}

    # --- loads -------------------------------------------------------------------------------
    await call("etabs_define_load_patterns", patterns=[
        {"name": "SDL", "type": "SuperDead"},
        {"name": "LL", "type": "Live"},
        {"name": "WX", "type": "Wind"},
    ])
    slab_names = [a["name"] for a in areas["objects"]]
    beam_names = [o["name"] for o in listed["objects"] if o["start"][2] == o["end"][2]]
    roof_pts = [p for p in (await call("etabs_list_objects", kind="point", include_labels=False))["objects"]
                if p["z"] == pytest.approx(7.5)]
    loads = await call("etabs_assign_loads", loads=[
        {"kind": "area_uniform", "pattern": "SDL", "names": slab_names, "value": 1.5},
        {"kind": "area_uniform", "pattern": "LL", "names": slab_names, "value": 3.0},
        {"kind": "frame_distributed", "pattern": "SDL", "names": beam_names, "value": 2.0},
        {"kind": "point_force", "pattern": "WX", "names": [p["name"] for p in roof_pts], "forces": [10, 0, 0, 0, 0, 0]},
    ])
    assert loads["assigned"] == 4 and not loads["errors"]
    combos = await call("etabs_define_combinations", combinations=[
        {"name": "1.2D+1.6L", "items": [{"name": "Dead", "scale_factor": 1.2}, {"name": "SDL", "scale_factor": 1.2},
                                         {"name": "LL", "scale_factor": 1.6}]},
        {"name": "1.2D+1.0W+L", "items": [{"name": "Dead", "scale_factor": 1.2}, {"name": "SDL", "scale_factor": 1.2},
                                           {"name": "WX", "scale_factor": 1.0}, {"name": "LL", "scale_factor": 1.0}]},
    ], add_default_design_combos=["steel"])
    assert {"1.2D+1.6L", "1.2D+1.0W+L"} <= set(combos["all_combinations"])
    defs = await call("etabs_list_load_definitions")
    assert {p["name"] for p in defs["load_patterns"]} >= {"Dead", "SDL", "LL", "WX"}

    sel = await call("etabs_select", action="objects", kind="frame", names=beam_names[:3])
    assert len(sel["selected"]["frame"]) == 3
    await call("etabs_select", action="clear")
    await call.screenshot("01_model")

    # --- generic API: anything else ETABS can do ----------------------------------------------
    found = await call("etabs_search_api", query="frame releases")
    assert any(r["path"].endswith("FrameObj.SetReleases") for r in found["results"])
    rel = await call("etabs_call_api", path="FrameObj.SetReleases", args={
        "Name": beam_names[0], "II": [False, False, False, False, False, True],
        "JJ": [False, False, False, False, False, True], "StartValue": [0.0] * 6, "EndValue": [0.0] * 6})
    assert rel["ok"], rel
    back = await call("etabs_call_api", path="FrameObj.GetReleases", args={"Name": beam_names[0]})
    assert back["outputs"]["II"][5] is True and back["outputs"]["JJ"][5] is True
    batch = await call("etabs_call_api_batch", calls=[
        {"path": "GroupDef.SetGroup", "args": {"Name": "ROOF_BEAMS"}},
        *[{"path": "FrameObj.SetGroupAssign", "args": {"Name": n, "GroupName": "ROOF_BEAMS"}} for n in beam_names[-7:]],
    ])
    assert batch["failed"] == 0
    bad = await call("etabs_call_api", path="FrameObj.GetSection", args={"Name": "DOES_NOT_EXIST"})
    assert bad["ok"] is False and bad["ret"] != 0
    await call.expect_error("etabs_call_api", "invalid_arguments", path="FrameObj.SetSection", args={"Name": "1"})
    await call.expect_error("etabs_call_api", "confirmation_required", path="FrameObj.Delete", args={"Name": "1"})

    # --- analysis ----------------------------------------------------------------------------
    run = await call("etabs_run_analysis", save_as=str(model_path))
    assert run["all_requested_finished"], run
    status = await call("etabs_analysis_status")
    assert status["locked"]
    reactions = await call("etabs_get_results", result="base_reactions", load_cases=["Dead", "SDL", "LL"],
                           load_combinations=[])
    fz = {r["OutputCase"]: r["FZ"] for r in reactions["rows"]}
    slab_area = 12.0 * 5.0 * 2
    assert fz["LL"] == pytest.approx(3.0 * slab_area, rel=1e-3)
    assert fz["SDL"] == pytest.approx(1.5 * slab_area + 2.0 * sum(
        abs(o["end"][0] - o["start"][0]) + abs(o["end"][1] - o["start"][1])
        for o in listed["objects"] if o["name"] in beam_names), rel=1e-3)
    drifts = await call("etabs_get_results", result="story_drifts", load_cases=["WX"], load_combinations=[])
    assert drifts["total_rows"] > 0 and all(r["Drift"] > 0 for r in drifts["rows"])
    modal = await call("etabs_get_results", result="modal_periods", load_cases=["Modal"], load_combinations=[])
    assert modal["rows"][0]["Period"] > 0
    disp = await call("etabs_get_results", result="joint_displacements", load_cases=["WX"], load_combinations=[],
                      limit=5)
    assert disp["returned"] == 5 and "next_offset" in disp
    await call.expect_error("etabs_add_frames", "locked", frames=[{"start": [0, 0, 0], "end": [1, 1, 1]}]) \
        if False else None  # editing while locked reports a per-item error instead, checked below
    locked_edit = await call("etabs_add_frames", frames=[{"start": [20, 0, 0], "end": [20, 0, 4]}])
    assert locked_edit["errors"] and "locked" in locked_edit["errors"][0].get("hint", "")

    # --- design ------------------------------------------------------------------------------
    design = await call("etabs_run_design", kind="steel")
    assert design["members_designed"] == 26 and design["max_ratio"] > 0
    await call.screenshot("02_after_design")

    # --- tables ------------------------------------------------------------------------------
    tables = await call("etabs_list_tables", contains="frame assignments")
    keys = {t["key"]: t for t in tables["tables"]}
    assert "Frame Assignments - Section Properties" in keys
    tbl = await call("etabs_get_table", table_key="Frame Assignments - Section Properties")
    assert tbl["total_rows"] == 26

    await call("etabs_unlock_model", confirm=True)
    assert not (await call("etabs_analysis_status"))["locked"]
    first_beam_label = tbl["rows"][0]
    edit = await call("etabs_edit_table", table_key="Frame Assignments - Section Properties",
                      updates=[{"match": {"UniqueName": first_beam_label["UniqueName"]}, "set": {"Section Property": "W14X90"}}]
                      if "Section Property" in tbl["fields"] else
                      [{"match": {"UniqueName": first_beam_label["UniqueName"]}, "set": {tbl["fields"][-1]: "W14X90"}}])
    assert edit["applied"] and edit["fatal_errors"] == 0, edit
    sec = await call("etabs_call_api", path="FrameObj.GetSection", args={"Name": first_beam_label["UniqueName"]})
    assert sec["outputs"]["PropName"] == "W14X90"

    saved = await call("etabs_save_model")
    assert saved["saved"].lower().endswith("livetest.edb")
    overview = await call("etabs_model_overview")
    assert overview["counts"]["frames"] == 26 and overview["counts"]["areas"] == 4
