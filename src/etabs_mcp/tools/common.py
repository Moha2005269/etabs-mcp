"""Helpers shared by the curated tools."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Literal

from mcp.types import ToolAnnotations

from ..errors import ITEM_ERRORS, ArgumentValidationError, ConfirmationRequired, EtabsMcpError
from ..session import EtabsSession

ObjectKind = Literal["frame", "area", "point"]
OBJ_INTERFACE = {"frame": "FrameObj", "area": "AreaObj", "point": "PointObj"}
# cSelect.GetSelected object type codes.
SELECTED_TYPES = {1: "point", 2: "frame", 3: "cable", 4: "tendon", 5: "area", 6: "solid", 7: "link"}

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)


def targets(
    names: Sequence[str] | None, group: str | None, selected: bool
) -> list[tuple[str, str]]:
    """Translate the (names | group | selected) choice into (Name, eItemType) pairs."""
    chosen = sum(bool(x) for x in (names, group, selected))
    if chosen != 1:
        raise ArgumentValidationError(
            "Specify exactly one of 'names', 'group' or 'selected'.",
            hint="Use selected=true to act on the current ETABS selection, or group='All' for every object.",
        )
    if selected:
        return [("", "SelectedObjects")]
    if group:
        return [(group, "Group")]
    return [(n, "Objects") for n in names or []]


def require_confirm(confirm: bool, session: EtabsSession, what: str) -> None:
    if not (confirm or session.settings.allow_destructive):
        raise ConfirmationRequired(
            f"{what} cannot be undone from the API.",
            hint="Confirm with the user, then call again with confirm=true.",
        )


def model_has_objects(session: EtabsSession) -> bool:
    try:
        return any(session.value(f"{i}.Count") for i in ("PointObj", "FrameObj", "AreaObj"))
    except ITEM_ERRORS:
        return False


def present_units(session: EtabsSession) -> str | None:
    try:
        return session.value("GetPresentUnits")
    except ITEM_ERRORS:
        return None


@contextmanager
def suspended_tree(session: EtabsSession, count: int, threshold: int = 25) -> Iterator[None]:
    """Suspend the ETABS model-explorer refresh during large batches; it dominates batch time."""
    suspend = count >= threshold
    if suspend:
        try:
            session.call("TreeSuspendUpdateData", {"updateAtResume": True}, _internal=True)
        except ITEM_ERRORS:
            suspend = False
    try:
        yield
    finally:
        if suspend:
            try:
                session.call("TreeResumeUpdateData", _internal=True)
            except ITEM_ERRORS:
                pass


def batch_item_error(index: int, exc: EtabsMcpError, item: Any = None) -> dict[str, Any]:
    entry = {"index": index, "error": exc.code, "message": exc.message}
    if exc.hint:
        entry["hint"] = exc.hint
    if item is not None:
        entry["item"] = item
    return entry


def names_list(session: EtabsSession, path: str, **kwargs: Any) -> list[str]:
    out = session.out(path, **kwargs)
    return list(out.get("MyName") or [])


def model_file(session: EtabsSession) -> str:
    """The model's .EDB path, or '' for an untitled model.

    ETABS reports the temporary text database (Model.$et) after interactive table edits, and the source file
    for imported .e2k models; the real model file is always the .EDB next to it."""
    from pathlib import Path

    name = session.value("GetModelFilename", IncludePath=True) or ""
    path = Path(name)
    if not name or not path.suffix or path.parent == Path(""):
        return ""
    if path.suffix.lower() != ".edb":
        path = path.with_suffix(".EDB")
    return str(path)
