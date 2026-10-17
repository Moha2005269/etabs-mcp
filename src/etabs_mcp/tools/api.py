"""Full-coverage tools: search, describe and call any ETABS API method, validated against its signature."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from ..catalog import _ci_key
from ..errors import ApiNotFoundError, EtabsMcpError
from ..session import EtabsSession
from .common import DESTRUCTIVE, READ


class ApiCall(BaseModel):
    path: str = Field(description="API path, e.g. 'FrameObj.SetSection' or 'LoadCases.StaticLinear.SetCase'.")
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments by parameter name.")


def register(mcp: FastMCP, session: EtabsSession) -> None:
    @mcp.tool(annotations=READ)
    async def etabs_search_api(
        query: Annotated[str, Field(description="Words to match against method names, interface paths and parameter names, e.g. 'frame release' or 'story drift'.")],
        interface: Annotated[str | None, Field(description="Restrict to one interface, e.g. 'cFrameObj'.")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 25,
    ) -> dict[str, Any]:
        """Search all ~1,300 ETABS API methods. Use this to find a method when no curated tool covers the task,
        then etabs_describe_api for its parameters and etabs_call_api to run it."""

        def work() -> dict[str, Any]:
            cat = session.catalog
            return {"catalog": cat.source, "results": cat.search(query, limit=limit, interface=interface)}

        return await session.run(work)

    @mcp.tool(annotations=READ)
    async def etabs_describe_api(
        path: Annotated[str, Field(description="A method path ('FrameObj.GetReleases'), an interface ('cFrameObj' or 'FrameObj') or an enum name ('eLoadPatternType').")],
    ) -> dict[str, Any]:
        """Show a method's exact signature (types, ByRef outputs, defaults, enum values, safety level), or list an
        interface's methods and sub-interfaces, or an enum's values. Reflects the connected ETABS version."""

        def work() -> dict[str, Any]:
            cat = session.catalog
            enum_key = _ci_key(cat.enums, path.strip())
            if enum_key:
                return {"enum": enum_key, "values": cat.enums[enum_key]}
            try:
                return cat.describe(path)
            except ApiNotFoundError as method_error:
                iface = _ci_key(cat.interfaces, path.strip())
                if iface is None:
                    try:
                        resolved_iface = _resolve_interface(cat, path)
                    except ApiNotFoundError:
                        raise method_error from None
                    iface = resolved_iface
                return cat.interface_summary(iface)

        return await session.run(work)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def etabs_call_api(
        path: Annotated[str, Field(description="Method path relative to SapModel ('PointObj.SetRestraint'), or 'App.<method>' for the application object.")],
        args: Annotated[dict[str, Any] | None, Field(description="Arguments by parameter name. Omit ByRef outputs; enums accept names or numbers; arrays are JSON lists.")] = None,
        confirm: Annotated[bool, Field(description="Required for destructive methods (Delete*, New*, OpenFile, unlocking...). Ask the user first.")] = False,
        max_items: Annotated[int | None, Field(description="Cap on returned array lengths.")] = None,
    ) -> dict[str, Any]:
        """Call any ETABS API method. Arguments are validated and converted against the live signature; ByRef
        values come back in 'outputs'. A non-zero 'ret' means ETABS rejected the call (ok=false)."""

        def work() -> dict[str, Any]:
            result = session.call(path, args or {}, confirm=confirm, check=False, max_items=max_items)
            if result.get("ok") is False:
                sig = session.catalog.resolve(path).methods[0]
                result["hint"] = session.failure_hint(sig)
            return result

        return await session.run(work)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def etabs_call_api_batch(
        calls: Annotated[list[ApiCall], Field(min_length=1, max_length=500)],
        stop_on_error: Annotated[bool, Field(description="Stop at the first failed call.")] = True,
        confirm: Annotated[bool, Field(description="Confirms every destructive call in the batch.")] = False,
    ) -> dict[str, Any]:
        """Run several API calls in order in one round trip (for example, define and assign many properties).
        Each result is reported; with stop_on_error the batch halts at the first failure."""

        def work() -> dict[str, Any]:
            results: list[dict[str, Any]] = []
            failed = 0
            for i, c in enumerate(calls):
                try:
                    r = session.call(c.path, c.args, confirm=confirm, check=False)
                except EtabsMcpError as exc:
                    r = {"call": c.path, "ok": False, **exc.to_dict()}
                r["index"] = i
                results.append(r)
                if r.get("ok") is False:
                    failed += 1
                    if stop_on_error:
                        break
            return {"executed": len(results), "failed": failed, "total": len(calls), "results": results}

        return await session.run(work)


def _resolve_interface(cat: Any, path: str) -> str:
    """Resolve a property path such as 'LoadCases.StaticLinear' to its interface name."""
    probe = path.strip().rstrip(".")
    tokens = probe.split(".")
    root = "SapModel"
    if tokens and tokens[0].lower() in ("sapmodel", "app"):
        root = "App" if tokens[0].lower() == "app" else "SapModel"
        tokens = tokens[1:]
    from ..catalog import ROOTS

    iface = ROOTS[root]
    for tok in tokens:
        props = cat.interfaces[iface].properties
        key = _ci_key(props, tok)
        if key is None:
            raise ApiNotFoundError(f"'{tok}' is not a property of {iface}.")
        iface = props[key]
    return iface
