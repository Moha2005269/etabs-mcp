"""Geometry tools: draw frames, areas and points, list and delete objects, supports, sections, selection."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from ..errors import ITEM_ERRORS, ArgumentValidationError
from ..session import EtabsSession
from ..units import LengthConverter
from .common import (
    DESTRUCTIVE,
    OBJ_INTERFACE,
    READ,
    SELECTED_TYPES,
    WRITE,
    ObjectKind,
    batch_item_error,
    require_confirm,
    suspended_tree,
    targets,
)

Point3 = Annotated[list[float | str], Field(min_length=3, max_length=3, description="[x, y, z]: numbers in present length units, or strings like '6 m' or '20 ft'.")]

RESTRAINT_PRESETS = {
    "fixed": [True] * 6,
    "pinned": [True, True, True, False, False, False],
    "roller_z": [False, False, True, False, False, False],
    "free": [False] * 6,
}


class FrameInput(BaseModel):
    start: Point3
    end: Point3
    section: str = Field("Default", description="Frame section name ('Default' uses the program default).")
    name: str = Field("", description="Optional user label/name for the new frame.")


class AreaInput(BaseModel):
    points: list[Point3] = Field(min_length=3, description="Corner points in order (3 or more).")
    property: str = Field("Default", description="Slab/deck/wall property name.")
    name: str = ""


class PointInput(BaseModel):
    coordinates: Point3
    name: str = ""


def register(mcp: FastMCP, session: EtabsSession) -> None:
    @mcp.tool(annotations=WRITE)
    async def etabs_add_frames(
        frames: Annotated[list[FrameInput], Field(min_length=1, max_length=5000)],
        csys: str = "Global",
    ) -> dict[str, Any]:
        """Draw beams, columns and braces between two points (like Draw > Frame). Joints are created or merged
        automatically. Returns the ETABS names of the new frames, in input order; failures are listed per item."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            L = LengthConverter(session.value("GetPresentUnits"))
            created, errors = [], []
            with suspended_tree(session, len(frames)):
                for i, f in enumerate(frames):
                    try:
                        start, end = L.point(f.start, "start"), L.point(f.end, "end")
                        o = session.out(
                            "FrameObj.AddByCoord",
                            XI=start[0], YI=start[1], ZI=start[2],
                            XJ=end[0], YJ=end[1], ZJ=end[2],
                            PropName=f.section, UserName=f.name, CSys=csys,
                        )
                        created.append(o["Name"])
                    except ITEM_ERRORS as exc:
                        created.append(None)
                        errors.append(batch_item_error(i, exc, f.model_dump()))
            return {"created": created, "count": sum(n is not None for n in created), "errors": errors}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_add_areas(
        areas: Annotated[list[AreaInput], Field(min_length=1, max_length=5000)],
        csys: str = "Global",
    ) -> dict[str, Any]:
        """Draw floors, slabs and walls from corner points (like Draw > Floor/Wall). Returns new area names."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            created, errors = [], []
            L = LengthConverter(session.value("GetPresentUnits"))
            with suspended_tree(session, len(areas)):
                for i, a in enumerate(areas):
                    try:
                        pts = [L.point(p, f"points[{j}]") for j, p in enumerate(a.points)]
                        o = session.out(
                            "AreaObj.AddByCoord",
                            NumberPoints=len(pts),
                            X=[p[0] for p in pts], Y=[p[1] for p in pts], Z=[p[2] for p in pts],
                            PropName=a.property, UserName=a.name, CSys=csys,
                        )
                        created.append(o["Name"])
                    except ITEM_ERRORS as exc:
                        created.append(None)
                        errors.append(batch_item_error(i, exc, a.model_dump()))
            return {"created": created, "count": sum(n is not None for n in created), "errors": errors}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_add_points(
        points: Annotated[list[PointInput], Field(min_length=1, max_length=5000)],
        csys: str = "Global",
    ) -> dict[str, Any]:
        """Add joints at coordinates (existing joints at the same location are reused)."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            created, errors = [], []
            L = LengthConverter(session.value("GetPresentUnits"))
            with suspended_tree(session, len(points)):
                for i, p in enumerate(points):
                    try:
                        x, y, z = L.point(p.coordinates, "coordinates")
                        o = session.out("PointObj.AddCartesian", X=x, Y=y, Z=z, UserName=p.name, CSys=csys)
                        created.append(o["Name"])
                    except ITEM_ERRORS as exc:
                        created.append(None)
                        errors.append(batch_item_error(i, exc, p.model_dump()))
            return {"created": created, "errors": errors}

        return await session.run(work)

    @mcp.tool(annotations=READ)
    async def etabs_list_objects(
        kind: ObjectKind,
        story: Annotated[str | None, Field(description="Only objects on this story.")] = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=5000)] = 200,
        include_labels: Annotated[bool, Field(description="Add the label and story of each object (one extra call per object).")] = True,
    ) -> dict[str, Any]:
        """List frames, areas or points with their geometry (in present units), sections and stories.
        ETABS 'name' is the unique name used by every API call; 'label' is what the GUI shows per story."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            if kind == "frame":
                rows = _frames(session)
            elif kind == "area":
                rows = _areas(session)
            else:
                rows = _points(session)
            if story is not None:
                on_story = set(_names_on_story(session, kind, story))
                rows = [r for r in rows if r["name"] in on_story]
            total = len(rows)
            page = rows[offset: offset + limit]
            if include_labels:
                for r in page:
                    try:
                        o = session.out(f"{OBJ_INTERFACE[kind]}.GetLabelFromName", Name=r["name"])
                        r["label"], r["story"] = o.get("Label"), o.get("Story")
                    except ITEM_ERRORS:
                        pass
                    if kind == "area":
                        try:
                            r["property"] = session.out("AreaObj.GetProperty", Name=r["name"]).get("PropName")
                        except ITEM_ERRORS:
                            pass
            return {"kind": kind, "total": total, "offset": offset, "returned": len(page),
                    "units": session.value("GetPresentUnits"), "objects": page}

        return await session.run(work)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def etabs_delete_objects(
        kind: ObjectKind,
        names: list[str] | None = None,
        group: str | None = None,
        selected: bool = False,
        confirm: Annotated[bool, Field(description="Deleting cannot be undone through the API. Ask the user first.")] = False,
    ) -> dict[str, Any]:
        """Delete frames, areas or points chosen by name, by group, or the current selection."""

        def work() -> dict[str, Any]:
            require_confirm(confirm, session, "Deleting objects")
            done, errors = 0, []
            for i, (name, item) in enumerate(targets(names, group, selected)):
                try:
                    session.call(f"{OBJ_INTERFACE[kind]}.Delete", {"Name": name, "ItemType": item}, _internal=True)
                    done += 1
                except ITEM_ERRORS as exc:
                    errors.append(batch_item_error(i, exc, name))
            return {"deleted_calls_ok": done, "errors": errors,
                    "remaining": session.value(f"{OBJ_INTERFACE[kind]}.Count")}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_set_restraints(
        restraint: Annotated[
            Literal["fixed", "pinned", "roller_z", "free", "custom"],
            Field(description="Support type; 'custom' uses 'dofs'."),
        ] = "fixed",
        dofs: Annotated[list[bool] | None, Field(min_length=6, max_length=6, description="[UX, UY, UZ, RX, RY, RZ] for 'custom'.")] = None,
        points: Annotated[list[str] | None, Field(description="Joint names.")] = None,
        at_elevation: Annotated[float | str | None, Field(description="Apply to every joint at this Z, e.g. 0 or '0 m'.")] = None,
        group: str | None = None,
        selected: bool = False,
        tolerance: float = 1e-3,
    ) -> dict[str, Any]:
        """Assign supports (Assign > Joint > Restraints): fixed, pinned, roller or custom DOFs, to joints chosen by
        name, by elevation, by group, or the current selection."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            if restraint == "custom":
                if dofs is None:
                    raise ArgumentValidationError("restraint='custom' needs 'dofs' (6 booleans).")
                value = list(dofs)
            else:
                value = RESTRAINT_PRESETS[restraint]
            if at_elevation is not None:
                z0 = LengthConverter(session.value("GetPresentUnits"))(at_elevation, "at_elevation")
                if points or group or selected:
                    raise ArgumentValidationError("Use at_elevation alone, not with points/group/selected.")
                pts = [p["name"] for p in _points(session) if abs(p["z"] - z0) <= tolerance]
                if not pts:
                    raise ArgumentValidationError(f"No joints found at z = {z0} (tolerance {tolerance}).")
                chosen = [(n, "Objects") for n in pts]
            else:
                chosen = targets(points, group, selected)
            ok, errors = [], []
            with suspended_tree(session, len(chosen)):
                for i, (name, item) in enumerate(chosen):
                    try:
                        session.call("PointObj.SetRestraint", {"Name": name, "Value": value, "ItemType": item}, _internal=True)
                        ok.append(name or item)
                    except ITEM_ERRORS as exc:
                        errors.append(batch_item_error(i, exc, name))
            return {"restraint": dict(zip(["UX", "UY", "UZ", "RX", "RY", "RZ"], value)),
                    "assigned": ok, "errors": errors}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_assign_section(
        kind: Literal["frame", "area"],
        section: Annotated[str, Field(description="Frame section or area (slab/deck/wall) property name.")],
        names: list[str] | None = None,
        group: str | None = None,
        selected: bool = False,
    ) -> dict[str, Any]:
        """Assign a frame section or an area property (Assign > Frame > Section / Shell > Property)."""

        def work() -> dict[str, Any]:
            path = "FrameObj.SetSection" if kind == "frame" else "AreaObj.SetProperty"
            ok, errors = 0, []
            chosen = targets(names, group, selected)
            with suspended_tree(session, len(chosen)):
                for i, (name, item) in enumerate(chosen):
                    try:
                        session.call(path, {"Name": name, "PropName": section, "ItemType": item}, _internal=True)
                        ok += 1
                    except ITEM_ERRORS as exc:
                        errors.append(batch_item_error(i, exc, name))
            return {"section": section, "assigned_calls_ok": ok, "errors": errors}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_select(
        action: Annotated[
            Literal["get", "clear", "all", "invert", "previous", "group", "objects"],
            Field(description="'get' only reports the selection; 'objects' selects 'names' of 'kind'."),
        ] = "get",
        kind: ObjectKind | None = None,
        names: list[str] | None = None,
        group: str | None = None,
        deselect: bool = False,
    ) -> dict[str, Any]:
        """Change or read the selection, like clicking in the ETABS window. Many tools accept selected=true to act
        on it. Returns the selected objects grouped by type."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            if action == "clear":
                session.call("SelectObj.ClearSelection", _internal=True)
            elif action == "all":
                session.call("SelectObj.All", {"Deselect": deselect}, _internal=True)
            elif action == "invert":
                session.call("SelectObj.InvertSelection", _internal=True)
            elif action == "previous":
                session.call("SelectObj.PreviousSelection", _internal=True)
            elif action == "group":
                if not group:
                    raise ArgumentValidationError("action='group' needs 'group'.")
                session.call("SelectObj.Group", {"Name": group, "Deselect": deselect}, _internal=True)
            elif action == "objects":
                if not kind or not names:
                    raise ArgumentValidationError("action='objects' needs 'kind' and 'names'.")
                for n in names:
                    session.call(f"{OBJ_INTERFACE[kind]}.SetSelected", {"Name": n, "Selected": not deselect}, _internal=True)
            o = session.out("SelectObj.GetSelected")
            grouped: dict[str, list[str]] = {}
            for t, n in zip(o.get("ObjectType") or [], o.get("ObjectName") or []):
                grouped.setdefault(SELECTED_TYPES.get(t, str(t)), []).append(n)
            try:
                session.call("View.RefreshView", {"Window": 0, "Zoom": False}, _internal=True)
            except ITEM_ERRORS:
                pass
            return {"selected_count": o.get("NumberItems", 0), "selected": grouped}

        return await session.run(work)


def _frames(session: EtabsSession) -> list[dict[str, Any]]:
    o = session.out("FrameObj.GetAllFrames")
    names = o.get("MyName") or []
    return [
        {
            "name": names[i],
            "section": o["PropName"][i],
            "story": o["StoryName"][i],
            "point_i": o["PointName1"][i],
            "point_j": o["PointName2"][i],
            "start": [o["Point1X"][i], o["Point1Y"][i], o["Point1Z"][i]],
            "end": [o["Point2X"][i], o["Point2Y"][i], o["Point2Z"][i]],
            "angle": o["Angle"][i],
        }
        for i in range(len(names))
    ]


def _areas(session: EtabsSession) -> list[dict[str, Any]]:
    o = session.out("AreaObj.GetAllAreas")
    names = o.get("MyName") or []
    delim = o.get("PointDelimiter") or []
    rows, start = [], 0
    for i, n in enumerate(names):
        end = delim[i] + 1 if i < len(delim) else start
        rows.append(
            {
                "name": n,
                "points": o["PointNames"][start:end],
                "coordinates": [[o["PointX"][k], o["PointY"][k], o["PointZ"][k]] for k in range(start, end)],
                "design_orientation": (o.get("DesignOrientation") or [None] * len(names))[i],
            }
        )
        start = end
    return rows


def _points(session: EtabsSession) -> list[dict[str, Any]]:
    o = session.out("PointObj.GetAllPoints")
    names = o.get("MyName") or []
    return [{"name": names[i], "x": o["X"][i], "y": o["Y"][i], "z": o["Z"][i]} for i in range(len(names))]


def _names_on_story(session: EtabsSession, kind: str, story: str) -> list[str]:
    if kind == "point":
        # PointObj has no GetNameListOnStory: use the label lookup instead.
        names = []
        for p in _points(session):
            try:
                if session.out("PointObj.GetLabelFromName", Name=p["name"]).get("Story") == story:
                    names.append(p["name"])
            except ITEM_ERRORS:
                pass
        return names
    return list(session.out(f"{OBJ_INTERFACE[kind]}.GetNameListOnStory", StoryName=story).get("MyName") or [])
