"""Connection tools: find ETABS, attach to or launch it, report status, disconnect."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..discovery import find_installations, find_running_etabs
from ..errors import ITEM_ERRORS
from ..session import EtabsSession
from .common import DESTRUCTIVE, READ, WRITE, model_file, require_confirm


def register(mcp: FastMCP, session: EtabsSession) -> None:
    @mcp.tool(annotations=READ)
    async def etabs_list_installations() -> dict[str, Any]:
        """List ETABS installations on this machine (v22+ are supported) and ETABS instances that are running now."""

        def work() -> dict[str, Any]:
            return {
                "installations": [i.to_dict() for i in find_installations(session.settings.extra_search_dirs)],
                "running": [r.to_dict() for r in find_running_etabs()],
            }

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_connect(
        mode: Annotated[
            Literal["auto", "attach", "start"],
            Field(description="attach = use a running ETABS; start = launch a new one; auto = attach if one is running, else start."),
        ] = "auto",
        version: Annotated[int | None, Field(description="ETABS major version to use, e.g. 22 or 23. Default: the running one, else the newest installed.")] = None,
        install_dir: Annotated[str | None, Field(description="ETABS program folder, overrides 'version'.")] = None,
        pid: Annotated[int | None, Field(description="Attach to this ETABS process id (see etabs_list_installations).")] = None,
        visible: Annotated[bool, Field(description="Show the ETABS window when launching it.")] = True,
        reconnect: Annotated[bool, Field(description="Drop the current connection first.")] = False,
    ) -> dict[str, Any]:
        """Connect to ETABS. Other tools connect automatically with mode 'auto', so call this only to choose a
        specific instance or version, or to recover after ETABS was closed. Launching ETABS takes 10-30 s."""

        def work() -> dict[str, Any]:
            return session.connect_sync(
                mode=mode,
                install_dir=Path(install_dir) if install_dir else None,
                major=version,
                pid=pid,
                visible=visible,
                reconnect=reconnect,
            )

        return await session.run(work)

    @mcp.tool(annotations=READ)
    async def etabs_status() -> dict[str, Any]:
        """Connection and model status without connecting: whether ETABS is attached, its version, the open
        model file, present units, and whether the model is locked (has analysis results)."""

        def work() -> dict[str, Any]:
            info: dict[str, Any] = dict(session.backend.info())
            info["read_only_mode"] = session.settings.read_only
            if session.backend.connected and session.backend.is_alive():
                try:
                    info["model_file"] = model_file(session) or "(untitled)"
                except ITEM_ERRORS:
                    info["model_file"] = None
                for key, path in (
                                        ("present_units", "GetPresentUnits"),
                    ("model_locked", "GetModelIsLocked"),
                ):
                    try:
                        info[key] = session.value(path)
                    except ITEM_ERRORS as exc:
                        info[key] = None
                        info.setdefault("warnings", []).append(f"{path}: {exc.message}")
            elif session.backend.connected:
                info["connected"] = False
                info["warning"] = "The ETABS connection is stale (ETABS was closed). Call etabs_connect."
            return info

        return await session.run(work)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def etabs_disconnect(
        close_etabs: Annotated[bool, Field(description="Also exit the ETABS application.")] = False,
        save: Annotated[bool, Field(description="Save the model before exiting (only with close_etabs).")] = False,
        confirm: Annotated[bool, Field(description="Required when closing ETABS without saving.")] = False,
    ) -> dict[str, Any]:
        """Release the ETABS connection. ETABS keeps running unless close_etabs is true."""

        def work() -> dict[str, Any]:
            if close_etabs and not save:
                require_confirm(confirm, session, "Closing ETABS without saving discards unsaved changes and")
            session.disconnect_sync(close_app=close_etabs, save=save)
            return {"connected": False, "closed_etabs": close_etabs}

        return await session.run(work)
