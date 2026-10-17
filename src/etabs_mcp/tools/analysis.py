"""Analysis, results and design tools."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..errors import ITEM_ERRORS, ArgumentValidationError
from ..session import EtabsSession
from .common import DESTRUCTIVE, READ, WRITE, model_file, require_confirm
from .tables import read_table, select_cases_for_display

CASE_STATUS = {1: "not run", 2: "could not start", 3: "not finished", 4: "finished"}

# Friendly result names -> ETABS table keys (identical across ETABS 22+).
RESULT_TABLES = {
    "base_reactions": "Base Reactions",
    "story_drifts": "Story Drifts",
    "story_max_avg_drifts": "Story Max Over Avg Drifts",
    "story_forces": "Story Forces",
    "story_stiffness": "Story Stiffness",
    "joint_displacements": "Joint Displacements",
    "joint_reactions": "Joint Reactions",
    "joint_drifts": "Joint Drifts",
    "diaphragm_com_displacements": "Diaphragm Center Of Mass Displacements",
    "centers_of_mass_and_rigidity": "Centers Of Mass And Rigidity",
    "modal_periods": "Modal Periods And Frequencies",
    "modal_mass_ratios": "Modal Participating Mass Ratios",
    "modal_load_participation": "Modal Load Participation Ratios",
    "column_forces": "Element Forces - Columns",
    "beam_forces": "Element Forces - Beams",
    "brace_forces": "Element Forces - Braces",
    "pier_forces": "Pier Forces",
    "spandrel_forces": "Spandrel Forces",
    "mass_summary_by_story": "Mass Summary by Story",
}
ResultKind = Literal[tuple(RESULT_TABLES)]  # type: ignore[valid-type]

DESIGN_PATHS = {
    "steel": "DesignSteel.StartDesign",
    "concrete": "DesignConcrete.StartDesign",
    "composite_beam": "DesignCompositeBeam.StartDesign",
    "composite_column": "DesignCompositeColumn.StartDesign",
    "concrete_shell": "DesignConcreteShell.StartDesign",
    "concrete_slab": "DesignConcreteSlab.StartSlabDesign",
}


def case_status(session: EtabsSession) -> list[dict[str, Any]]:
    o = session.out("Analyze.GetCaseStatus")
    run = session.out("Analyze.GetRunCaseFlag")
    flags = dict(zip(run.get("CaseName") or [], run.get("Run") or []))
    return [
        {"case": c, "status": CASE_STATUS.get(s, s), "set_to_run": flags.get(c)}
        for c, s in zip(o.get("CaseName") or [], o.get("Status") or [])
    ]


def register(mcp: FastMCP, session: EtabsSession) -> None:
    @mcp.tool(annotations=WRITE)
    async def etabs_run_analysis(
        cases: Annotated[list[str] | None, Field(description="Load cases to run (default: all).")] = None,
        save_as: Annotated[str | None, Field(description="Save to this .EDB path first. Required for a never-saved model.")] = None,
    ) -> dict[str, Any]:
        """Run the analysis (Analyze > Run Analysis). ETABS needs a saved model, saves it again before running,
        and locks the model afterwards. Large models can take minutes; the call returns when ETABS finishes."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            if save_as:
                Path(save_as).parent.mkdir(parents=True, exist_ok=True)
                session.call("File.Save", {"FileName": save_as}, _internal=True)
            current = model_file(session)
            if not current:
                raise ArgumentValidationError(
                    "The model has not been saved, and ETABS cannot analyse an untitled model.",
                    hint="Pass save_as, e.g. C:\\\\Models\\\\Building.EDB",
                )
            if cases:
                session.call("Analyze.SetRunCaseFlag", {"Name": "", "Run": False, "All": True}, _internal=True)
                for c in cases:
                    session.call("Analyze.SetRunCaseFlag", {"Name": c, "Run": True}, _internal=True)
            else:
                session.call("Analyze.SetRunCaseFlag", {"Name": "", "Run": True, "All": True}, _internal=True)
            t0 = time.monotonic()
            session.call("Analyze.RunAnalysis", _internal=True)
            statuses = case_status(session)
            failed = [s["case"] for s in statuses if s["set_to_run"] and s["status"] != "finished"]
            return {
                "model": current,
                "seconds": round(time.monotonic() - t0, 1),
                "all_requested_finished": not failed,
                "not_finished": failed,
                "cases": statuses,
            }

        return await session.run(work)

    @mcp.tool(annotations=READ)
    async def etabs_analysis_status() -> dict[str, Any]:
        """Per-case analysis status (not run / finished ...), run flags, and whether the model is locked."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            return {"locked": session.value("GetModelIsLocked"), "cases": case_status(session)}

        return await session.run(work)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def etabs_unlock_model(
        confirm: Annotated[bool, Field(description="Unlocking deletes all analysis and design results. Ask the user first.")] = False,
    ) -> dict[str, Any]:
        """Unlock the model so it can be edited after analysis (the padlock button). Deletes analysis results."""

        def work() -> dict[str, Any]:
            require_confirm(confirm, session, "Unlocking deletes analysis results and")
            session.call("SetModelIsLocked", {"Lockit": False}, _internal=True)
            return {"locked": session.value("GetModelIsLocked")}

        return await session.run(work)

    @mcp.tool(annotations=READ)
    async def etabs_get_results(
        result: Annotated[ResultKind, Field(description="Which result table to read.")],
        load_cases: Annotated[list[str] | None, Field(description="Load cases to include. Omit both lists for every case and combination.")] = None,
        load_combinations: list[str] | None = None,
        where: Annotated[dict[str, Any] | None, Field(description="Row filter, e.g. {'Story': 'Story2'} or {'OutputCase': 'Dead'}.")] = None,
        group: str = "",
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int | None, Field(ge=1, le=20000)] = None,
    ) -> dict[str, Any]:
        """Read analysis results the way they appear in ETABS tables: base reactions, story drifts and forces,
        joint displacements/reactions, modal periods and mass ratios, element forces, piers and spandrels.
        For any other result table use etabs_get_table."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            if not session.value("GetModelIsLocked"):
                raise ArgumentValidationError(
                    "There are no analysis results (the model is not analysed).",
                    hint="Run etabs_run_analysis first.",
                )
            shown = select_cases_for_display(session, load_cases, load_combinations)
            res = read_table(session, RESULT_TABLES[result], group=group, where=where, offset=offset, limit=limit)
            res["cases_shown"] = shown
            return res

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_run_design(
        kind: Annotated[
            Literal["steel", "concrete", "composite_beam", "composite_column", "concrete_shell", "concrete_slab"],
            Field(description="Shear wall design cannot be started through the ETABS API; read its results with etabs_get_table."),
        ],
        summary_limit: Annotated[int, Field(ge=0, le=5000, description="Members to return in the summary, worst first.")] = 50,
    ) -> dict[str, Any]:
        """Run design (Design > ... > Start Design/Check) with the model's current design codes and preferences.
        Needs analysis results. Steel and concrete return a member summary, worst first."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            if not session.value("GetModelIsLocked"):
                raise ArgumentValidationError("Design needs analysis results.", hint="Run etabs_run_analysis first.")
            t0 = time.monotonic()
            session.call(DESIGN_PATHS[kind], _internal=True)
            out: dict[str, Any] = {"design": kind, "seconds": round(time.monotonic() - t0, 1)}
            if kind in ("steel", "concrete"):
                out.update(design_summary(session, kind, summary_limit))
            else:
                out["note"] = "Read detailed results with etabs_list_tables(contains='design') and etabs_get_table."
            return out

        return await session.run(work)

    @mcp.tool(annotations=READ)
    async def etabs_get_design_summary(
        kind: Literal["steel", "concrete"],
        limit: Annotated[int, Field(ge=0, le=5000)] = 50,
        only_failing: Annotated[bool, Field(description="Steel: only members with ratio > 1 or errors.")] = False,
    ) -> dict[str, Any]:
        """Design results per member: steel demand/capacity ratios with governing combo, or concrete required
        reinforcement for beams and columns. Worst members first."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            return design_summary(session, kind, limit, only_failing=only_failing)

        return await session.run(work)


