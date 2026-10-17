"""Offline tests for v0.3: building generator, concrete and shear-wall design, design codes, code checks,
RS scaling, menu automation (mocked), multiple instances, audit log and HTTP auth."""

from __future__ import annotations

import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from etabs_mcp.settings import Settings


@pytest.mark.asyncio
async def test_generate_concrete_wall_building(harness):
    res = await harness.call("etabs_generate_building", system="concrete_wall", bays_x=["8 m", "6 m"],
                             bays_y=["7 m"], story_heights=["4.5 m", "3.5 m", "3.0 m"])
    c = res["counts"]
    assert c["columns"] == 3 * 2 * 3 and c["slabs"] == 2 * 1 * 3 and c["beams"] == (2 * 2 + 3 * 1) * 3
    assert c["walls"] > 0 and res["piers"] and res["sections"]["wall"] == "GEN-WALL"
    assert res["grid"] == {"x": [0.0, 8.0, 14.0], "y": [0.0, 7.0]}
    # non-uniform heights and grid go through SetStories_2 and the grid table
    st = harness.called("cStory.SetStories_2")[0]
    assert st["StoryHeights"] == [4.5, 3.5, 3.0]
    grid_rows = harness.backend.state.tables["Grid Definitions - Grid Lines"][1]
    assert sorted(float(r[3]) for r in grid_rows if r[1].startswith("X")) == [0.0, 8.0, 14.0]
    assert harness.called("cAreaObj.SetPier") and harness.called("cAreaObj.SetDiaphragm")
    assert {c["LoadPat"] for c in harness.called("cAreaObj.SetLoadUniform")} == {"SDL", "LL"}
    assert harness.called("cCombo.AddDesignDefaultCombos")[0]["DesignConcrete"] is True
    with pytest.raises(ToolError, match="confirmation_required"):
        await harness.call("etabs_generate_building", bays_x=[6], bays_y=[6], story_heights=[3])


@pytest.mark.asyncio
async def test_generate_steel_braced_in_feet(harness):
    res = await harness.call("etabs_generate_building", system="steel_braced", units="kip_ft_F",
                             bays_x=["30 ft", "30 ft", "30 ft"], bays_y=["25 ft", "25 ft"],
                             story_heights=["15 ft", "12 ft"])
    assert res["counts"]["braces"] == 2 * 8 * 2
    assert harness.called("cFile.NewGridOnly")[0]["SpacingX"] == 30.0
    assert not harness.called("cStory.SetStories_2")  # uniform above the first story -> NewGridOnly suffices
    imported = {c["PropName"] for c in harness.called("cPropFrame.ImportProp")}
    assert imported == {"W14X90", "W18X35", "W10X33"}


@pytest.mark.asyncio
async def test_code_checks(harness):
    harness.backend.state.locked = True
    harness.backend.state.cases += ["EQX", "RSX"]
    res = await harness.call("etabs_code_checks", drift_limit=0.02, drift_amplification=4.0)
    by = {r["check"]: r for r in res["results"]}
    assert res["cases"] == ["EQX", "RSX"]
    assert by["drift"]["pass"] is True and by["drift"]["items"][0]["design_drift"] == pytest.approx(0.016)
    assert by["torsion"]["pass"] is False and by["torsion"]["flagged"][0]["irregularity"] == "torsional (1a)"
    soft = by["soft_story"]
    assert soft["pass"] is False and soft["flagged"][0]["story"] == "Story1"  # 500 < 0.7 * 900
    assert by["weight"]["pass"] is False and by["weight"]["flagged"][0]["story"] == "Story1"  # 180 > 1.5 * 100
    assert by["modal_mass"]["pass"] is True and by["modal_mass"]["modes_to_reach_ux"] == 3
    rs = by["rs_scaling"]
    assert rs["ratio"] == pytest.approx(0.8) and rs["required_scale_factor"] == pytest.approx(1.25)
    assert res["summary"]["drift"] == "pass" and res["all_pass"] is False
    only = await harness.call("etabs_code_checks", checks=["modal_mass"], modal_target=0.96)
    assert only["results"][0]["pass"] is False and "hint" in only["results"][0]
    harness.backend.state.locked = False
    with pytest.raises(ToolError, match="analysis results"):
        await harness.call("etabs_code_checks")


@pytest.mark.asyncio
async def test_scale_response_spectrum(harness):
    harness.backend.state.locked = True
    with pytest.raises(ToolError, match="confirmation_required"):
        await harness.call("etabs_scale_response_spectrum", rs_case="RSX", elf_case="EQX")
    res = await harness.call("etabs_scale_response_spectrum", rs_case="RSX", elf_case="EQX", confirm=True)
    assert res["scaled"] and res["factor"] == pytest.approx(1.25)
    assert res["new_scale_factors"][0] == pytest.approx(1.2258 * 1.25)
    assert res["after"]["pass"] is True and res["after"]["ratio"] == pytest.approx(1.0)
    again = await harness.call("etabs_scale_response_spectrum", rs_case="RSX", elf_case="EQX", confirm=True)
    assert again["scaled"] is False


