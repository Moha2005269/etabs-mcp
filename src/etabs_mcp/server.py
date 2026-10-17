"""Builds the FastMCP server: tools, resources and prompts."""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from . import __version__
from .session import EtabsSession
from .settings import Settings
from .tools import analysis, api, connection, geometry, loads, model, properties, tables, view

INSTRUCTIONS = """\
This server drives CSI ETABS (v22 and newer) on this machine through its official API, so you can do what an
engineer does in the ETABS GUI: build and edit models, define properties and loads, run analysis and design,
read results and tables, and look at the model window.

Working rules:
- Start with etabs_model_overview. It connects automatically (attaching to a running ETABS, else launching one).
- Every length/force you send or receive is in the model's *present units* (see etabs_model_overview /
  etabs_set_units). Convert before sending.
- ETABS identifies objects by unique 'name'; the GUI label (e.g. 'B12' on 'Story3') is separate. Use names in
  tool calls; etabs_list_objects shows both.
- After analysis the model is locked; edits fail until etabs_unlock_model, which deletes results. Plan edits
  before analysing.
- Curated tools cover the common workflow. Anything else in ETABS is reachable: find the method with
  etabs_search_api, read its signature with etabs_describe_api, run it with etabs_call_api (or a batch).
  Every model table (definitions, assignments, results) is readable with etabs_get_table and importable
  tables are editable with etabs_edit_table.
- Destructive operations (deleting, replacing the model, unlocking, closing ETABS) need confirm=true. Ask the
  user before confirming.
- Use etabs_screenshot to check what you built, the same way a person glances at the screen.
"""

WORKFLOW_PROMPT = """\
Build, analyse and check a structure in ETABS:
1. etabs_model_overview - see what is open; etabs_new_model (grid_only) or etabs_open_model.
2. etabs_set_units if needed; etabs_get_stories / etabs_set_stories.
3. etabs_define_material, etabs_define_frame_section, etabs_define_area_section.
4. etabs_add_frames (columns and beams), etabs_add_areas (slabs, walls), etabs_set_restraints at the base.
5. etabs_define_load_patterns, etabs_assign_loads, etabs_define_combinations.
6. etabs_screenshot to check the geometry.
7. etabs_run_analysis (save_as for a new model), then etabs_get_results (base_reactions, story_drifts,
   modal_periods ...).
8. etabs_run_design and etabs_get_design_summary; iterate on sections (etabs_unlock_model first).
Task: {task}
"""


def create_server(settings: Settings | None = None, session: EtabsSession | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    session = session or EtabsSession(settings)
    mcp = FastMCP("etabs", instructions=INSTRUCTIONS)
    mcp._etabs_session = session  # type: ignore[attr-defined]  # exposed for tests and shutdown

    for module in (connection, model, geometry, properties, loads, analysis, tables, view, api):
        module.register(mcp, session)

    @mcp.resource("etabs://api/interfaces", mime_type="application/json")
    def api_interfaces() -> str:
        """Every ETABS API interface with its access path and method count."""
        cat = session.catalog
        rows = [
            {"interface": name, "path": cat.interface_path(name), "methods": len(info.methods)}
            for name, info in sorted(cat.interfaces.items())
            if cat.interface_path(name)
        ]
        return json.dumps({"catalog": cat.source, "interfaces": rows}, indent=1)

    @mcp.resource("etabs://api/interface/{name}", mime_type="application/json")
    def api_interface(name: str) -> str:
        """Methods and sub-interfaces of one ETABS API interface, e.g. cFrameObj."""
        return json.dumps(session.catalog.interface_summary(name), indent=1)

    @mcp.resource("etabs://api/enum/{name}", mime_type="application/json")
    def api_enum(name: str) -> str:
        """Values of an ETABS API enum, e.g. eLoadPatternType."""
        cat = session.catalog
        key = next((k for k in cat.enums if k.lower() == name.lower()), None)
        return json.dumps({"enum": key, "values": cat.enums.get(key or "", {})}, indent=1)

    @mcp.resource("etabs://server/info", mime_type="application/json")
    def server_info() -> str:
        """Server version, settings and connection state."""
        return json.dumps(
            {
                "etabs_mcp_version": __version__,
                "read_only": settings.read_only,
                "connect_mode": settings.connect_mode,
                "connection": session.backend.info(),
            },
            indent=1,
            default=str,
        )

    @mcp.prompt(name="etabs_build_and_check")
    def build_and_check(task: str) -> str:
        """Step-by-step workflow for modelling, analysing and designing a structure in ETABS."""
        return WORKFLOW_PROMPT.format(task=task)

    return mcp
