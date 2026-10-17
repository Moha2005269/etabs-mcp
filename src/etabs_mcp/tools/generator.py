"""Generate a complete regular building in one call: grids, stories, materials, sections, columns, beams,
braces or shear walls, slabs, supports, diaphragm, gravity loads and design combinations."""

from __future__ import annotations

import time
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..errors import ITEM_ERRORS, ArgumentValidationError, EtabsCallFailed
from ..session import EtabsSession
from ..units import Length, LengthConverter
from .common import DESTRUCTIVE, names_list, suspended_tree
from .model import Units, _guard_replace, stories
from .properties import import_library_section

System = Literal["steel_moment", "steel_braced", "concrete_frame", "concrete_wall"]
STEEL_DEFAULTS = {"column": "W14X90", "beam": "W18X35", "brace": "W10X33"}


def _write_grid(session: EtabsSession, xs: list[float], ys: list[float]) -> str | None:
    """Replace grid system G1 with the given ordinates (NewGridOnly only makes uniform grids)."""
    key = "Grid Definitions - Grid Lines"
    try:
        o = session.out("DatabaseTables.GetTableForEditingArray", TableKey=key, GroupName="")
        fields = list(o["FieldsKeysIncluded"])
        letters = [chr(ord("A") + i) if i < 26 else f"A{chr(ord('A') + i - 26)}" for i in range(len(xs))]
        rows = [{"Name": "G1", "LineType": "X (Cartesian)", "ID": letters[i], "Ordinate": x, "BubbleLoc": "End",
                 "Visible": "Yes"} for i, x in enumerate(xs)]
        rows += [{"Name": "G1", "LineType": "Y (Cartesian)", "ID": str(j + 1), "Ordinate": y, "BubbleLoc": "Start",
                  "Visible": "Yes"} for j, y in enumerate(ys)]
        flat = [str(r.get(f, "")) for r in rows for f in fields]
        session.call("DatabaseTables.SetTableForEditingArray",
                     {"TableKey": key, "TableVersion": o.get("TableVersion", 0), "FieldsKeysIncluded": fields,
                      "NumberRecords": len(rows), "TableData": flat}, _internal=True)
        log = session.out("DatabaseTables.ApplyEditedTables", FillImportLog=True)
        if int(log.get("NumFatalErrors") or 0) or int(log.get("NumErrorMsgs") or 0):
            return "Grid lines could not be updated; geometry is unaffected."
    except ITEM_ERRORS as exc:
        return f"Grid lines were not updated ({exc.message}); geometry is unaffected."
    return None


def lateral_bays(nx: int, ny: int) -> list[tuple[str, int, int]]:
    """End bays of each perimeter frame line: ('x', bay index, y line) along X faces, ('y', bay, x line)."""
    out: list[tuple[str, int, int]] = []
    for yl in (0, ny):
        for b in sorted({0, nx - 1}):
            out.append(("x", b, yl))
    for xl in (0, nx):
        for b in sorted({0, ny - 1}):
            out.append(("y", b, xl))
    return out


