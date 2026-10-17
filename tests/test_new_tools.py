"""Offline tests for the v0.2 tools: rendering, queries, change tracking, checkpoints, export, reports, labels,
lateral loads, background jobs and unit-aware lengths. API calls are validated against the real catalog."""

from __future__ import annotations

import asyncio
import time

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from etabs_mcp.errors import ArgumentValidationError
from etabs_mcp.units import LengthConverter, length_unit, to_length

FRAMES = [
    {"start": [0, 0, 0], "end": [0, 0, 3]},
    {"start": [6, 0, 0], "end": [6, 0, 3]},
    {"start": [0, 0, 3], "end": [6, 0, 3]},
    {"start": [0, 0, 0], "end": [6, 0, 3]},
]


async def build(h):
    await h.call("etabs_add_frames", frames=FRAMES)
    await h.call("etabs_add_areas", areas=[{"points": [[0, 0, 3], [6, 0, 3], [6, 5, 3], [0, 5, 3]]}])


def test_units():
    assert length_unit("kip_ft_F") == "ft" and length_unit("kN_mm_C") == "mm" and length_unit(None) == "m"
    assert to_length("20 ft", "m") == pytest.approx(6.096)
    assert to_length("12'6\"", "ft") == pytest.approx(12.5)
    assert to_length("250mm", "m") == pytest.approx(0.25)
    assert to_length(3, "ft") == 3.0 and to_length("3", "ft") == 3.0
    assert LengthConverter("kN_m_C").point(["0", "6 m", "300 cm"]) == [0.0, 6.0, 3.0]
    with pytest.raises(ArgumentValidationError):
        to_length("6 furlongs", "m")
    with pytest.raises(ArgumentValidationError):
        to_length(True, "m")


@pytest.mark.asyncio
async def test_unit_aware_geometry(harness):
    await harness.call("etabs_add_frames", frames=[{"start": ["0", "0", "0"], "end": ["20 ft", "0", "300 cm"]}])
    call = harness.called("cFrameObj.AddByCoord")[0]
    assert call["XJ"] == pytest.approx(6.096) and call["ZJ"] == pytest.approx(3.0)
    await harness.call("etabs_define_frame_section", name="R", shape="rectangle", material="C30",
                       depth="600 mm", width="0.3")
    assert harness.called("cPropFrame.SetRectangle")[0]["T3"] == pytest.approx(0.6)
    await harness.call("etabs_define_area_section", name="S", kind="slab", material="C30", thickness="200 mm")
    assert harness.called("cPropArea.SetSlab")[0]["Thickness"] == pytest.approx(0.2)
    await harness.call("etabs_new_model", template="grid_only", units="kip_ft_F", typical_story_height="3.6 m",
                       spacing_x="30", confirm=True)
    grid = harness.called("cFile.NewGridOnly")[0]
    assert grid["TypicalStoryHeight"] == pytest.approx(3.6 / 0.3048) and grid["SpacingX"] == 30.0


@pytest.mark.asyncio
async def test_render_view_variants(harness):
    await build(harness)
    res = await harness.call("etabs_render_view", view="front", color_by="section", theme="light")
    image = next(c for c in res if getattr(c, "type", "") == "image")
    assert len(image.data) > 1000
    with pytest.raises(ToolError, match="design ratios"):
        await harness.call("etabs_render_view", color_by="design_ratio")
    harness.backend.state.locked = True
    res = await harness.call("etabs_render_view", color_by="design_ratio")
    assert "max_ratio" in next(c.text for c in res if getattr(c, "type", "") == "text")
    # deformed shape from the joint displacement table
    pts = harness.backend.state.points
    harness.backend.state.tables["Joint Displacements"][1][:] = [
        ["Story1", n, n, "Dead", "0.01" if z > 0 else "0", "0", "0"] for n, (_, _, z) in pts.items()]
    res = await harness.call("etabs_render_view", deformed_case="Dead", color_by="displacement")
    text = next(c.text for c in res if getattr(c, "type", "") == "text")
    assert "deformed_scale" in text and "'deformed_scale': 0.0" not in text