STEEL_RATIO_TYPES = {1: "PMM", 2: "Major shear", 3: "Minor shear", 4: "Major beam-column capacity", 5: "Minor beam-column capacity", 6: "Other"}


def design_summary(session: EtabsSession, kind: str, limit: int, *, only_failing: bool = False) -> dict[str, Any]:
    if kind == "steel":
        try:
            o = session.out("DesignSteel.GetSummaryResults", Name="All", ItemType="Group")
        except ITEM_ERRORS as exc:
            return {"members": [], "warning": f"No steel design results: {exc.message}"}
        rows = [
            {
                "frame": f, "ratio": r, "ratio_type": STEEL_RATIO_TYPES.get(t, t), "location": loc,
                "combo": c, "error": e or None, "warning": w or None,
            }
            for f, r, t, loc, c, e, w in zip(
                o.get("FrameName") or [], o.get("Ratio") or [], o.get("RatioType") or [], o.get("Location") or [],
                o.get("ComboName") or [], o.get("ErrorSummary") or [], o.get("WarningSummary") or [],
            )
        ]
        failing = [r for r in rows if (r["ratio"] or 0) > 1.0 or r["error"]]
        if only_failing:
            rows = failing
        rows.sort(key=lambda r: -(r["ratio"] or 0))
        return {
            "members_designed": len(o.get("FrameName") or []),
            "failing": len(failing),
            "max_ratio": rows[0]["ratio"] if rows else None,
            "members": rows[:limit],
        }

    out: dict[str, Any] = {}
    try:
        b = session.out("DesignConcrete.GetSummaryResultsBeam", Name="All", ItemType="Group")
        beams = [
            {"frame": f, "location": loc, "top_area": ta, "top_combo": tc, "bottom_area": ba, "bottom_combo": bc,
             "shear_area": va, "shear_combo": vc, "error": e or None, "warning": w or None}
            for f, loc, tc, ta, bc, ba, vc, va, e, w in zip(
                b.get("FrameName") or [], b.get("Location") or [], b.get("TopCombo") or [], b.get("TopArea") or [],
                b.get("BotCombo") or [], b.get("BotArea") or [], b.get("VMajorCombo") or [], b.get("VMajorArea") or [],
                b.get("ErrorSummary") or [], b.get("WarningSummary") or [],
            )
        ]
        beams.sort(key=lambda r: -max(r["top_area"] or 0, r["bottom_area"] or 0))
        out["beams"] = {"stations": len(beams), "with_errors": sum(1 for r in beams if r["error"]), "rows": beams[:limit]}
    except ITEM_ERRORS as exc:
        out["beams"] = {"warning": exc.message}
    try:
        c = session.out("DesignConcrete.GetSummaryResultsColumn", Name="All", ItemType="Group")
        cols = [
            {"frame": f, "location": loc, "option": "check" if opt == 2 else "design", "pmm_area": pa,
             "pmm_ratio": pr, "pmm_combo": pc, "error": e or None, "warning": w or None}
            for f, opt, loc, pc, pa, pr, e, w in zip(
                c.get("FrameName") or [], c.get("MyOption") or [], c.get("Location") or [], c.get("PMMCombo") or [],
                c.get("PMMArea") or [], c.get("PMMRatio") or [], c.get("ErrorSummary") or [],
                c.get("WarningSummary") or [],
            )
        ]
        cols.sort(key=lambda r: -(r["pmm_ratio"] or 0))
        out["columns"] = {"stations": len(cols), "with_errors": sum(1 for r in cols if r["error"]), "rows": cols[:limit]}
    except ITEM_ERRORS as exc:
        out["columns"] = {"warning": exc.message}
    return out