def register(mcp: FastMCP, session: EtabsSession) -> None:
    @mcp.tool(annotations=DESTRUCTIVE)
    async def etabs_generate_building(
        bays_x: Annotated[list[Length], Field(min_length=1, max_length=40, description="Bay widths along X, e.g. ['8 m', '8 m', '6 m'].")],
        bays_y: Annotated[list[Length], Field(min_length=1, max_length=40, description="Bay widths along Y.")],
        story_heights: Annotated[list[Length], Field(min_length=1, max_length=200, description="Story heights bottom-up, e.g. ['4.5 m', '3.5 m', '3.5 m'].")],
        system: Annotated[System, Field(description="Lateral system. Braces/walls go in the end bays of every perimeter frame line.")] = "steel_moment",
        units: Units = "kN_m_C",
        column_section: Annotated[str | None, Field(description="Steel: AISC16 shape (default W14X90). Concrete: ignored, use column_size.")] = None,
        beam_section: Annotated[str | None, Field(description="Steel: AISC16 shape (default W18X35).")] = None,
        brace_section: Annotated[str | None, Field(description="Steel braced: AISC16 shape (default W10X33).")] = None,
        column_size: Annotated[list[Length], Field(min_length=2, max_length=2, description="Concrete column [depth, width].")] = ["600 mm", "600 mm"],  # noqa: B006
        beam_size: Annotated[list[Length], Field(min_length=2, max_length=2, description="Concrete beam [depth, width].")] = ["600 mm", "300 mm"],  # noqa: B006
        slab_thickness: Length = "200 mm",
        wall_thickness: Length = "300 mm",
        superimposed_dead: Annotated[float | None, Field(description="SDL area load in present force/length² units (e.g. 1.5 for kN/m²).")] = 1.5,
        live: Annotated[float | None, Field(description="Live area load in present force/length² units.")] = 3.0,
        diaphragm: bool = True,
        confirm: Annotated[bool, Field(description="Required when the open model has objects: it is replaced.")] = False,
    ) -> dict[str, Any]:
        """Build a complete regular building in one call: grids, stories, materials, sections, columns, beams,
        braces or shear walls (with pier labels), slabs, fixed supports, a rigid diaphragm, SDL/LL patterns with
        slab loads, and default design combinations. Follow with lateral loads (etabs_define_seismic_load ...),
        then analysis."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            _guard_replace(session, confirm)
            t0 = time.monotonic()
            L = LengthConverter(units)
            bx = [L(v, "bays_x") for v in bays_x]
            by = [L(v, "bays_y") for v in bays_y]
            hs = [L(v, "story_heights") for v in story_heights]
            if min(bx + by + hs) <= 0:
                raise ArgumentValidationError("Bay widths and story heights must be positive.")
            xs = [sum(bx[:i]) for i in range(len(bx) + 1)]
            ys = [sum(by[:j]) for j in range(len(by) + 1)]
            zs = [sum(hs[:k]) for k in range(len(hs) + 1)]
            notes: list[str] = []

            session.call("InitializeNewModel", {"Units": units}, _internal=True)
            session.call("File.NewGridOnly", {
                "NumberStorys": len(hs), "TypicalStoryHeight": hs[-1], "BottomStoryHeight": hs[0],
                "NumberLinesX": len(xs), "NumberLinesY": len(ys), "SpacingX": bx[0], "SpacingY": by[0]},
                _internal=True)
            if len(set(hs[1:])) > 1:
                session.call("Story.SetStories_2", {
                    "BaseElevation": 0.0, "NumberStories": len(hs),
                    "StoryNames": [f"Story{k + 1}" for k in range(len(hs))], "StoryHeights": hs,
                    "IsMasterStory": [k == len(hs) - 1 for k in range(len(hs))], "SimilarToStory": [""] * len(hs),
                    "SpliceAbove": [False] * len(hs), "SpliceHeight": [0.0] * len(hs), "color": [-1] * len(hs)},
                    _internal=True)
            if len(set(bx)) > 1 or len(set(by)) > 1:
                note = _write_grid(session, xs, ys)
                if note:
                    notes.append(note)

            steel = system.startswith("steel")
            if steel:
                mat = "A992Fy50"
                if mat not in names_list(session, "PropMaterial.GetNameList"):
                    session.call("PropMaterial.AddMaterial", {"Name": mat, "MatType": "Steel", "Region": "United States",
                                                              "Standard": "ASTM A992", "Grade": "Grade 50",
                                                              "UserName": mat}, _internal=True)
                col = import_library_section(session, column_section or STEEL_DEFAULTS["column"], mat, "AISC16.xml",
                                             column_section or STEEL_DEFAULTS["column"])["section"]
                beam = import_library_section(session, beam_section or STEEL_DEFAULTS["beam"], mat, "AISC16.xml",
                                              beam_section or STEEL_DEFAULTS["beam"])["section"]
                brace = None
                if system == "steel_braced":
                    brace = import_library_section(session, brace_section or STEEL_DEFAULTS["brace"], mat, "AISC16.xml",
                                                   brace_section or STEEL_DEFAULTS["brace"])["section"]
            else:
                mat = "4000Psi"
                if mat not in names_list(session, "PropMaterial.GetNameList"):
                    raise EtabsCallFailed("The default concrete material '4000Psi' is missing.", path="PropMaterial")
                cd, cw = (L(v, "column_size") for v in column_size)
                bd, bw = (L(v, "beam_size") for v in beam_size)
                col, beam = "GEN-COL", "GEN-BEAM"
                session.call("PropFrame.SetRectangle", {"Name": col, "MatProp": mat, "T3": cd, "T2": cw}, _internal=True)
                session.call("PropFrame.SetRectangle", {"Name": beam, "MatProp": mat, "T3": bd, "T2": bw}, _internal=True)
                session.call("PropFrame.SetRebarBeam", {"Name": beam, "MatPropLong": "A615Gr60",
                                                        "MatPropConfine": "A615Gr60", "CoverTop": 0.05 * _m(units),
                                                        "CoverBot": 0.05 * _m(units), "TopLeftArea": 0,
                                                        "TopRightArea": 0, "BotLeftArea": 0, "BotRightArea": 0},
                             _internal=True)
                brace = None
            slab_mat = "4000Psi" if "4000Psi" in names_list(session, "PropMaterial.GetNameList") else mat
            session.call("PropArea.SetSlab", {"Name": "GEN-SLAB", "SlabType": "Slab", "ShellType": "Membrane" if steel else "ShellThin",
                                              "MatProp": slab_mat, "Thickness": L(slab_thickness, "slab_thickness")},
                         _internal=True)
            wall = None
            if system == "concrete_wall":
                wall = "GEN-WALL"
                session.call("PropArea.SetWall", {"Name": wall, "WallPropType": "Specified", "ShellType": "ShellThin",
                                                  "MatProp": mat, "Thickness": L(wall_thickness, "wall_thickness")},
                             _internal=True)

            nx, ny = len(bx), len(by)
            special = lateral_bays(nx, ny) if system in ("steel_braced", "concrete_wall") else []
            counts = {"columns": 0, "beams": 0, "braces": 0, "walls": 0, "slabs": 0}
            piers: dict[str, list[str]] = {}

            def frame(a, b, prop, key):
                session.out("FrameObj.AddByCoord", XI=a[0], YI=a[1], ZI=a[2], XJ=b[0], YJ=b[1], ZJ=b[2], PropName=prop)
                counts[key] += 1

            def area(pts, prop, key):
                o = session.out("AreaObj.AddByCoord", NumberPoints=len(pts), X=[p[0] for p in pts],
                                Y=[p[1] for p in pts], Z=[p[2] for p in pts], PropName=prop)
                counts[key] += 1
                return o["Name"]

            total = len(zs) * len(xs) * len(ys) * 3
            with suspended_tree(session, total):
                for k in range(len(hs)):
                    z0, z1 = zs[k], zs[k + 1]
                    for x in xs:
                        for y in ys:
                            frame((x, y, z0), (x, y, z1), col, "columns")
                    for y in ys:
                        for i in range(nx):
                            frame((xs[i], y, z1), (xs[i + 1], y, z1), beam, "beams")
                    for x in xs:
                        for j in range(ny):
                            frame((x, ys[j], z1), (x, ys[j + 1], z1), beam, "beams")
                    for axis, bay, line in special:
                        if axis == "x":
                            a, b, c = (xs[bay], ys[line]), (xs[bay + 1], ys[line]), f"X{bay}-{line}"
                        else:
                            a, b, c = (xs[line], ys[bay]), (xs[line], ys[bay + 1]), f"Y{bay}-{line}"
                        if brace:
                            frame((a[0], a[1], z0), (b[0], b[1], z1), brace, "braces")
                            frame((b[0], b[1], z0), (a[0], a[1], z1), brace, "braces")
                        if wall:
                            name = area([(a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z1), (a[0], a[1], z1)], wall, "walls")
                            piers.setdefault(f"P-{c}", []).append(name)
                    for i in range(nx):
                        for j in range(ny):
                            area([(xs[i], ys[j], z1), (xs[i + 1], ys[j], z1), (xs[i + 1], ys[j + 1], z1),
                                  (xs[i], ys[j + 1], z1)], "GEN-SLAB", "slabs")

                existing = set(names_list(session, "PierLabel.GetNameList"))
                for label, members in piers.items():
                    if label not in existing:
                        session.call("PierLabel.SetPier", {"Name": label}, _internal=True)
                    for a in members:
                        session.call("AreaObj.SetPier", {"Name": a, "PierName": label}, _internal=True)

                pts = session.out("PointObj.GetAllPoints")
                for n, z in zip(pts.get("MyName") or [], pts.get("Z") or []):
                    if abs(z) < 1e-9:
                        session.call("PointObj.SetRestraint", {"Name": n, "Value": [True] * 6}, _internal=True)

                slabs = []
                for st in names_list(session, "Story.GetNameList"):
                    for a in session.out("AreaObj.GetNameListOnStory", StoryName=st).get("MyName") or []:
                        prop = session.out("AreaObj.GetProperty", Name=a).get("PropName")
                        if prop == "GEN-SLAB":
                            slabs.append(a)
                if diaphragm:
                    if "D1" not in names_list(session, "Diaphragm.GetNameList"):
                        session.call("Diaphragm.SetDiaphragm", {"Name": "D1", "SemiRigid": False}, _internal=True)
                    for a in slabs:
                        session.call("AreaObj.SetDiaphragm", {"Name": a, "DiaphragmName": "D1"}, _internal=True)

                patterns = set(names_list(session, "LoadPatterns.GetNameList"))
                for name, kind, value in (("SDL", "SuperDead", superimposed_dead), ("LL", "Live", live)):
                    if value is None:
                        continue
                    if name not in patterns:
                        session.call("LoadPatterns.Add", {"Name": name, "MyType": kind, "SelfWTMultiplier": 0,
                                                          "AddAnalysisCase": True}, _internal=True)
                    for a in slabs:
                        session.call("AreaObj.SetLoadUniform", {"Name": a, "LoadPat": name, "Value": value, "Dir": 10},
                                     _internal=True)
            session.call("RespCombo.AddDesignDefaultCombos", {"DesignSteel": steel, "DesignConcrete": not steel,
                                                              "DesignAluminum": False, "DesignColdFormed": False},
                         _internal=True)
            try:
                session.call("View.RefreshView", {"Window": 0, "Zoom": True}, _internal=True)
            except ITEM_ERRORS:
                pass
            out: dict[str, Any] = {
                "system": system, "units": units, "seconds": round(time.monotonic() - t0, 1),
                "grid": {"x": xs, "y": ys}, "stories": stories(session)["stories"], "counts": counts,
                "sections": {"column": col, "beam": beam, "brace": brace, "slab": "GEN-SLAB", "wall": wall},
                "piers": sorted(piers), "diaphragm": "D1" if diaphragm else None,
                "next": "Add lateral loads (etabs_define_seismic_load / etabs_define_wind_load), save, then "
                        "etabs_run_analysis.",
            }
            if notes:
                out["notes"] = notes
            return out

        return await session.run(work)


def _m(units: str) -> float:
    """Metres expressed in the given units' length unit."""
    from ..units import TO_METRES, length_unit

    return 1.0 / TO_METRES[length_unit(units)]
