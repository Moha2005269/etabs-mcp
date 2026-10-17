"""Every curated tool, driven through FastMCP against the fake backend.

Because the fake resolves and coerces every API call against the real ETABSv1.dll catalog, these tests
also prove that each tool uses existing methods with correctly named and typed parameters."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from etabs_mcp.settings import Settings

pytestmark = pytest.mark.asyncio


async def test_server_surface(harness):
    tools = await harness.mcp.list_tools()
    names = {t.name for t in tools}
    assert len(tools) >= 40
    assert {"etabs_call_api", "etabs_get_table", "etabs_screenshot", "etabs_run_analysis"} <= names
    for t in tools:
        assert t.description and len(t.description) > 30, t.name
        assert t.annotations is not None, t.name
    reads = {t.name for t in tools if t.annotations.readOnlyHint}
    assert "etabs_model_overview" in reads and "etabs_add_frames" not in reads
    resources = await harness.mcp.list_resources()
    assert any(str(r.uri) == "etabs://api/interfaces" for r in resources)
    prompts = await harness.mcp.list_prompts()
    assert prompts[0].name == "etabs_build_and_check"


async def test_overview_autoconnects(harness):
    ov = await harness.call("etabs_model_overview")
    assert harness.backend.connected
    assert ov["present_units"] == "kN_m_C" and ov["model_open"]
    assert ov["stories"]["stories"][0]["name"] == "Story2"  # top story first
    assert ov["load_patterns"] == ["Dead", "Live"]


async def test_new_model_guard_and_create(harness):
    await harness.call("etabs_add_frames", frames=[{"start": [0, 0, 0], "end": [0, 0, 3]}])
    with pytest.raises(ToolError, match="confirmation_required"):
        await harness.call("etabs_new_model", template="grid_only")
    res = await harness.call("etabs_new_model", template="grid_only", units="kip_ft_F", confirm=True,
                             number_of_stories=4)
    assert res["created"] == "grid_only"
    assert harness.called("cSapModel.InitializeNewModel")[0]["Units"] == "kip_ft_F"
    assert harness.called("cFile.NewGridOnly")[0]["NumberStorys"] == 4


async def test_frames_areas_points_and_listing(harness):
    res = await harness.call("etabs_add_frames", frames=[
        {"start": [0, 0, 0], "end": [0, 0, 3], "section": "C1"},
        {"start": [0, 0, 3], "end": [6, 0, 3], "section": "B1", "name": "B1"},
    ])
    assert res["created"] == ["1", "B1"] and not res["errors"]
    await harness.call("etabs_add_areas", areas=[{"points": [[0, 0, 3], [6, 0, 3], [6, 6, 3], [0, 6, 3]]}])
    await harness.call("etabs_add_points", points=[{"coordinates": [9, 9, 0]}])
    frames = await harness.call("etabs_list_objects", kind="frame")
    assert frames["total"] == 2 and frames["objects"][1]["end"] == [6.0, 0.0, 3.0]
    assert frames["objects"][0]["label"] == "C1"
    areas = await harness.call("etabs_list_objects", kind="area")
    assert areas["objects"][0]["coordinates"][2] == [6.0, 6.0, 3.0]
    on_story = await harness.call("etabs_list_objects", kind="frame", story="Story1", include_labels=False)
    assert on_story["total"] == 2


async def test_restraints_by_elevation(harness):
    await harness.call("etabs_add_frames", frames=[{"start": [0, 0, 0], "end": [0, 0, 3]},
                                                   {"start": [6, 0, 0], "end": [6, 0, 3]}])
    res = await harness.call("etabs_set_restraints", restraint="pinned", at_elevation=0)
    assert len(res["assigned"]) == 2
    assert harness.called("cPointObj.SetRestraint")[0]["Value"] == [True, True, True, False, False, False]
    with pytest.raises(ToolError, match="invalid_arguments"):
        await harness.call("etabs_set_restraints", restraint="fixed")


async def test_assignments_and_selection(harness):
    await harness.call("etabs_assign_section", kind="frame", section="W14X90", group="All")
    assert harness.called("cFrameObj.SetSection")[0]["ItemType"] == "Group"
    await harness.call("etabs_assign_section", kind="area", section="Slab1", selected=True)
    assert harness.called("cAreaObj.SetProperty")[0]["ItemType"] == "SelectedObjects"
    sel = await harness.call("etabs_select", action="objects", kind="frame", names=["1"])
    assert sel["selected"] == {"frame": ["1"], "point": ["1"]}
    with pytest.raises(ToolError, match="exactly one"):
        await harness.call("etabs_assign_section", kind="frame", section="X", names=["1"], group="All")


async def test_properties(harness):
    await harness.call("etabs_define_material", name="C30", material_type="Concrete",
                       elastic_modulus=30e6, unit_weight=25, fc=30000)
    assert harness.called("cPropMaterial.SetOConcrete_1")[0]["Fc"] == 30000.0
    await harness.call("etabs_define_material", name="A992", material_type="Steel",
                       region="United States", standard="ASTM A992", grade="Grade 50")
    assert harness.called("cPropMaterial.AddMaterial")[0]["MatType"] == "Steel"
    # A library material that already exists is reused, never duplicated as 'A992Fy50-1'.
    again = await harness.call("etabs_define_material", name="A992Fy50", material_type="Steel",
                               region="United States", standard="ASTM A992", grade="Grade 50")
    assert again["already_existed"] and len(harness.called("cPropMaterial.AddMaterial")) == 1
    for shape, extra in [
        ("rectangle", {"depth": 0.6, "width": 0.3}),
        ("circle", {"depth": 0.5}),
        ("i_section", {"depth": 0.4, "width": 0.2, "flange_thickness": 0.015, "web_thickness": 0.01}),
        ("tube", {"depth": 0.3, "width": 0.3, "flange_thickness": 0.01, "web_thickness": 0.01}),
        ("pipe", {"depth": 0.3, "web_thickness": 0.01}),
        ("from_library", {"library_file": "AISC16.xml", "library_section": "W14X90"}),
    ]:
        await harness.call("etabs_define_frame_section", name=shape, shape=shape, material="C30", **extra)
    with pytest.raises(ToolError, match="Missing"):
        await harness.call("etabs_define_frame_section", name="x", shape="rectangle", material="C30")
    # ETABS silently imports under a substitute name on collisions; the tool renames it back.
    harness.backend.handlers["cPropFrame.ImportProp"] = lambda a: (
        harness.backend.state.frame_props.append(a["Name"] + "-1") or 0, {})
    res = await harness.call("etabs_define_frame_section", name="W18X35", shape="from_library", material="C30",
                             library_file="AISC16.xml", library_section="W18X35")
    assert res["section"] == "W18X35" and "W18X35" in harness.backend.state.frame_props
    harness.backend.handlers["cPropFrame.ImportProp"] = lambda a: (0, {})
    with pytest.raises(ToolError, match="did not import"):
        await harness.call("etabs_define_frame_section", name="NOPE", shape="from_library", material="C30",
                           library_file="AISC16.xml", library_section="W99X999")
    await harness.call("etabs_define_area_section", name="S200", kind="slab", material="C30", thickness=0.2)
    await harness.call("etabs_define_area_section", name="W300", kind="wall", material="C30", thickness=0.3)
    props = await harness.call("etabs_list_properties")
    assert {"materials", "frame_sections", "area_sections"} <= set(props)


async def test_loads_and_combinations(harness):
    await harness.call("etabs_define_load_patterns", patterns=[{"name": "SDL", "type": "SuperDead"},
                                                              {"name": "EQX", "type": "Quake"}])
    assert harness.called("cLoadPatterns.Add")[1]["MyType"] == "Quake"
    res = await harness.call("etabs_assign_loads", loads=[
        {"kind": "frame_distributed", "pattern": "SDL", "names": ["1"], "value": 5},
        {"kind": "frame_point", "pattern": "SDL", "names": ["1"], "value": 10, "start_distance": 0.5},
        {"kind": "area_uniform", "pattern": "SDL", "group": "All", "value": 2},
        {"kind": "point_force", "pattern": "EQX", "names": ["3"], "forces": [10, 0, 0, 0, 0, 0]},
        {"kind": "area_uniform", "pattern": "SDL", "names": ["1"]},  # missing value -> per-item error
    ])
    assert res["assigned"] == 4 and res["errors"][0]["index"] == 4
    dist = harness.called("cFrameObj.SetLoadDistributed")[0]
    assert dist["Dir"] == 10 and dist["Val1"] == dist["Val2"] == 5.0
    combos = await harness.call("etabs_define_combinations", combinations=[
        {"name": "ULS1", "items": [{"name": "Dead", "scale_factor": 1.4}]},
        {"name": "ENV", "type": "envelope", "items": [{"name": "ULS1"}]},
    ], add_default_design_combos=["steel"])
    assert combos["defined"] == ["ULS1", "ENV"]
    kinds = [c["CNameType"] for c in harness.called("cCombo.SetCaseList")]
    assert kinds == ["LoadCase", "LoadCombo"]
    defs = await harness.call("etabs_list_load_definitions")
    assert [p["name"] for p in defs["load_patterns"]] == ["Dead", "Live"]


async def test_analysis_results_and_design(harness):
    with pytest.raises(ToolError, match="not been saved"):
        await harness.call("etabs_run_analysis")
    with pytest.raises(ToolError, match="no analysis results"):
        await harness.call("etabs_get_results", result="story_drifts")
    run = await harness.call("etabs_run_analysis", save_as=r"C:\tmp\m.EDB", cases=["Dead"])
    assert run["all_requested_finished"]
    flags = harness.called("cAnalyze.SetRunCaseFlag")
    assert flags[0] == {"Name": "", "Run": False, "All": True} and flags[1]["Name"] == "Dead"
    drifts = await harness.call("etabs_get_results", result="story_drifts", where={"Story": "Story2", "OutputCase": "EQX"})
    assert drifts["total_rows"] == 1 and drifts["rows"][0]["Drift"] == 0.004
    assert harness.called("cDatabaseTables.SetLoadCasesSelectedForDisplay")[-1]["LoadCaseList"] == ["Dead", "Live", "Modal"]
    design = await harness.call("etabs_run_design", kind="steel")
    assert design["failing"] == 1 and design["members"][0]["ratio"] == 1.08
    summary = await harness.call("etabs_get_design_summary", kind="concrete")
    assert "beams" in summary and "columns" in summary
    with pytest.raises(ToolError, match="confirmation_required"):
        await harness.call("etabs_unlock_model")
    assert (await harness.call("etabs_unlock_model", confirm=True))["locked"] is False


async def test_tables_list_get_edit(harness):
    listed = await harness.call("etabs_list_tables", contains="frame")
    assert listed["tables"][0]["editable"]
    t = await harness.call("etabs_get_table", table_key="Frame Assignments - Section Properties", limit=1)
    assert t["returned"] == 1 and t["next_offset"] == 1
    res = await harness.call(
        "etabs_edit_table",
        table_key="Frame Assignments - Section Properties",
        updates=[{"match": {"UniqueName": "2"}, "set": {"Section": "B2"}}],
        append_rows=[{"UniqueName": "3", "Story": "Story1", "Section": "B1"}],
    )
    assert res["applied"] and res["rows_after"] == 3
    after = await harness.call("etabs_get_table", table_key="Frame Assignments - Section Properties")
    assert [r["Section"] for r in after["rows"]] == ["C1", "B2", "B1"]
    with pytest.raises(ToolError, match="No row matches"):
        await harness.call("etabs_edit_table", table_key="Frame Assignments - Section Properties",
                           updates=[{"match": {"UniqueName": "99"}, "set": {"Section": "X"}}])
    with pytest.raises(ToolError, match="Unknown field"):
        await harness.call("etabs_edit_table", table_key="Frame Assignments - Section Properties",
                           append_rows=[{"Nope": 1}])
    dry = await harness.call("etabs_edit_table", table_key="Frame Assignments - Section Properties",
                             delete_where=[{"UniqueName": "3"}], apply=False)
    assert not dry["applied"] and harness.called("cDatabaseTables.CancelTableEditing")


async def test_generic_api_tools(harness):
    found = await harness.call("etabs_search_api", query="frame releases")
    assert any(r["path"].endswith("SetReleases") for r in found["results"])
    desc = await harness.call("etabs_describe_api", path="FrameObj.SetReleases")
    assert desc["overloads"][0]["params"][0]["name"] == "Name"
    assert (await harness.call("etabs_describe_api", path="eUnits"))["values"]["kN_m_C"] == 6
    assert "cCaseStaticLinear" == (await harness.call("etabs_describe_api", path="LoadCases.StaticLinear"))["interface"]
    r = await harness.call("etabs_call_api", path="FrameObj.GetNameList")
    assert r["ok"] and r["outputs"]["NumberNames"] == 0
    with pytest.raises(ToolError, match="confirmation_required"):
        await harness.call("etabs_call_api", path="FrameObj.Delete", args={"Name": "1"})
    batch = await harness.call("etabs_call_api_batch", calls=[
        {"path": "SetPresentUnits", "args": {"Units": "N_mm_C"}},
        {"path": "FrameObj.SetSection", "args": {"Name": "1"}},  # missing PropName
        {"path": "GetPresentUnits"},
    ])
    assert batch["executed"] == 2 and batch["failed"] == 1 and batch["results"][1]["error"] == "invalid_arguments"
    resource = await harness.mcp.read_resource("etabs://api/interface/cFrameObj")
    assert "SetReleases" in list(resource)[0].content


async def test_failed_status_reports_lock_hint(harness):
    harness.backend.handlers["cFrameObj.SetSection"] = lambda a: (1, {})
    harness.backend.state.locked = True
    r = await harness.call("etabs_call_api", path="FrameObj.SetSection", args={"Name": "1", "PropName": "X"})
    assert r["ok"] is False and "locked" in r["hint"]


async def test_read_only_mode_blocks_writes(make_harness):
    h = make_harness(Settings(read_only=True))
    assert (await h.call("etabs_model_overview"))["model_open"]
    with pytest.raises(ToolError, match="read-only"):
        await h.call("etabs_add_frames", frames=[{"start": [0, 0, 0], "end": [0, 0, 3]}])
    with pytest.raises(ToolError, match="read-only"):
        await h.call("etabs_call_api", path="SetPresentUnits", args={"Units": "N_m_C"})


async def test_connection_status_and_disconnect(harness):
    st = await harness.call("etabs_status")
    assert st["connected"] is False
    await harness.call("etabs_connect")
    st = await harness.call("etabs_status")
    assert st["connected"] and st["present_units"] == "kN_m_C"
    with pytest.raises(ToolError, match="confirmation_required"):
        await harness.call("etabs_disconnect", close_etabs=True)
    await harness.call("etabs_disconnect")
    assert not harness.backend.connected


async def test_save_model_requires_path_for_untitled(harness):
    with pytest.raises(ToolError, match="never been saved"):
        await harness.call("etabs_save_model")
    res = await harness.call("etabs_save_model", path=r"C:\tmp\x\Model.EDB")
    assert res["saved"].endswith("Model.EDB")
