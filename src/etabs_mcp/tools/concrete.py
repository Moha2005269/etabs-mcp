"""Concrete, shear-wall and slab design tools, design-code selection, and access to ETABS menu commands that
have no API (driven through Windows UI Automation)."""

from __future__ import annotations

import time
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .. import gui
from ..discovery import main_window_for_pid
from ..errors import ITEM_ERRORS, ArgumentValidationError, ConfirmationRequired, EtabsCallFailed, NotConnectedError
from ..session import EtabsSession
from .common import DESTRUCTIVE, READ, WRITE, names_list
from .tables import read_table, select_cases_for_display

CODE_TABLES = {
    "steel": "Steel Frame Design Preferences - ",
    "concrete": "Concrete Frame Design Preferences - ",
    "shear_wall": "Shear Wall Design Preferences - ",
    "concrete_slab": "Concrete Slab Design Preferences - ",
    "composite_beam": "Composite Beam Design Preferences - ",
}
CODE_PATHS = {"steel": "DesignSteel", "concrete": "DesignConcrete", "concrete_shell": "DesignConcreteShell"}
SHEAR_WALL_MENU = ["Design", "Shear Wall Design", "Start Design/Check"]


def _main_hwnd(session: EtabsSession) -> tuple[int, int]:
    pid = getattr(session.backend, "pid", None)
    proc = main_window_for_pid(pid) if pid else None
    if proc is None or proc.hwnd is None:
        raise NotConnectedError("The ETABS window was not found.",
                                hint="Menu commands need a visible ETABS window: etabs_show_window(visible=true).")
    return pid, proc.hwnd


def _rows(o: dict[str, Any], keys: list[str]) -> list[dict[str, Any]]:
    cols = [o.get(k) or [] for k in keys]
    n = min((len(c) for c in cols), default=0)
    return [{k: cols[j][i] for j, k in enumerate(keys)} for i in range(n)]


def pier_results(session: EtabsSession) -> list[dict[str, Any]]:
    try:
        o = session.out("DesignShearWall.GetPierSummaryResults")
    except EtabsCallFailed:
        return []
    keys = ["Story", "PierLabel", "Station", "DesignType", "PierSecType", "EdgeBar", "EndBar", "BarSpacing",
            "ReinfPercent", "CurrPercent", "DCRatio", "ShearAv", "BZoneL", "BZoneR", "WarnMsg", "ErrMsg"]
    return _rows(o, keys)


def spandrel_results(session: EtabsSession) -> list[dict[str, Any]]:
    try:
        o = session.out("DesignShearWall.GetSpandrelSummaryResults")
    except EtabsCallFailed:
        return []
    keys = ["Story", "Spandrel", "Station", "TopRebar", "TopRebarCombo", "BotRebar", "BotRebarCombo", "AVert",
            "AHorz", "ShearCombo", "Vu", "ADiag", "WarnMsg", "ErrMsg"]
    return _rows(o, keys)


def _summarise(rows: list[dict[str, Any]], ratio_key: str | None, limit: int) -> dict[str, Any]:
    errs = [r for r in rows if (r.get("ErrMsg") or r.get("error")) not in (None, "", "No Message")]
    if ratio_key:
        rows = sorted(rows, key=lambda r: -(r.get(ratio_key) or 0))
    return {"count": len(rows), "with_errors": len(errs), "rows": rows[:limit]}


