"""Safety policy for API calls: read-only mode, blocked internals, and confirmation for destructive calls."""

from __future__ import annotations

from typing import Literal

from .catalog import MethodSig
from .errors import ConfirmationRequired, PolicyError

Safety = Literal["read", "write", "destructive", "blocked"]

READ_PREFIXES = ("Get", "Count")
# Read-only calls whose names do not start with Get/Count.
READ_EXTRA = {"cOAPI.Visible"}
# Undocumented or process-level hooks that agents should never reach through the generic dispatcher.
BLOCKED = {"cOAPI.InternalExec", "cOAPI.SetAsActiveObject", "cOAPI.UnsetAsActiveObject"}
# Calls that discard model data, results or the session. Delete*/Clear* are added by prefix.
DESTRUCTIVE = {
    "cOAPI.ApplicationExit",
    "cSapModel.InitializeNewModel",
    "cSapModel.SetModelIsLocked",  # unlocking deletes all analysis results
    "cFile.NewBlank",
    "cFile.NewGridOnly",
    "cFile.NewSteelDeck",
    "cFile.OpenFile",
    "cAnalyze.MergeAnalysisResults",
    "cAnalyze.ModifyUndeformedGeometry",
    "cAnalyze.ModifyUndeformedGeometryModeShape",
    "cDesignSteel.ResetOverwrites",
    "cDesignConcrete.ResetOverwrites",
    "cDesignCompositeBeam.ResetOverwrites",
    "cDesignCompositeColumn.ResetOverwrites",
    "cDesignConcreteShell.ResetOverwrites",
}
DESTRUCTIVE_PREFIXES = ("Delete", "Clear")


def classify(sig: MethodSig) -> Safety:
    q = sig.qualified_name
    if q in BLOCKED or sig.interface in {"cHelper", "cPluginCallback", "cPluginContract"}:
        return "blocked"
    if q in READ_EXTRA or sig.name.startswith(READ_PREFIXES):
        return "read"
    if q in DESTRUCTIVE or sig.name.startswith(DESTRUCTIVE_PREFIXES):
        return "destructive"
    return "write"


def check(sig: MethodSig, *, read_only: bool, allow_destructive: bool, confirm: bool) -> Safety:
    level = classify(sig)
    if level == "blocked":
        raise PolicyError(f"{sig.qualified_name} is blocked: it is an internal or process-level hook.")
    if read_only and level != "read":
        raise PolicyError(
            f"{sig.qualified_name} modifies the model, but the server runs in read-only mode.",
            hint="Restart the server without ETABS_MCP_READ_ONLY=1 to allow edits.",
        )
    if level == "destructive" and not (confirm or allow_destructive):
        raise ConfirmationRequired(
            f"{sig.qualified_name} is destructive (it can discard model data, results or the session).",
            hint="Confirm with the user, then repeat the call with confirm=true.",
        )
    return level
