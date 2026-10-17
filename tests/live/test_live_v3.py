"""Live end-to-end test of the v0.3 tools (generator, concrete and shear-wall design, code checks, RS scaling,
menu automation) against a real ETABS, through MCP over stdio.

    pytest tests/live/test_live_v3.py --live -s
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .test_live_e2e import Client

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


async def test_v3_tools(tmp_path: Path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "etabs_mcp", "--connect-mode", "start"],
                                   env={**os.environ, "PYTHONUNBUFFERED": "1"})
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        call = Client(session)
        try:
            await _concrete_wall_building(call, tmp_path)
            await _steel_braced_building(call, tmp_path)
        finally:
            await session.call_tool("etabs_disconnect", {"close_etabs": True, "confirm": True})


async def _concrete_wall_building(call: Client, tmp: Path) -> None:
    await call("etabs_connect", mode="start")
    gen = await call("etabs_generate_building", system="concrete_wall", bays_x=["8 m", "6 m", "8 m"],
                     bays_y=["7 m", "7 m"], story_heights=["4.5 m", "3.5 m", "3.5 m", "3.5 m"], confirm=True)
    c = gen["counts"]
    assert c["columns"] == 4 * 3 * 4 and c["slabs"] == 4 * 3 * 2 and c["walls"] > 0 and gen["piers"]
    assert gen["grid"]["x"] == pytest.approx([0, 8, 14, 22]) and gen["stories"][0]["elevation"] == pytest.approx(15.0)
    grid = await call("etabs_get_table", table_key="Grid Definitions - Grid Lines")
    xo = sorted(r["Ordinate"] for r in grid["rows"] if r["LineType"].startswith("X"))
    assert xo == pytest.approx([0, 8, 14, 22])

    await call("etabs_set_mass_source", loads=[{"pattern": "SDL"}, {"pattern": "LL", "multiplier": 0.25}])
    await call("etabs_set_modal_case", max_modes=12)
    await call("etabs_define_seismic_load", name="EQX", directions=["X"], r=5, omega=2.5, cd=4.5)
    await call("etabs_define_seismic_load", name="EQY", directions=["Y"], r=5, omega=2.5, cd=4.5)
    await call("etabs_define_response_spectrum", case_name="RSX", direction="U1", r=5)
    codes = await call("etabs_design_codes", action="list")
    assert "ACI 318-19" in codes["available"]["concrete"]
    await call("etabs_design_codes", action="set", kind="concrete", code="ACI 318-19")
    await call("etabs_save_model", path=str(tmp / "Wall.EDB"))
    job = await call("etabs_run_analysis", background=True)
    done = await call("etabs_job_wait", job_id=job["job_id"], timeout_seconds=600)
    assert done["status"] == "done" and done["result"]["all_requested_finished"], done

    checks = await call("etabs_code_checks", rs_case="RSX", elf_case="EQX", drift_amplification=4.5)
    names = {r["check"] for r in checks["results"]}
    assert names == {"drift", "torsion", "soft_story", "weight", "modal_mass", "rs_scaling"}
    rs = next(r for r in checks["results"] if r["check"] == "rs_scaling")
    assert rs["v_rs"] > 0 and rs["v_elf"] > 0
    modal = next(r for r in checks["results"] if r["check"] == "modal_mass")
    assert modal["modes"] == 12
    drift = next(r for r in checks["results"] if r["check"] == "drift")
    assert drift["items"] and all(i["design_drift"] == pytest.approx(i["elastic_drift"] * 4.5, rel=1e-6) for i in drift["items"])

    if not rs["pass"]:
        scaled = await call("etabs_scale_response_spectrum", rs_case="RSX", elf_case="EQX", confirm=True)
        assert scaled["scaled"] and scaled["after"]["ratio"] == pytest.approx(1.0, abs=0.01), scaled

    design = await call("etabs_run_design", kind="concrete")
    assert design["design"] == "concrete"
    beams = await call("etabs_get_concrete_design", element="beam", limit=5)
    assert beams["count"] > 0 and beams["rows"][0]["TopArea"] >= 0
    cols = await call("etabs_get_concrete_design", element="column", limit=5)
    assert cols["count"] == 48
    joints = await call("etabs_get_concrete_design", element="joint", limit=5)
    assert joints["count"] > 0 or "special" in joints["note"]

    wall = await call("etabs_run_shear_wall_design")
    assert wall["piers"]["count"] > 0, wall
    piers = await call("etabs_get_wall_design", kind="pier", story="Story1")
    assert piers["count"] > 0 and all(r["Story"] == "Story1" for r in piers["rows"])

    menus = await call("etabs_menu", action="list", path=["Design"])
    assert "Shear Wall Design" in menus["items"]
    dlg = await call("etabs_dialogs", action="list")
    assert isinstance(dlg["dialogs"], list)


async def _steel_braced_building(call: Client, tmp: Path) -> None:
    gen = await call("etabs_generate_building", system="steel_braced", bays_x=["30 ft", "30 ft", "30 ft"],
                     bays_y=["25 ft", "25 ft"], story_heights=["15 ft", "12 ft", "12 ft"], units="kip_ft_F",
                     superimposed_dead=0.015, live=0.05, confirm=True)
    c = gen["counts"]
    assert c["columns"] == 4 * 3 * 3 and c["braces"] == 2 * 8 * 3 and gen["sections"]["column"] == "W14X90"
    assert gen["grid"]["x"] == pytest.approx([0, 30, 60, 90])
    await call("etabs_define_seismic_load", name="EQX", directions=["X"], r=6, omega=2, cd=5)
    await call("etabs_save_model", path=str(tmp / "Braced.EDB"))
    run = await call("etabs_run_analysis")
    assert run["all_requested_finished"]
    base = await call("etabs_get_results", result="base_reactions", load_cases=["LL"], load_combinations=[])
    fz = base["rows"][0]["FZ"]
    assert fz == pytest.approx(0.05 * 90 * 50 * 3, rel=1e-3)          # 0.05 ksf × plan area × 3 floors
    design = await call("etabs_run_design", kind="steel")
    assert design["members_designed"] == c["columns"] + c["beams"] + c["braces"]
    braces = await call("etabs_find_objects", role="brace")
    assert braces["count"] == c["braces"]
