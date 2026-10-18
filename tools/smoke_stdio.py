"""Start an MCP server command over stdio, list its tools and call one: the check registries such as Glama run.

    python tools/smoke_stdio.py etabs-mcp
    python tools/smoke_stdio.py docker run -i --rm etabs-mcp
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(cmd: list[str]) -> None:
    params = StdioServerParameters(command=cmd[0], args=cmd[1:], env=dict(os.environ))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        info = await session.initialize()
        tools = (await session.list_tools()).tools
        print(f"server={info.serverInfo.name} version={info.serverInfo.version} tools={len(tools)}")
        assert len(tools) >= 70, len(tools)
        res = await session.call_tool("etabs_search_api", {"query": "frame section", "limit": 3})
        assert not res.isError, res.content
        res = await session.call_tool("etabs_list_installations", {})
        assert not res.isError, res.content
        if sys.platform != "win32":
            # Off Windows, connecting must fail with a clear explanation (on Windows it would launch ETABS).
            res = await session.call_tool("etabs_connect", {})
            text = " ".join(getattr(c, "text", "") for c in res.content)
            assert res.isError and "Windows" in text, text
        print("smoke test passed")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["etabs-mcp"]))