def register(mcp: FastMCP, session: EtabsSession) -> None:
    @mcp.tool(annotations=WRITE)
    async def etabs_design_codes(
        action: Literal["get", "set", "list"] = "get",
        kind: Annotated[Literal["steel", "concrete", "concrete_shell"] | None, Field(description="For 'set'.")] = None,
        code: Annotated[str | None, Field(description="For 'set': e.g. 'AISC 360-16', 'ACI 318-19', 'Eurocode 2-2004'. 'list' shows valid names.")] = None,
    ) -> dict[str, Any]:
        """Read, list or change the design codes (Design > ... > View/Revise Preferences). 'list' returns every code
        this ETABS version supports for steel, concrete, shear wall, slab and composite design."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            if action == "set":
                if not kind or not code:
                    raise ArgumentValidationError("action='set' needs kind and code.")
                session.call(f"{CODE_PATHS[kind]}.SetCode", {"CodeName": code}, _internal=True)
            current = {}
            for k, iface in CODE_PATHS.items():
                try:
                    current[k] = session.out(f"{iface}.GetCode").get("CodeName")
                except ITEM_ERRORS:
                    current[k] = None
            out: dict[str, Any] = {"current": current}
            if action == "list":
                keys = session.out("DatabaseTables.GetAllTables").get("TableKey") or []
                out["available"] = {k: sorted(t[len(prefix):] for t in keys if t.startswith(prefix))
                                    for k, prefix in CODE_TABLES.items()}
            return out

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_run_shear_wall_design(
        limit: Annotated[int, Field(ge=1, le=5000, description="Pier/spandrel rows to return, worst first.")] = 50,
    ) -> dict[str, Any]:
        """Run shear wall design (Design > Shear Wall Design > Start Design/Check) and return pier and spandrel
        results. The ETABS API has no call for this, so the server invokes the menu command through Windows UI
        Automation; ETABS must be visible. Walls must carry pier/spandrel labels (etabs_assign_label)."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            if not session.value("GetModelIsLocked"):
                raise ArgumentValidationError("Shear wall design needs analysis results.", hint="Run etabs_run_analysis first.")
            if not names_list(session, "PierLabel.GetNameList") and not names_list(session, "SpandrelLabel.GetNameList"):
                raise ArgumentValidationError("No pier or spandrel labels: nothing to design.",
                                              hint="Label wall areas with etabs_assign_label(label_type='pier', ...).")
            pid, hwnd = _main_hwnd(session)
            t0 = time.monotonic()
            gui.invoke_menu(hwnd, SHEAR_WALL_MENU)
            piers = pier_results(session)
            for _ in range(40):
                if piers:
                    break
                time.sleep(0.5)
                piers = pier_results(session)
            open_dialogs = gui.dialogs(pid, hwnd)
            out = {"seconds": round(time.monotonic() - t0, 1),
                   "piers": _summarise(piers, "DCRatio", limit),
                   "spandrels": _summarise(spandrel_results(session), "Vu", limit)}
            if open_dialogs:
                out["open_dialogs"] = [d["title"] for d in open_dialogs]
                out["hint"] = "ETABS opened a dialog; inspect it with etabs_screenshot and close it with etabs_dialogs."
            if not piers:
                out["warning"] = "No pier results were produced. Check the labels, design combinations and preferences."
            return out

        return await session.run(work)

    @mcp.tool(annotations=READ)
    async def etabs_get_wall_design(
        kind: Literal["pier", "spandrel"] = "pier",
        story: str | None = None,
        label: str | None = None,
        limit: Annotated[int, Field(ge=1, le=5000)] = 100,
    ) -> dict[str, Any]:
        """Shear wall design results: piers (reinforcing ratio, D/C, boundary zones, messages) or spandrels
        (flexural and shear reinforcement), worst first. Run etabs_run_shear_wall_design first."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            rows = pier_results(session) if kind == "pier" else spandrel_results(session)
            key = "PierLabel" if kind == "pier" else "Spandrel"
            rows = [r for r in rows if (story is None or r["Story"] == story) and (label is None or r[key] == label)]
            if not rows:
                return {"count": 0, "rows": [],
                        "hint": "No results: run etabs_run_shear_wall_design (needs analysis and pier/spandrel labels)."}
            return {"units": session.value("GetPresentUnits"),
                    **_summarise(rows, "DCRatio" if kind == "pier" else "Vu", limit)}

        return await session.run(work)

    @mcp.tool(annotations=READ)
    async def etabs_get_concrete_design(
        element: Literal["beam", "column", "joint"] = "beam",
        names: Annotated[list[str] | None, Field(description="Only these frames.")] = None,
        envelope: Annotated[bool, Field(description="One row per frame (governing station) instead of every station.")] = True,
        limit: Annotated[int, Field(ge=1, le=20000)] = 100,
    ) -> dict[str, Any]:
        """Concrete frame design results (after etabs_run_design(kind='concrete')): required longitudinal and shear
        reinforcement for beams, PMM ratios and areas for columns, joint shear and beam/column capacity ratios.
        Areas are in present units."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            base = {"Name": "All", "ItemType": "Group"}
            if element == "beam":
                o = session.out("DesignConcrete.GetSummaryResultsBeam", **base)
                keys = ["FrameName", "Location", "TopCombo", "TopArea", "BotCombo", "BotArea", "VMajorCombo",
                        "VMajorArea", "TLCombo", "TLArea", "TTCombo", "TTArea", "ErrorSummary", "WarningSummary"]
                score = lambda r: max(r["TopArea"] or 0, r["BotArea"] or 0)  # noqa: E731
            elif element == "column":
                o = session.out("DesignConcrete.GetSummaryResultsColumn", **base)
                keys = ["FrameName", "MyOption", "Location", "PMMCombo", "PMMArea", "PMMRatio", "VMajorCombo",
                        "AVMajor", "VMinorCombo", "AVMinor", "ErrorSummary", "WarningSummary"]
                score = lambda r: r["PMMRatio"] or 0  # noqa: E731
            else:
                o = session.out("DesignConcrete.GetSummaryResultsJoint", **base)
                keys = ["FrameName", "LCJSRatioMajor", "JSRatioMajor", "LCJSRatioMinor", "JSRatioMinor",
                        "LCBCCRatioMajor", "BCCRatioMajor", "LCBCCRatioMinor", "BCCRatioMinor", "ErrorSummary",
                        "WarningSummary"]
                score = lambda r: max(r["JSRatioMajor"] or 0, r["JSRatioMinor"] or 0, r["BCCRatioMajor"] or 0,  # noqa: E731
                                      r["BCCRatioMinor"] or 0)
            rows = _rows(o, keys)
            if not rows and element == "joint":
                return {"element": element, "count": 0, "rows": [],
                        "note": "No joint checks: ETABS checks beam-column joints only in special (and intermediate) "
                                "moment frames (ACI 318 Ch. 18). Set the framing type to check them."}
            if not rows:
                raise ArgumentValidationError("No concrete design results.", hint="Run etabs_run_design(kind='concrete').")
            if names:
                rows = [r for r in rows if r["FrameName"] in set(names)]
            if envelope:
                best: dict[str, dict] = {}
                for r in rows:
                    if r["FrameName"] not in best or score(r) > score(best[r["FrameName"]]):
                        best[r["FrameName"]] = r
                rows = list(best.values())
            rows.sort(key=lambda r: -score(r))
            errors = [r for r in rows if r.get("ErrorSummary")]
            return {"element": element, "units": session.value("GetPresentUnits"), "count": len(rows),
                    "with_errors": len(errors), "rows": rows[:limit]}

        return await session.run(work)

    @mcp.tool(annotations=READ)
    async def etabs_get_slab_design(
        part: Literal["strips", "punching"] = "punching",
        limit: Annotated[int, Field(ge=1, le=20000)] = 100,
    ) -> dict[str, Any]:
        """Concrete slab design results (after etabs_run_design(kind='concrete_slab')): design-strip flexure and
        shear reinforcement, or punching shear checks at columns, worst first."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            if part == "strips":
                o = session.out("DesignConcreteSlab.GetSummaryResultsFlexureAndShear")
                rows = _rows(o, ["StoryName", "DesignStripName", "SpanID", "Location", "FTopCombo", "FTopMoment",
                                 "FTopArea", "FBotCombo", "FBotMoment", "FBotArea", "VCombo", "VForce", "VArea",
                                 "Status", "Layer"])
                rows.sort(key=lambda r: -max(r["FTopArea"] or 0, r["FBotArea"] or 0))
                return {"part": part, "count": len(rows), "rows": rows[:limit],
                        **({} if rows else {"hint": "No strip results: slabs need design strips and a slab design run."})}
            select_cases_for_display(session, None, None)
            try:
                res = read_table(session, "Concrete Slab Design - Punching Shear Data", limit=10 ** 6)
            except EtabsCallFailed:
                return {"part": part, "count": 0, "rows": [],
                        "hint": "No punching results: run etabs_run_design(kind='concrete_slab') first."}
            ratio_key = next((f for f in res["fields"] if "ratio" in f.lower()), None)
            rows = res["rows"]
            if ratio_key:
                rows.sort(key=lambda r: -(r.get(ratio_key) if isinstance(r.get(ratio_key), float) else 0))
            return {"part": part, "ratio_field": ratio_key, "count": len(rows), "fields": res["fields"],
                    "units": res["units"], "rows": rows[:limit],
                    "over_capacity": sum(1 for r in rows if ratio_key and isinstance(r.get(ratio_key), float)
                                         and r[ratio_key] > 1.0)}

        return await session.run(work)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def etabs_menu(
        action: Literal["list", "invoke"] = "list",
        path: Annotated[list[str] | None, Field(description="Menu path, e.g. ['Design', 'Shear Wall Design'] to list, or ['Analyze', 'Check Model...'] to invoke.")] = None,
        confirm: Annotated[bool, Field(description="Required for 'invoke': menu commands can change or delete model data. Ask the user first.")] = False,
    ) -> dict[str, Any]:
        """Use ETABS menu commands that have no API, exactly as a person would click them (through Windows UI
        Automation, no mouse). 'list' browses the menus; 'invoke' runs a command. Commands that open a dialog
        leave it open: check with etabs_screenshot and close it with etabs_dialogs."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            pid, hwnd = _main_hwnd(session)
            if action == "list":
                return {"path": path or [], "items": gui.list_menu(hwnd, path)}
            if not path:
                raise ArgumentValidationError("action='invoke' needs a full menu path.")
            if not (confirm or session.settings.allow_destructive):
                raise ConfirmationRequired(f"Invoking {' > '.join(path)} acts on the model through the GUI.",
                                           hint="Confirm with the user, then repeat with confirm=true.")
            if session.settings.read_only:
                raise ArgumentValidationError("Menu commands are disabled in read-only mode.")
            gui.invoke_menu(hwnd, path)
            time.sleep(0.5)
            return {"invoked": path, "open_dialogs": [d["title"] for d in gui.dialogs(pid, hwnd)]}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_dialogs(
        action: Literal["list", "close"] = "list",
        title: Annotated[str | None, Field(description="Close the dialog with this title ('close' with no title closes all).")] = None,
    ) -> dict[str, Any]:
        """List or close dialogs ETABS has open. A modal dialog blocks the ETABS API, so close leftovers after
        etabs_menu or a design run."""

        def work() -> dict[str, Any]:
            pid, hwnd = _main_hwnd(session)
            found = gui.dialogs(pid, hwnd)
            if action == "close":
                targets = [d for d in found if title is None or d["title"] == title]
                for d in targets:
                    gui.close_dialog(d["hwnd"])
                time.sleep(0.5)
                return {"closed": [d["title"] for d in targets],
                        "still_open": [d["title"] for d in gui.dialogs(pid, hwnd)]}
            return {"dialogs": [d["title"] for d in found]}

        # Dialog handling must work even while ETABS is busy, so it does not go through the worker queue.
        import asyncio

        return await asyncio.to_thread(work)
