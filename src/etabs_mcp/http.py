"""HTTP transports with bearer-token authentication.

stdio needs no auth (the client launches the server). Over HTTP, anyone who can reach the port could drive
ETABS, so a token is required whenever the server listens beyond localhost.
"""

from __future__ import annotations

import hmac
import json
from typing import Any

LOCALHOST = {"127.0.0.1", "localhost", "::1"}


class BearerAuth:
    """ASGI middleware: every HTTP request must carry `Authorization: Bearer <token>`."""

    def __init__(self, app: Any, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"")
        supplied = auth[7:] if auth[:7].lower() == b"bearer " else b""
        if not supplied or not hmac.compare_digest(supplied, self.token):
            body = json.dumps({"error": "unauthorized", "message": "Missing or invalid bearer token."}).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def build_http_app(mcp: Any, transport: str, token: str | None) -> Any:
    app = mcp.streamable_http_app() if transport == "streamable-http" else mcp.sse_app()
    return BearerAuth(app, token) if token else app


def check_exposure(host: str, token: str | None) -> None:
    if host not in LOCALHOST and not token:
        raise SystemExit(
            f"Refusing to listen on {host} without a token: anyone on the network could drive ETABS.\n"
            "Set ETABS_MCP_TOKEN (or --token), or bind to 127.0.0.1."
        )


def serve(mcp: Any, transport: str, host: str, port: int, token: str | None) -> None:
    import uvicorn

    check_exposure(host, token)
    uvicorn.run(build_http_app(mcp, transport, token), host=host, port=port, log_level="warning")
