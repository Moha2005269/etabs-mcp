"""Command line entry point: `etabs-mcp` (stdio by default)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .settings import Settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="etabs-mcp", description="MCP server for CSI ETABS (v22+).")
    parser.add_argument("--version", action="version", version=f"etabs-mcp {__version__}")
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP transports only.")
    parser.add_argument("--port", type=int, default=8765, help="HTTP transports only.")
    parser.add_argument("--etabs-version", type=int, help="ETABS major version to use (env ETABS_MCP_VERSION).")
    parser.add_argument("--install-dir", help="ETABS program folder (env ETABS_MCP_INSTALL_DIR).")
    parser.add_argument("--connect-mode", choices=["auto", "attach", "start"], help="env ETABS_MCP_CONNECT_MODE.")
    parser.add_argument("--hidden", action="store_true", help="Launch ETABS without showing its window.")
    parser.add_argument("--read-only", action="store_true", help="Refuse every call that modifies the model.")
    parser.add_argument("--allow-destructive", action="store_true", help="Skip confirm=true for destructive calls.")
    parser.add_argument("--list-installs", action="store_true", help="Print detected ETABS installs and exit.")
    parser.add_argument("--token", help="Bearer token required on HTTP transports (env ETABS_MCP_TOKEN).")
    parser.add_argument("--audit-log", help="Append every model-changing call to this JSON-lines file (env ETABS_MCP_AUDIT_LOG).")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)

    # stdout carries the MCP protocol on stdio, so logs go to stderr.
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = Settings.from_env()
    if args.etabs_version:
        settings.version = args.etabs_version
    if args.install_dir:
        settings.install_dir = Path(args.install_dir)
    if args.connect_mode:
        settings.connect_mode = args.connect_mode
    if args.hidden:
        settings.visible = False
    if args.read_only:
        settings.read_only = True
    if args.allow_destructive:
        settings.allow_destructive = True
    if args.token:
        settings.http_token = args.token
    if args.audit_log:
        settings.audit_log = Path(args.audit_log)

    if args.list_installs:
        from .discovery import find_installations, find_running_etabs

        print(json.dumps({
            "installations": [i.to_dict() for i in find_installations()],
            "running": [r.to_dict() for r in find_running_etabs()],
        }, indent=2))
        return

    from .server import create_server

    mcp = create_server(settings)
    try:
        if args.transport == "stdio":
            mcp.run(transport="stdio")
        else:
            from .http import serve

            serve(mcp, args.transport, args.host, args.port, settings.http_token)
    finally:
        mcp._etabs_session.shutdown()  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()