@pytest.mark.asyncio
async def test_find_objects(harness):
    await build(harness)
    cols = await harness.call("etabs_find_objects", role="column")
    assert cols["count"] == 2
    braces = await harness.call("etabs_find_objects", role="brace")
    assert braces["names"] == ["4"]
    long_ = await harness.call("etabs_find_objects", min_length="6.5 m")
    assert long_["names"] == ["4"]
    boxed = await harness.call("etabs_find_objects", box={"xmin": 5, "xmax": 7})
    assert boxed["names"] == ["2"]
    harness.backend.state.locked = True
    hot = await harness.call("etabs_find_objects", min_ratio=1.0, select=True)
    assert hot["names"] == ["2"] and harness.called("cFrameObj.SetSelected")
    grouped = await harness.call("etabs_find_objects", group="G1")
    assert grouped["names"] == ["1"]
    areas = await harness.call("etabs_find_objects", kind="area")
    assert areas["count"] == 1


@pytest.mark.asyncio
async def test_what_changed(harness):
    await build(harness)
    first = await harness.call("etabs_what_changed")
    assert first["baseline_recorded"]
    same = await harness.call("etabs_what_changed")
    assert same["changed"] is False
    await harness.call("etabs_add_frames", frames=[{"start": [6, 0, 3], "end": [12, 0, 3]}])
    harness.backend.state.frames["1"]["section"] = "W14X90"
    diff = await harness.call("etabs_what_changed")
    assert diff["changes"]["frames"]["added"] == ["5"]
    assert diff["changes"]["frames"]["modified"][0]["property"] == {"from": "Default", "to": "W14X90"}


@pytest.mark.asyncio
async def test_checkpoints(harness, tmp_path):
    with pytest.raises(ToolError, match="saved model"):
        await harness.call("etabs_checkpoint", label="x")
    model = tmp_path / "M.EDB"
    model.write_bytes(b"v1")
    harness.backend.state.filename = str(model)
    cp = await harness.call("etabs_checkpoint", label="before-resize")
    assert cp["checkpoint"].startswith("M__before-resize__")
    listed = await harness.call("etabs_list_checkpoints")
    assert listed["checkpoints"][0]["label"] == "before-resize"
    with pytest.raises(ToolError, match="confirmation_required"):
        await harness.call("etabs_restore_checkpoint", checkpoint="before-resize")
    res = await harness.call("etabs_restore_checkpoint", checkpoint="before-resize", confirm=True)
    assert res["model"].endswith("M.EDB")
    opened = harness.called("cFile.OpenFile")[0]["FileName"]
    assert "before-resize" in opened
    with pytest.raises(ToolError, match="No checkpoint"):
        await harness.call("etabs_restore_checkpoint", checkpoint="nope", confirm=True)


@pytest.mark.asyncio
async def test_export_and_report(harness, tmp_path):
    out = tmp_path / "m.e2k"
    exp = await harness.call("etabs_export_model", path=str(out))
    assert any(x.startswith("STORIES") for x in exp["sections"]) and "PROGRAM" in exp["text"]
    only = await harness.call("etabs_export_model", path=str(out), sections=["FRAME ASSIGNS"])
    assert "LINEASSIGN" in only["text"] and "STORY" not in only["text"]
    await build(harness)
    rep = await harness.call("etabs_design_report", path=str(tmp_path / "r.md"))
    assert "not been analysed" in rep["markdown"] and (tmp_path / "r.md").is_file()
    harness.backend.state.locked = True
    rep = await harness.call("etabs_design_report", path=str(tmp_path / "r.md"))
    assert "## Steel design" in rep["markdown"] and "1.08" in rep["markdown"]


@pytest.mark.asyncio
async def test_labels(harness):
    await build(harness)
    await harness.call("etabs_assign_label", label_type="group", label="ROOF", object_kind="frame", names=["3"])
    assert harness.called("cGroup.SetGroup")[0]["Name"] == "ROOF"
    assert harness.called("cFrameObj.SetGroupAssign")[0]["GroupName"] == "ROOF"
    await harness.call("etabs_assign_label", label_type="pier", label="P1", names=["1"])
    assert harness.called("cAreaObj.SetPier")[0]["PierName"] == "P1"
    await harness.call("etabs_assign_label", label_type="spandrel", label="S1", object_kind="frame", selected=True)
    assert harness.called("cFrameObj.SetSpandrel")[0]["ItemType"] == "SelectedObjects"
    with pytest.raises(ToolError, match="frames and areas"):
        await harness.call("etabs_assign_label", label_type="pier", label="P", object_kind="point", names=["1"])


