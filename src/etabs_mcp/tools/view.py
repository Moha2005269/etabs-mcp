"""View tools: see the ETABS window, refresh it, and show or hide it."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP, Image
from pydantic import Field

from ..discovery import find_running_etabs, main_window_for_pid
from ..errors import ITEM_ERRORS, NotConnectedError
from ..screenshot import capture_window
from ..session import EtabsSession
from .common import READ, WRITE


def register(mcp: FastMCP, session: EtabsSession) -> None:
    @mcp.tool(annotations=READ)
    async def etabs_screenshot(
        refresh: Annotated[bool, Field(description="Refresh the model views first so recent API edits are drawn.")] = True,
        zoom_to_fit: Annotated[bool, Field(description="Zoom all views to the full model.")] = False,
        max_width: Annotated[int, Field(ge=320, le=3840)] = 1600,
        capture: Annotated[
            Literal["screen", "offscreen"],
            Field(description="screen = what is on the monitor (brings ETABS forward for a moment, then restores focus); "
                              "offscreen = never changes focus, but ETABS's GPU model views come out blank."),
        ] = "screen",
    ) -> Image:
        """Take a screenshot of the ETABS window: the model views, selection and any open dialog, exactly what an
        engineer sees. Use it to check geometry, deformed shapes or design colours after other tools ran."""

        def work() -> Image:
            session.ensure_connected()
            if refresh:
                try:
                    session.call("View.RefreshView", {"Window": 0, "Zoom": zoom_to_fit}, _internal=True)
                except ITEM_ERRORS:
                    pass
            pid = getattr(session.backend, "pid", None)
            proc = main_window_for_pid(pid) if pid else None
            if proc is None:
                running = find_running_etabs()
                proc = running[-1] if len(running) == 1 else None
            if proc is None or proc.hwnd is None:
                raise NotConnectedError(
                    "Could not find the ETABS window.",
                    hint="The window may be hidden: call etabs_show_window(visible=true).",
                )
            png, _meta = capture_window(proc.hwnd, max_width=max_width,
                                         mode="auto" if capture == "screen" else "printwindow")
            return Image(data=png, format="png")

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_show_window(visible: bool = True) -> dict[str, Any]:
        """Show or hide the ETABS application window. Hidden ETABS runs faster for batch work."""

        def work() -> dict[str, Any]:
            session.call("App.Unhide" if visible else "App.Hide", _internal=True)
            return {"visible": session.value("App.Visible")}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_refresh_view(zoom_to_fit: bool = True) -> dict[str, Any]:
        """Redraw the ETABS model views (and optionally zoom to fit) after model changes made through the API."""

        def work() -> dict[str, Any]:
            session.call("View.RefreshView", {"Window": 0, "Zoom": zoom_to_fit}, _internal=True)
            return {"refreshed": True}

        return await session.run(work)
