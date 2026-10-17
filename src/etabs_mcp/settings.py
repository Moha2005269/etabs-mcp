"""Server configuration, read from environment variables and overridable from the command line."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ConnectMode = Literal["auto", "attach", "start"]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else None


@dataclass
class Settings:
    # Which ETABS to use. Directory wins over version; both empty means "newest installed / the running one".
    install_dir: Path | None = None
    version: int | None = None
    # How the first tool call connects when the agent did not call etabs_connect explicitly.
    connect_mode: ConnectMode = "auto"
    visible: bool = True
    # Safety.
    read_only: bool = False
    allow_destructive: bool = False
    # .NET host for pythonnet: "coreclr" (.NET 8, ETABS 22+) or "netfx" (.NET Framework 4.8).
    runtime: str = "coreclr"
    # Output shaping.
    max_array_items: int = 2000
    max_table_rows: int = 500
    extra_search_dirs: list[Path] = field(default_factory=list)
    # Production: JSON-lines log of every call that changes the model, and a bearer token for HTTP transports.
    audit_log: Path | None = None
    http_token: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        install = os.environ.get("ETABS_MCP_INSTALL_DIR", "").strip()
        mode = os.environ.get("ETABS_MCP_CONNECT_MODE", "auto").strip().lower() or "auto"
        if mode not in ("auto", "attach", "start"):
            raise ValueError(f"ETABS_MCP_CONNECT_MODE must be auto, attach or start (got {mode!r})")
        return cls(
            install_dir=Path(install) if install else None,
            version=_env_int("ETABS_MCP_VERSION"),
            connect_mode=mode,  # type: ignore[arg-type]
            visible=_env_bool("ETABS_MCP_VISIBLE", True),
            read_only=_env_bool("ETABS_MCP_READ_ONLY", False),
            allow_destructive=_env_bool("ETABS_MCP_ALLOW_DESTRUCTIVE", False),
            runtime=os.environ.get("ETABS_MCP_RUNTIME", "coreclr").strip().lower() or "coreclr",
            max_array_items=_env_int("ETABS_MCP_MAX_ARRAY_ITEMS") or 2000,
            max_table_rows=_env_int("ETABS_MCP_MAX_TABLE_ROWS") or 500,
            audit_log=Path(os.environ["ETABS_MCP_AUDIT_LOG"]) if os.environ.get("ETABS_MCP_AUDIT_LOG") else None,
            http_token=os.environ.get("ETABS_MCP_TOKEN") or None,
        )
