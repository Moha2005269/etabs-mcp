"""Error types. Every error carries a stable code and, where possible, a hint telling the agent what to do next."""

from __future__ import annotations

from typing import Any


class EtabsMcpError(Exception):
    code = "etabs_error"

    def __init__(self, message: str, *, hint: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        if self.details:
            out["details"] = self.details
        return out

    def __str__(self) -> str:
        text = f"[{self.code}] {self.message}"
        if self.hint:
            text += f"\nHint: {self.hint}"
        return text


class FatalError(EtabsMcpError):
    """Errors that must abort a whole tool call, never be collected as per-item batch errors."""


class InstallNotFoundError(FatalError):
    code = "install_not_found"


class RuntimeLoadError(FatalError):
    code = "runtime_error"


class NotConnectedError(FatalError):
    code = "not_connected"


class ConnectionLostError(FatalError):
    code = "connection_lost"


class ApiNotFoundError(EtabsMcpError):
    code = "api_not_found"


class ArgumentValidationError(EtabsMcpError):
    code = "invalid_arguments"


class EtabsCallFailed(EtabsMcpError):
    """ETABS returned a non-zero status code, or the call raised inside ETABS."""

    code = "etabs_call_failed"

    def __init__(self, message: str, *, path: str, ret: int | None = None, **kwargs: Any):
        super().__init__(message, **kwargs)
        self.path = path
        self.ret = ret
        self.details.setdefault("call", path)
        if ret is not None:
            self.details.setdefault("ret", ret)


class PolicyError(FatalError):
    code = "blocked_by_policy"


class ConfirmationRequired(FatalError):
    code = "confirmation_required"


# Errors a batch tool may record per item and continue past.
ITEM_ERRORS = (ArgumentValidationError, EtabsCallFailed, ApiNotFoundError)
