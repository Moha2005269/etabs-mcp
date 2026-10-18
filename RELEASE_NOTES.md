## etabs-mcp v0.3.1

**Now on PyPI and in the MCP Registry.**

```
pip install etabs-mcp
```

Or run it without installing: `uvx etabs-mcp`. For Claude Desktop, download the `.mcpb` matching your Python (`py311`, `py312` or `py313`) below and open it.

Requires Windows and ETABS 22 or newer.

### Changes since v0.3.0
- Published to PyPI with trusted publishing, and listed in the official MCP Registry as `io.github.moha2005269/etabs-mcp`.
- Linux container support for registries such as Glama: a `Dockerfile` and `glama.json`. The server starts and lists its tools on Linux, and connecting explains that ETABS needs Windows. CI builds the image and runs an MCP handshake inside it on every push.

71 tools. See the [v0.3.0 notes](https://github.com/Moha2005269/etabs-mcp/releases/tag/v0.3.0) for the full feature list.
