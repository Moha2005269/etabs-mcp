"""Lateral load tools: ASCE 7-16 seismic and wind, response spectrum, mass source, diaphragms, modal setup.

Where the ETABS API has a method (auto seismic, spectrum cases, mass source, diaphragms) it is used; where it
has none (auto wind, spectrum functions) the definition is written through ETABS's own importable database
tables, which ETABS validates and reports on like any interactive edit.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from ..errors import ITEM_ERRORS, ArgumentValidationError, EtabsCallFailed
from ..session import EtabsSession
from ..units import length_unit
from .common import WRITE, batch_item_error, names_list
from .model import stories as story_table

SITE_CLASSES = ["A", "B", "C", "D", "E", "F"]
# Order of SetASCE716_1's nDir array, determined against ETABS 23 (it interleaves X and Y; it is not the
# order of the table columns).
SEISMIC_DIRS = ["X", "Y", "X+E", "Y+E", "X-E", "Y-E"]
G = {"m": 9.80665, "cm": 980.665, "mm": 9806.65, "ft": 32.174, "in": 386.089}


class MassLoad(BaseModel):
    pattern: str
    multiplier: float = 1.0


def _apply_table_rows(session: EtabsSession, key: str, rows_by_name: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Upsert rows (matched on 'Name') into an importable table and apply; raises on fatal import errors."""
    o = session.out("DatabaseTables.GetTableForEditingArray", TableKey=key, GroupName="")
    fields = list(o.get("FieldsKeysIncluded") or [])
    flat = list(o.get("TableData") or [])
    n = len(fields)
    rows = [dict(zip(fields, flat[i:i + n])) for i in range(0, len(flat), n)] if n else []
    rows = [r for r in rows if r.get("Name") not in rows_by_name]
    for name, values in rows_by_name.items():
        unknown = [k for k in values if k not in fields]
        if unknown:
            raise ArgumentValidationError(f"Unknown field(s) {unknown} for table '{key}'.", hint=f"Fields: {fields}")
        rows.append({"Name": name, **{k: str(v) for k, v in values.items()}})
    data = [str(r.get(f, "")) for r in rows for f in fields]
    session.call("DatabaseTables.SetTableForEditingArray",
                 {"TableKey": key, "TableVersion": o.get("TableVersion", 0), "FieldsKeysIncluded": fields,
                  "NumberRecords": len(rows), "TableData": data}, _internal=True)
    log = session.out("DatabaseTables.ApplyEditedTables", FillImportLog=True)
    fatal, errs = int(log.get("NumFatalErrors") or 0), int(log.get("NumErrorMsgs") or 0)
    if fatal or errs:
        raise EtabsCallFailed(f"ETABS rejected the '{key}' import ({fatal} fatal, {errs} errors).",
                              path="DatabaseTables.ApplyEditedTables",
                              hint=(log.get("ImportLog") or "")[-1500:])
    return {"table": key, "warnings": int(log.get("NumWarnMsgs") or 0)}


def _top_bottom(session: EtabsSession, top: str | None, bottom: str | None) -> tuple[str, str]:
    st = story_table(session)["stories"]
    if not st:
        raise ArgumentValidationError("The model has no stories.")
    return top or st[0]["name"], bottom or "Base"


def _ensure_pattern(session: EtabsSession, name: str, kind: str) -> None:
    if name not in names_list(session, "LoadPatterns.GetNameList"):
        session.call("LoadPatterns.Add", {"Name": name, "MyType": kind, "SelfWTMultiplier": 0, "AddAnalysisCase": True},
                     _internal=True)


