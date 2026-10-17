"""Load tools: patterns, assignments to objects, combinations, and a summary of load definitions."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from ..errors import ITEM_ERRORS, ArgumentValidationError
from ..session import EtabsSession
from .common import READ, WRITE, batch_item_error, names_list, suspended_tree, targets

# ETABS load direction codes for frame and area loads.
DIRECTIONS = {
    "local_1": 1, "local_2": 2, "local_3": 3,
    "x": 4, "y": 5, "z": 6,
    "x_projected": 7, "y_projected": 8, "z_projected": 9,
    "gravity": 10, "gravity_projected": 11,
}
Direction = Literal[
    "local_1", "local_2", "local_3", "x", "y", "z",
    "x_projected", "y_projected", "z_projected", "gravity", "gravity_projected",
]
COMBO_TYPES = {"linear_add": 0, "envelope": 1, "absolute_add": 2, "srss": 3, "range_add": 4}


class PatternInput(BaseModel):
    name: str
    type: str = Field("Dead", description="eLoadPatternType name: Dead, SuperDead, Live, ReduceLive, Rooflive, Quake, Wind, Snow, Notional, Other ...")
    self_weight_multiplier: float = 0.0
    add_analysis_case: bool = Field(True, description="Also create the matching linear static load case.")


class LoadInput(BaseModel):
    kind: Literal["frame_distributed", "frame_point", "area_uniform", "point_force"]
    pattern: str = Field(description="Load pattern name.")
    names: list[str] | None = Field(None, description="Object names.")
    group: str | None = None
    selected: bool = False
    direction: Direction = Field("gravity", description="Load direction; 'gravity' is global -Z.")
    value: float | None = Field(None, description="frame_point/area_uniform: load value. frame_distributed: uniform value (sets start and end).")
    start_value: float | None = Field(None, description="frame_distributed: value at start_distance (trapezoidal loads).")
    end_value: float | None = None
    start_distance: float = Field(0.0, description="frame loads: start position (relative 0-1 unless relative_distance=false).")
    end_distance: float = 1.0
    relative_distance: bool = True
    moment: bool = Field(False, description="frame loads: apply a moment instead of a force.")
    forces: list[float] | None = Field(None, min_length=6, max_length=6, description="point_force: [FX, FY, FZ, MX, MY, MZ].")
    replace: bool = Field(True, description="Replace existing loads of this pattern on the objects (False adds to them).")
    csys: str = "Global"


class ComboItem(BaseModel):
    name: str = Field(description="Load case or combination name.")
    scale_factor: float = 1.0


class ComboInput(BaseModel):
    name: str
    type: Literal["linear_add", "envelope", "absolute_add", "srss", "range_add"] = "linear_add"
    items: list[ComboItem] = Field(min_length=1)


def register(mcp: FastMCP, session: EtabsSession) -> None:
    @mcp.tool(annotations=WRITE)
    async def etabs_define_load_patterns(patterns: Annotated[list[PatternInput], Field(min_length=1)]) -> dict[str, Any]:
        """Define load patterns (Define > Load Patterns), each optionally with its linear static load case."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            ok, errors = [], []
            for i, p in enumerate(patterns):
                try:
                    session.call(
                        "LoadPatterns.Add",
                        {"Name": p.name, "MyType": p.type, "SelfWTMultiplier": p.self_weight_multiplier,
                         "AddAnalysisCase": p.add_analysis_case},
                        _internal=True,
                    )
                    ok.append(p.name)
                except ITEM_ERRORS as exc:
                    errors.append(batch_item_error(i, exc, p.model_dump()))
            return {"defined": ok, "errors": errors}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_assign_loads(loads: Annotated[list[LoadInput], Field(min_length=1, max_length=5000)]) -> dict[str, Any]:
        """Assign loads (Assign > Frame/Shell/Joint Loads): distributed or point loads on frames, uniform loads on
        areas, and forces/moments on joints. Values are in present units; 'gravity' acts downward (-Z)."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            ok, errors = 0, []
            with suspended_tree(session, len(loads)):
                for i, ld in enumerate(loads):
                    try:
                        for name, item in targets(ld.names, ld.group, ld.selected):
                            _assign_one(session, ld, name, item)
                        ok += 1
                    except ITEM_ERRORS as exc:
                        errors.append(batch_item_error(i, exc, ld.model_dump(exclude_defaults=True)))
            return {"assigned": ok, "errors": errors}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_define_combinations(
        combinations: Annotated[list[ComboInput], Field(description="Combinations to create.")] = [],  # noqa: B006
        add_default_design_combos: Annotated[
            list[Literal["steel", "concrete", "aluminum", "cold_formed"]],
            Field(description="Also generate the code default design combinations for these materials."),
        ] = [],  # noqa: B006
    ) -> dict[str, Any]:
        """Define load combinations (Define > Load Combinations) from load cases and other combinations, and/or
        generate the default design combinations for the active design codes."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            if not combinations and not add_default_design_combos:
                raise ArgumentValidationError("Pass combinations and/or add_default_design_combos.")
            existing_combos = set(names_list(session, "RespCombo.GetNameList"))
            ok, errors = [], []
            for i, c in enumerate(combinations):
                try:
                    session.call("RespCombo.Add", {"Name": c.name, "ComboType": COMBO_TYPES[c.type]}, _internal=True)
                    for it in c.items:
                        kind = "LoadCombo" if it.name in existing_combos else "LoadCase"
                        session.call(
                            "RespCombo.SetCaseList",
                            {"Name": c.name, "CNameType": kind, "CName": it.name, "SF": it.scale_factor},
                            _internal=True,
                        )
                    existing_combos.add(c.name)
                    ok.append(c.name)
                except ITEM_ERRORS as exc:
                    errors.append(batch_item_error(i, exc, c.model_dump()))
            if add_default_design_combos:
                sel = set(add_default_design_combos)
                session.call(
                    "RespCombo.AddDesignDefaultCombos",
                    {"DesignSteel": "steel" in sel, "DesignConcrete": "concrete" in sel,
                     "DesignAluminum": "aluminum" in sel, "DesignColdFormed": "cold_formed" in sel},
                    _internal=True,
                )
            return {"defined": ok, "errors": errors, "all_combinations": names_list(session, "RespCombo.GetNameList")}

        return await session.run(work)

    @mcp.tool(annotations=READ)
    async def etabs_list_load_definitions() -> dict[str, Any]:
        """Load patterns (with type and self-weight multiplier), load cases (with type) and combinations (with
        their cases and scale factors)."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            patterns = []
            for n in names_list(session, "LoadPatterns.GetNameList"):
                entry: dict[str, Any] = {"name": n}
                try:
                    entry["type"] = session.out("LoadPatterns.GetLoadType", Name=n).get("MyType")
                    entry["self_weight_multiplier"] = session.out("LoadPatterns.GetSelfWTMultiplier", Name=n).get("SelfWTMultiplier")
                except ITEM_ERRORS:
                    pass
                patterns.append(entry)
            cases = []
            for n in names_list(session, "LoadCases.GetNameList"):
                entry = {"name": n}
                try:
                    o = session.out("LoadCases.GetTypeOAPI_1", Name=n)
                    entry["type"] = o.get("CaseType")
                except ITEM_ERRORS:
                    pass
                cases.append(entry)
            combos = []
            for n in names_list(session, "RespCombo.GetNameList"):
                o = session.out("RespCombo.GetCaseList", Name=n)
                combos.append(
                    {
                        "name": n,
                        "items": [
                            {"name": cn, "kind": t, "scale_factor": sf}
                            for t, cn, sf in zip(o.get("CNameType") or [], o.get("CName") or [], o.get("SF") or [])
                        ],
                    }
                )
            return {"load_patterns": patterns, "load_cases": cases, "load_combinations": combos}

        return await session.run(work)


def _assign_one(session: EtabsSession, ld: LoadInput, name: str, item: str) -> None:
    d = DIRECTIONS[ld.direction]
    if ld.kind == "frame_distributed":
        v1 = ld.start_value if ld.start_value is not None else ld.value
        v2 = ld.end_value if ld.end_value is not None else ld.value
        if v1 is None or v2 is None:
            raise ArgumentValidationError("frame_distributed needs 'value' or 'start_value' + 'end_value'.")
        session.call(
            "FrameObj.SetLoadDistributed",
            {"Name": name, "LoadPat": ld.pattern, "MyType": 2 if ld.moment else 1, "Dir": d,
             "Dist1": ld.start_distance, "Dist2": ld.end_distance, "Val1": v1, "Val2": v2, "CSys": ld.csys,
             "RelDist": ld.relative_distance, "Replace": ld.replace, "ItemType": item},
            _internal=True,
        )
    elif ld.kind == "frame_point":
        if ld.value is None:
            raise ArgumentValidationError("frame_point needs 'value' (and 'start_distance' for its position).")
        session.call(
            "FrameObj.SetLoadPoint",
            {"Name": name, "LoadPat": ld.pattern, "MyType": 2 if ld.moment else 1, "Dir": d,
             "Dist": ld.start_distance, "Val": ld.value, "CSys": ld.csys, "RelDist": ld.relative_distance,
             "Replace": ld.replace, "ItemType": item},
            _internal=True,
        )
    elif ld.kind == "area_uniform":
        if ld.value is None:
            raise ArgumentValidationError("area_uniform needs 'value'.")
        session.call(
            "AreaObj.SetLoadUniform",
            {"Name": name, "LoadPat": ld.pattern, "Value": ld.value, "Dir": d, "Replace": ld.replace,
             "CSys": ld.csys, "ItemType": item},
            _internal=True,
        )
    else:
        if ld.forces is None:
            raise ArgumentValidationError("point_force needs 'forces' = [FX, FY, FZ, MX, MY, MZ].")
        session.call(
            "PointObj.SetLoadForce",
            {"Name": name, "LoadPat": ld.pattern, "Value": ld.forces, "Replace": ld.replace, "CSys": ld.csys,
             "ItemType": item},
            _internal=True,
        )