@pytest.mark.asyncio
async def test_concrete_and_wall_design(harness, monkeypatch):
    harness.backend.state.locked = True
    beams = await harness.call("etabs_get_concrete_design", element="beam")
    assert beams["count"] == 2 and beams["rows"][0]["FrameName"] == "B1" and beams["rows"][0]["TopArea"] == 0.0009
    every = await harness.call("etabs_get_concrete_design", element="beam", envelope=False)
    assert every["count"] == 3
    cols = await harness.call("etabs_get_concrete_design", element="column")
    assert cols["rows"][0]["FrameName"] == "C2"
    joints = await harness.call("etabs_get_concrete_design", element="joint")
    assert joints["count"] == 0 and "special" in joints["note"]

    from etabs_mcp.discovery import RunningEtabs
    from etabs_mcp.tools import concrete

    invoked = []
    monkeypatch.setattr(concrete, "main_window_for_pid", lambda pid: RunningEtabs(pid, None, 4242, "ETABS"))
    monkeypatch.setattr(concrete.gui, "invoke_menu", lambda hwnd, path: invoked.append(path))
    monkeypatch.setattr(concrete.gui, "dialogs", lambda pid, hwnd: [])
    monkeypatch.setattr(concrete.gui, "list_menu", lambda hwnd, path=None: ["Shear Wall Design", "Steel Frame Design"])
    harness.backend.pid = 99
    wall = await harness.call("etabs_run_shear_wall_design")
    assert invoked == [["Design", "Shear Wall Design", "Start Design/Check"]]
    assert wall["piers"]["rows"][0]["DCRatio"] == 0.81
    one = await harness.call("etabs_get_wall_design", kind="pier", story="Story1")
    assert one["count"] == 1
    menu = await harness.call("etabs_menu", action="list", path=["Design"])
    assert "Shear Wall Design" in menu["items"]
    with pytest.raises(ToolError, match="confirmation_required"):
        await harness.call("etabs_menu", action="invoke", path=["Analyze", "Check Model..."])
    await harness.call("etabs_menu", action="invoke", path=["Analyze", "Check Model..."], confirm=True)
    assert invoked[-1] == ["Analyze", "Check Model..."]


@pytest.mark.asyncio
async def test_design_codes(harness):
    harness.backend.state.tables["Concrete Frame Design Preferences - ACI 318-19"] = (["Item"], [])
    harness.backend.state.tables["Steel Frame Design Preferences - AISC 360-16"] = (["Item"], [])
    listed = await harness.call("etabs_design_codes", action="list")
    assert listed["available"]["concrete"] == ["ACI 318-19"] and listed["current"]["concrete"] == "ACI 318-19"
    await harness.call("etabs_design_codes", action="set", kind="steel", code="AISC 360-16")
    assert harness.called("cDesignSteel.SetCode")[0]["CodeName"] == "AISC 360-16"


@pytest.mark.asyncio
async def test_multiple_instances():
    from conftest import Harness
    from fakes import FakeBackend

    h = Harness()
    h.session._factory = FakeBackend
    await h.call("etabs_add_frames", frames=[{"start": [0, 0, 0], "end": [0, 0, 3]}])
    res = await h.call("etabs_connect", instance="b")
    assert res["instance"] == "b"
    b = h.session.backend
    assert b is not h.backend and not b.state.frames
    await h.call("etabs_add_frames", frames=[{"start": [0, 0, 0], "end": [0, 0, 3]},
                                             {"start": [6, 0, 0], "end": [6, 0, 3]}])
    listed = await h.call("etabs_instances")
    assert {r["name"] for r in listed["instances"]} == {"default", "b"} and listed["active"] == "b"
    await h.call("etabs_instances", action="use", name="default")
    assert len(h.backend.state.frames) == 1 and len(b.state.frames) == 2
    await h.call("etabs_instances", action="remove", name="b")
    assert (await h.call("etabs_instances"))["active"] == "default"
    with pytest.raises(ToolError, match="No ETABS instance"):
        await h.call("etabs_instances", action="use", name="b")


@pytest.mark.asyncio
async def test_audit_log(make_harness, tmp_path):
    log = tmp_path / "audit.jsonl"
    h = make_harness(Settings(audit_log=log))
    await h.call("etabs_model_overview")
    assert not log.exists()  # reads are not audited
    await h.call("etabs_add_frames", frames=[{"start": [0, 0, 0], "end": [0, 0, 3]}])
    await h.call("etabs_call_api", path="FrameObj.Delete", args={"Name": "1"}, confirm=True)
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    calls = [e["call"] for e in lines]
    assert "SapModel.FrameObj.AddByCoord" in calls and "SapModel.FrameObj.Delete" in calls
    delete = next(e for e in lines if e["call"].endswith("Delete"))
    assert delete["safety"] == "destructive" and delete["args"] == {"Name": "1"} and delete["instance"] == "default"


def test_http_bearer_auth():
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from etabs_mcp.http import BearerAuth, check_exposure

    app = Starlette(routes=[Route("/mcp", lambda r: PlainTextResponse("ok"))])
    client = TestClient(BearerAuth(app, "s3cret"))
    assert client.get("/mcp").status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Bearer s3cret"}).text == "ok"
    check_exposure("127.0.0.1", None)
    with pytest.raises(SystemExit):
        check_exposure("0.0.0.0", None)
    check_exposure("0.0.0.0", "token")