def register(mcp: FastMCP, session: EtabsSession) -> None:
    @mcp.tool(annotations=WRITE)
    async def etabs_define_seismic_load(
        name: str = "EQX",
        directions: Annotated[list[Literal["X", "X+E", "X-E", "Y", "Y+E", "Y-E"]],
                              Field(min_length=1, description="Directions and accidental eccentricity sense.")] = ["X"],  # noqa: B006
        r: Annotated[float, Field(gt=0, description="Response modification coefficient R.")] = 8.0,
        omega: Annotated[float, Field(gt=0, description="Overstrength factor Ω0.")] = 3.0,
        cd: Annotated[float, Field(gt=0, description="Deflection amplification Cd.")] = 5.5,
        importance: float = 1.0,
        ss: Annotated[float, Field(ge=0, description="Mapped MCER short-period acceleration Ss (g).")] = 1.5,
        s1: Annotated[float, Field(ge=0, description="Mapped MCER 1-s acceleration S1 (g).")] = 0.6,
        tl: Annotated[float, Field(gt=0, description="Long-period transition TL (s).")] = 8.0,
        site_class: Literal["A", "B", "C", "D", "E", "F"] = "D",
        eccentricity: float = 0.05,
        period: Annotated[Literal["approximate", "program", "user"], Field(description="Fundamental period source.")] = "program",
        user_period: float = 0.0,
        ct_type: Annotated[int, Field(ge=1, le=4, description="Approximate-period Ct/x set (ETABS numbering, 2 = 0.03;0.75 'all other').")] = 2,
        top_story: Annotated[str | None, Field(description="Default: the top story.")] = None,
        bottom_story: Annotated[str | None, Field(description="Default: Base.")] = None,
    ) -> dict[str, Any]:
        """Define an ASCE 7-16 equivalent-lateral-force seismic load pattern (Define > Load Patterns > Auto
        Lateral Load). Creates the Quake pattern and its load case."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            _ensure_pattern(session, name, "Quake")
            session.call("LoadPatterns.AutoSeismic.SetASCE716_1", {
                "Name": name, "nDir": [d in directions for d in SEISMIC_DIRS], "Eccen": eccentricity,
                "PeriodFlag": {"approximate": 1, "program": 2, "user": 3}[period], "CtType": ct_type,
                "UserT": user_period, "UserZ": False, "TopZ": 0, "BottomZ": 0, "R": r, "Omega": omega, "Cd": cd,
                "I": importance, "Ss": ss, "S1": s1, "TL": tl, "SiteClass": SITE_CLASSES.index(site_class) + 1,
                "Fa": 1.0, "Fv": 1.0}, _internal=True)
            # The API leaves the story range at Base-Base; set it through the validated table.
            top, bottom = _top_bottom(session, top_story, bottom_story)
            key = "Load Pattern Definitions - Auto Seismic - ASCE 7-16"
            o = session.out("DatabaseTables.GetTableForEditingArray", TableKey=key, GroupName="")
            fields, flat = list(o["FieldsKeysIncluded"]), list(o["TableData"])
            rows = [dict(zip(fields, flat[i:i + len(fields)])) for i in range(0, len(flat), len(fields))]
            row = next((rw for rw in rows if rw.get("Name") == name), None)
            if row is None:
                raise EtabsCallFailed(f"Seismic pattern '{name}' did not appear in '{key}'.", path=key)
            row.update({"TopStory": top, "BotStory": bottom})
            _apply_table_rows(session, key, {name: {k: v for k, v in row.items() if k != "Name"}})
            return {"pattern": name, "code": "ASCE 7-16", "directions": directions, "stories": [bottom, top],
                    "parameters": {"R": r, "Omega": omega, "Cd": cd, "I": importance, "Ss": ss, "S1": s1, "TL": tl,
                                   "SiteClass": site_class, "period": period}}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_define_wind_load(
        name: str = "WX",
        wind_speed: Annotated[float, Field(gt=0, description="Basic wind speed V (mph for ASCE 7).")] = 115.0,
        exposure: Literal["B", "C", "D"] = "C",
        angle: Annotated[float, Field(description="Wind direction angle in degrees (0 = +X, 90 = +Y).")] = 0.0,
        kzt: float = 1.0,
        gust_factor: float = 0.85,
        kd: float = 0.85,
        windward_cp: float = 0.8,
        leeward_cp: float = 0.5,
        cases: Annotated[Literal["Create All", "Case 1", "Case 2", "Case 3", "Case 4"], Field(description="ASCE 7 wind load cases.")] = "Create All",
        top_story: str | None = None,
        bottom_story: str | None = None,
        code: Annotated[str, Field(description="Auto-wind table code suffix, e.g. 'ASCE 7-16', 'ASCE 7-22', 'Eurocode1 2005'. Other codes need 'fields'.")] = "ASCE 7-16",
        fields: Annotated[dict[str, Any] | None, Field(description="Extra/override table fields for the chosen code (see etabs_get_table on 'Load Pattern Definitions - Auto Wind - <code>').")] = None,
    ) -> dict[str, Any]:
        """Define an automatic wind load pattern applied to diaphragms (Define > Load Patterns > Auto Lateral
        Load > Wind). ASCE 7-16 is fully parameterised; other codes take their table fields via 'fields'."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            _ensure_pattern(session, name, "Wind")
            top, bottom = _top_bottom(session, top_story, bottom_story)
            row: dict[str, Any] = {"IsAuto": "No", "Exposure": "Diaphragms", "TopStory": top, "BotStory": bottom,
                                   "Parapet": "No", "Angle": angle}
            if code.startswith("ASCE 7-1") or code.startswith("ASCE 7-2"):
                row.update({"UserCp": "No", "ASCECase": cases, "e1": 0.15, "e2": 0.15, "WindSpeed": wind_speed,
                            "ExpType": exposure, "GrdElevFact": 1, "kzt": kzt, "GustFact": gust_factor, "Kd": kd})
            else:
                row.update({"CPWindward": windward_cp, "CPLeeward": leeward_cp, "WindSpeed": wind_speed})
            row.update(fields or {})
            res = _apply_table_rows(session, f"Load Pattern Definitions - Auto Wind - {code}", {name: row})
            return {"pattern": name, "code": code, "stories": [bottom, top], **res}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_define_response_spectrum(
        case_name: str = "RSX",
        function_name: str = "ASCE7-16-RS",
        direction: Literal["U1", "U2"] = "U1",
        ss: float = 1.5,
        s1: float = 0.6,
        tl: float = 8.0,
        site_class: Literal["A", "B", "C", "D", "E", "F"] = "D",
        damping: float = 0.05,
        scale_factor: Annotated[float | None, Field(description="Default g·I/R in present units (from importance and r).")] = None,
        importance: float = 1.0,
        r: float = 8.0,
        modal_case: str = "Modal",
        eccentricity: float = 0.05,
    ) -> dict[str, Any]:
        """Define an ASCE 7-16 response spectrum function and a response-spectrum load case that uses it."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            _apply_table_rows(session, "Functions - Response Spectrum - ASCE7-16",
                              {function_name: {"Ss": ss, "S1": s1, "TL": tl, "SiteClass": site_class, "DampRatio": damping}})
            g = G[length_unit(session.value("GetPresentUnits"))]
            sf = scale_factor if scale_factor is not None else g * importance / r
            session.call("LoadCases.ResponseSpectrum.SetCase", {"Name": case_name}, _internal=True)
            session.call("LoadCases.ResponseSpectrum.SetLoads",
                         {"Name": case_name, "NumberLoads": 1, "LoadName": [direction], "Func": [function_name],
                          "SF": [sf], "CSys": ["Global"], "Ang": [0.0]}, _internal=True)
            session.call("LoadCases.ResponseSpectrum.SetModalCase", {"Name": case_name, "ModalCase": modal_case}, _internal=True)
            session.call("LoadCases.ResponseSpectrum.SetEccentricity", {"Name": case_name, "Eccen": eccentricity}, _internal=True)
            return {"case": case_name, "function": function_name, "direction": direction, "scale_factor": sf,
                    "modal_case": modal_case}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_set_mass_source(
        include_self_weight: bool = True,
        include_added_mass: bool = True,
        loads: Annotated[list[MassLoad] | None, Field(description="Load patterns that contribute mass, e.g. [{'pattern':'SDL'}, {'pattern':'LL','multiplier':0.25}].")] = None,
    ) -> dict[str, Any]:
        """Set the default mass source (Define > Mass Source): element self mass, added mass and load patterns."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            pats = [m.pattern for m in loads or []]
            session.call("PropMaterial.SetMassSource_1",
                         {"IncludeElements": include_self_weight, "IncludeAddedMass": include_added_mass,
                          "IncludeLoads": bool(pats), "NumberLoads": len(pats), "LoadPat": pats,
                          "sf": [m.multiplier for m in loads or []]}, _internal=True)
            return {"self_weight": include_self_weight, "added_mass": include_added_mass,
                    "loads": [m.model_dump() for m in loads or []]}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_assign_diaphragm(
        name: str = "D1",
        semi_rigid: bool = False,
        stories: Annotated[list[str] | None, Field(description="Stories to assign (default: all with slabs).")] = None,
    ) -> dict[str, Any]:
        """Define a rigid or semi-rigid diaphragm and assign it to the slabs (area objects) on the chosen stories."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            # New models already contain 'D1', and ETABS rejects SetDiaphragm on an existing name; reuse it and
            # only redefine when the rigidity differs.
            note = None
            if name in names_list(session, "Diaphragm.GetNameList"):
                current = session.out("Diaphragm.GetDiaphragm", Name=name).get("SemiRigid")
                if bool(current) != semi_rigid:
                    try:
                        session.call("Diaphragm.SetDiaphragm", {"Name": name, "SemiRigid": semi_rigid}, _internal=True)
                    except ITEM_ERRORS:
                        note = (f"'{name}' already exists as {'semi-rigid' if current else 'rigid'} and ETABS would not "
                                "redefine it; pass another name to create a new diaphragm.")
            else:
                session.call("Diaphragm.SetDiaphragm", {"Name": name, "SemiRigid": semi_rigid}, _internal=True)
            all_stories = stories or names_list(session, "Story.GetNameList")
            done, errors = 0, []
            for st in all_stories:
                areas = list(session.out("AreaObj.GetNameListOnStory", StoryName=st).get("MyName") or [])
                for i, a in enumerate(areas):
                    try:
                        session.call("AreaObj.SetDiaphragm", {"Name": a, "DiaphragmName": name}, _internal=True)
                        done += 1
                    except ITEM_ERRORS as exc:
                        errors.append(batch_item_error(i, exc, a))
            out = {"diaphragm": name, "semi_rigid": semi_rigid, "areas_assigned": done, "errors": errors}
            if note:
                out["note"] = note
            return out

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_set_modal_case(
        case: str = "Modal",
        max_modes: Annotated[int, Field(ge=1, le=1000)] = 12,
        min_modes: Annotated[int, Field(ge=1, le=1000)] = 1,
    ) -> dict[str, Any]:
        """Set how many modes the eigen modal case computes (raise it until mass participation is enough)."""

        def work() -> dict[str, Any]:
            session.call("LoadCases.ModalEigen.SetNumberModes",
                         {"Name": case, "MaxModes": max_modes, "MinModes": min(min_modes, max_modes)}, _internal=True)
            return {"case": case, "max_modes": max_modes, "min_modes": min(min_modes, max_modes)}

        return await session.run(work)
