"""Model-level tools: overview, new/open/save, units and stories."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from ..errors import ITEM_ERRORS, ArgumentValidationError, ConfirmationRequired
from ..session import EtabsSession
from .common import DESTRUCTIVE, READ, WRITE, model_file, model_has_objects, names_list

Units = Literal[
    "lb_in_F", "lb_ft_F", "kip_in_F", "kip_ft_F", "kN_mm_C", "kN_m_C", "kgf_mm_C", "kgf_m_C",
    "N_mm_C", "N_m_C", "Ton_mm_C", "Ton_m_C", "kN_cm_C", "kgf_cm_C", "N_cm_C", "Ton_cm_C",
]


class StoryInput(BaseModel):
    name: str
    height: float = Field(gt=0, description="Story height in present length units.")
    is_master: bool = False
    similar_to: str = Field("", description="Master story this story is similar to ('' for none).")
    splice_above: bool = False
    splice_height: float = 0.0


def _safe(session: EtabsSession, fn: Any, warnings: list[str], label: str) -> Any:
    try:
        return fn()
    except ITEM_ERRORS as exc:
        warnings.append(f"{label}: {exc.message}")
        return None


def stories(session: EtabsSession) -> dict[str, Any]:
    o = session.out("Story.GetStories_2")
    names = o.get("StoryNames") or []
    rows = [
        {
            "name": names[i],
            "elevation": o["StoryElevations"][i],
            "height": o["StoryHeights"][i],
            "is_master": o["IsMasterStory"][i],
            "similar_to": o["SimilarToStory"][i],
            "splice_above": o["SpliceAbove"][i],
            "splice_height": o["SpliceHeight"][i],
        }
        for i in range(len(names))
    ]
    # ETABS lists stories bottom-up; agents read top-down like the ETABS story table.
    return {"base_elevation": o.get("BaseElevation"), "stories": list(reversed(rows))}


def register(mcp: FastMCP, session: EtabsSession) -> None:
    @mcp.tool(annotations=READ)
    async def etabs_model_overview() -> dict[str, Any]:
        """One-call summary of the open model: file, version, units, lock state, object counts, stories,
        load patterns/cases/combinations, materials, sections and groups. Start every task with this."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            w: list[str] = []
            info: dict[str, Any] = {
                "file": _safe(session, lambda: model_file(session), w, "file"),
                "etabs_version": session.backend.info().get("etabs_version"),
                "present_units": _safe(session, lambda: session.value("GetPresentUnits"), w, "units"),
                "database_units": _safe(session, lambda: session.value("GetDatabaseUnits"), w, "db units"),
                "locked": _safe(session, lambda: session.value("GetModelIsLocked"), w, "lock"),
            }
            info["counts"] = {
                k: _safe(session, lambda p=p: session.value(p), w, k)
                for k, p in (
                    ("points", "PointObj.Count"),
                    ("frames", "FrameObj.Count"),
                    ("areas", "AreaObj.Count"),
                    ("links", "LinkObj.Count"),
                )
            }
            st = _safe(session, lambda: stories(session), w, "stories")
            info["model_open"] = st is not None
            info["stories"] = st
            for key, path in (
                ("load_patterns", "LoadPatterns.GetNameList"),
                ("load_cases", "LoadCases.GetNameList"),
                ("load_combinations", "RespCombo.GetNameList"),
                ("materials", "PropMaterial.GetNameList"),
                ("frame_sections", "PropFrame.GetNameList"),
                ("area_sections", "PropArea.GetNameList"),
                ("groups", "GroupDef.GetNameList"),
            ):
                info[key] = _safe(session, lambda p=path: names_list(session, p), w, key)
            # Long section lists are rarely useful in an overview.
            for key in ("frame_sections",):
                if info[key] and len(info[key]) > 60:
                    info[key] = {"count": len(info[key]), "first": info[key][:60]}
            if not info["model_open"]:
                w.append("No model appears to be open. Use etabs_new_model or etabs_open_model.")
            if w:
                info["warnings"] = w
            return info

        return await session.run(work)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def etabs_new_model(
        template: Annotated[
            Literal["blank", "grid_only", "steel_deck"],
            Field(description="blank = empty model; grid_only = grids + stories; steel_deck = grids, stories and a steel floor system."),
        ] = "grid_only",
        units: Annotated[Units, Field(description="Units for the new model.")] = "kN_m_C",
        number_of_stories: Annotated[int, Field(ge=1, le=500)] = 3,
        typical_story_height: Annotated[float, Field(gt=0)] = 3.0,
        bottom_story_height: Annotated[float, Field(gt=0)] = 3.0,
        grid_lines_x: Annotated[int, Field(ge=1)] = 4,
        grid_lines_y: Annotated[int, Field(ge=1)] = 4,
        spacing_x: Annotated[float, Field(gt=0)] = 6.0,
        spacing_y: Annotated[float, Field(gt=0)] = 6.0,
        confirm: Annotated[bool, Field(description="Required when the current model has objects: it is replaced without saving.")] = False,
    ) -> dict[str, Any]:
        """Start a new model (replaces the open one). Lengths are in the chosen units."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            _guard_replace(session, confirm)
            session.call("InitializeNewModel", {"Units": units}, _internal=True)
            if template == "blank":
                session.call("File.NewBlank", _internal=True)
            else:
                session.call(
                    "File.NewGridOnly" if template == "grid_only" else "File.NewSteelDeck",
                    {
                        "NumberStorys": number_of_stories,
                        "TypicalStoryHeight": typical_story_height,
                        "BottomStoryHeight": bottom_story_height,
                        "NumberLinesX": grid_lines_x,
                        "NumberLinesY": grid_lines_y,
                        "SpacingX": spacing_x,
                        "SpacingY": spacing_y,
                    },
                    _internal=True,
                )
            return {"created": template, "units": session.value("GetPresentUnits"),
                    "stories": None if template == "blank" else stories(session)}

        return await session.run(work)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def etabs_open_model(
        path: Annotated[str, Field(description="Full path of an .EDB (or importable .e2k/.$et) file.")],
        confirm: Annotated[bool, Field(description="Required when the current model has objects: it is closed without saving.")] = False,
    ) -> dict[str, Any]:
        """Open an existing ETABS model file (.EDB, or an importable .e2k text file). Replaces the open model."""

        def work() -> dict[str, Any]:
            p = Path(path)
            if not p.is_file():
                raise ArgumentValidationError(f"File not found: {p}")
            session.ensure_connected()
            _guard_replace(session, confirm)
            session.call("File.OpenFile", {"FileName": str(p)}, _internal=True)
            return {"opened": model_file(session),
                    "units": session.value("GetPresentUnits")}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_save_model(
        path: Annotated[str | None, Field(description="Save as this .EDB path. Omit to save to the current file.")] = None,
    ) -> dict[str, Any]:
        """Save the model. A new (untitled) model needs a path. Analysis also requires a saved model."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            current = model_file(session)
            if not path and not current:
                raise ArgumentValidationError(
                    "The model has never been saved, so a path is required.",
                    hint="Pass path, e.g. C:\\\\Models\\\\Building.EDB",
                )
            target = path or current
            if path:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            session.call("File.Save", {"FileName": target}, _internal=True)
            return {"saved": model_file(session)}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_set_units(units: Units) -> dict[str, Any]:
        """Set the present units. All tool inputs and outputs use the present units."""

        def work() -> dict[str, Any]:
            session.call("SetPresentUnits", {"Units": units}, _internal=True)
            return {"present_units": session.value("GetPresentUnits")}

        return await session.run(work)

    @mcp.tool(annotations=READ)
    async def etabs_get_stories() -> dict[str, Any]:
        """Story table, top story first: name, elevation, height, master/similar settings."""
        return await session.run(lambda: stories(session))

    @mcp.tool(annotations=WRITE)
    async def etabs_set_stories(
        stories_bottom_up: Annotated[list[StoryInput], Field(min_length=1, description="Stories from the lowest to the highest.")],
        base_elevation: float = 0.0,
    ) -> dict[str, Any]:
        """Redefine the story table. ETABS only accepts this on a model without objects on the affected stories."""

        def work() -> dict[str, Any]:
            items = stories_bottom_up
            session.call(
                "Story.SetStories_2",
                {
                    "BaseElevation": base_elevation,
                    "NumberStories": len(items),
                    "StoryNames": [s.name for s in items],
                    "StoryHeights": [s.height for s in items],
                    "IsMasterStory": [s.is_master for s in items],
                    "SimilarToStory": [s.similar_to for s in items],
                    "SpliceAbove": [s.splice_above for s in items],
                    "SpliceHeight": [s.splice_height for s in items],
                    "color": [-1] * len(items),
                },
                _internal=True,
            )
            return stories(session)

        return await session.run(work)



def _guard_replace(session: EtabsSession, confirm: bool) -> None:
    if confirm or session.settings.allow_destructive:
        return
    if model_has_objects(session):
        raise ConfirmationRequired(
            "The open model has objects and will be replaced without saving.",
            hint="Save it first (etabs_save_model) or confirm with the user and pass confirm=true.",
        )