@pytest.mark.asyncio
async def test_lateral_loads(harness):
    eq = await harness.call("etabs_define_seismic_load", name="EQX", directions=["X", "X+E"], r=8, site_class="C")
    assert eq["stories"] == ["Base", "Story2"]
    call = harness.called("cAutoSeismic.SetASCE716_1")[0]
    assert call["nDir"] == [True, False, True, False, False, False] and call["SiteClass"] == 3
    fields, rows = harness.backend.state.tables["Load Pattern Definitions - Auto Seismic - ASCE 7-16"]
    row = dict(zip(fields, rows[0]))
    assert (row["XDir"], row["XDirPlusE"], row["XDirMinusE"], row["YDir"]) == ("Yes", "Yes", "No", "No")
    assert row["TopStory"] == "Story2" and row["BotStory"] == "Base"
    await harness.call("etabs_define_wind_load", name="WX", wind_speed=120, exposure="B")
    wrow = dict(zip(*[harness.backend.state.tables["Load Pattern Definitions - Auto Wind - ASCE 7-16"][0],
                      harness.backend.state.tables["Load Pattern Definitions - Auto Wind - ASCE 7-16"][1][0]]))
    assert wrow["WindSpeed"] == "120.0" and wrow["ExpType"] == "B" and wrow["TopStory"] == "Story2"
    with pytest.raises(ToolError, match="Unknown field"):
        await harness.call("etabs_define_wind_load", name="WY", fields={"Bogus": 1})
    rs = await harness.call("etabs_define_response_spectrum", case_name="RSX", importance=1.25, r=8)
    assert rs["scale_factor"] == pytest.approx(9.80665 * 1.25 / 8)
    assert harness.called("cCaseResponseSpectrum.SetLoads")[0]["Func"] == ["ASCE7-16-RS"]
    await harness.call("etabs_set_mass_source", loads=[{"pattern": "SDL"}, {"pattern": "LL", "multiplier": 0.25}])
    ms = harness.called("cPropMaterial.SetMassSource_1")[0]
    assert ms["LoadPat"] == ["SDL", "LL"] and ms["sf"] == [1.0, 0.25] and ms["IncludeLoads"] is True
    await build(harness)
    d = await harness.call("etabs_assign_diaphragm", name="D1", stories=["Story1"])
    assert d["areas_assigned"] == 1 and harness.called("cDiaphragm.SetDiaphragm")[0]["SemiRigid"] is False
    await harness.call("etabs_set_modal_case", max_modes=24)
    assert harness.called("cCaseModalEigen.SetNumberModes")[0]["MaxModes"] == 24


@pytest.mark.asyncio
async def test_background_jobs(harness, tmp_path):
    harness.backend.state.filename = str(tmp_path / "Job.EDB")
    started = await harness.call("etabs_run_analysis", background=True)
    assert started["status"] == "running" and started["job_id"] == "job-1"
    done = await harness.call("etabs_job_wait", job_id="job-1", timeout_seconds=5)
    assert done["status"] == "done" and done["result"]["all_requested_finished"]
    assert (await harness.call("etabs_job_status"))["status"] == "done"

    # While a job owns ETABS, other tools fail fast with 'busy' instead of hanging.
    def slow(a):
        time.sleep(0.6)
        return 0, {}

    harness.backend.handlers["cDesignSteel.StartDesign"] = slow
    job = await harness.call("etabs_run_design", kind="steel", background=True)
    with pytest.raises(ToolError, match="busy"):
        await harness.call("etabs_model_overview")
    status = await harness.call("etabs_job_status", job_id=job["job_id"])
    assert status["status"] == "running"
    waited = await harness.call("etabs_job_wait", job_id=job["job_id"], timeout_seconds=5)
    assert waited["status"] == "done" and waited["result"]["failing"] == 1
    await asyncio.sleep(0)
    assert (await harness.call("etabs_model_overview"))["model_open"]
    with pytest.raises(ToolError, match="Unknown job"):
        await harness.call("etabs_job_status", job_id="job-99")
