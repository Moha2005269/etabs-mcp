"""Turn agent-supplied JSON arguments into values that match an ETABS method signature, and turn
ETABS outputs back into JSON. Pure Python: the .NET marshalling lives in dotnet.py."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .catalog import FLOAT_TYPES, NUMERIC_TYPES, ApiCatalog, MethodSig, ParamSig
from .errors import ArgumentValidationError


class _Marker:
    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return self.name


# Optional parameter left out: use the .NET default value.
DEFAULT = _Marker("DEFAULT")
# ByRef parameter left out: pass an empty placeholder; ETABS fills it.
PLACEHOLDER = _Marker("PLACEHOLDER")


def choose_overload(methods: Sequence[MethodSig], args: Any) -> MethodSig:
    if len(methods) == 1:
        return methods[0]
    if isinstance(args, Mapping):
        keys = {k.lower() for k in args}
        fits = [m for m in methods if keys <= {p.name.lower() for p in m.params}]
    elif isinstance(args, (list, tuple)):
        fits = [m for m in methods if len(m.params) >= len(args)]
    else:
        fits = list(methods)
    if not fits:
        raise ArgumentValidationError(
            f"No overload of {methods[0].qualified_name} accepts these arguments.",
            details={"overloads": [m.signature() for m in methods]},
        )
    return min(fits, key=lambda m: len(m.params))


def coerce_arguments(method: MethodSig, args: Any, catalog: ApiCatalog) -> list[Any]:
    """Return one value per parameter: a coerced value, DEFAULT or PLACEHOLDER."""
    params = method.params
    provided: dict[int, Any] = {}
    if args is None:
        args = {}
    if isinstance(args, Mapping):
        index = {p.name.lower(): i for i, p in enumerate(params)}
        unknown = [k for k in args if k.lower() not in index]
        if unknown:
            raise ArgumentValidationError(
                f"Unknown argument(s) {unknown} for {method.qualified_name}.",
                hint=f"Signature: {method.signature()}",
            )
        for k, v in args.items():
            provided[index[k.lower()]] = v
    elif isinstance(args, (list, tuple)):
        if len(args) > len(params):
            raise ArgumentValidationError(
                f"{method.qualified_name} takes {len(params)} argument(s), got {len(args)}.",
                hint=f"Signature: {method.signature()}",
            )
        provided = dict(enumerate(args))
    else:
        raise ArgumentValidationError("'args' must be an object (by parameter name) or an array.")

    values: list[Any] = []
    missing: list[str] = []
    errors: list[str] = []
    for i, p in enumerate(params):
        if i in provided and provided[i] is not None:
            try:
                values.append(coerce_value(provided[i], p, catalog))
            except ArgumentValidationError as exc:
                errors.append(exc.message)
                values.append(None)
        elif p.by_ref:
            values.append(PLACEHOLDER)
        elif p.optional:
            values.append(DEFAULT)
        else:
            missing.append(f"{p.name} ({p.type})")
            values.append(None)
    if missing or errors:
        parts = []
        if missing:
            parts.append(f"missing required argument(s): {', '.join(missing)}")
        parts.extend(errors)
        raise ArgumentValidationError(
            f"{method.qualified_name}: " + "; ".join(parts) + ".",
            hint=f"Signature: {method.signature()}",
        )
    return values


def coerce_value(value: Any, p: ParamSig, catalog: ApiCatalog) -> Any:
    if p.is_array:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ArgumentValidationError(f"{p.name} must be an array of {p.base_type}")
        return [_coerce_scalar(v, p.base_type, f"{p.name}[{i}]", catalog) for i, v in enumerate(value)]
    return _coerce_scalar(value, p.base_type, p.name, catalog)


def _coerce_scalar(value: Any, type_name: str, label: str, catalog: ApiCatalog) -> Any:
    if type_name in catalog.enums:
        return _coerce_enum(value, type_name, label, catalog.enums[type_name])
    if type_name in NUMERIC_TYPES:
        if isinstance(value, bool):
            raise ArgumentValidationError(f"{label} must be an integer, got a boolean")
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                pass
        raise ArgumentValidationError(f"{label} must be an integer, got {value!r}")
    if type_name in FLOAT_TYPES:
        if isinstance(value, bool):
            raise ArgumentValidationError(f"{label} must be a number, got a boolean")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                pass
        raise ArgumentValidationError(f"{label} must be a number, got {value!r}")
    if type_name == "Boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0", "yes", "no"}:
            return value.strip().lower() in {"true", "1", "yes"}
        raise ArgumentValidationError(f"{label} must be true or false, got {value!r}")
    if type_name == "String":
        if isinstance(value, bool):
            return str(value)
        if isinstance(value, (str, int, float)):
            return str(value)
        raise ArgumentValidationError(f"{label} must be a string, got {value!r}")
    raise ArgumentValidationError(
        f"{label} has type {type_name}, which cannot be passed from JSON",
        hint="Use a curated tool for this operation, or a method that takes primitive arguments.",
    )


def _coerce_enum(value: Any, type_name: str, label: str, members: dict[str, int]) -> str:
    if isinstance(value, str):
        text = value.strip()
        if "." in text:  # accept 'eUnits.kN_m_C'
            text = text.split(".")[-1]
        for name in members:
            if name.lower() == text.lower():
                return name
        if text.lstrip("-").isdigit():
            value = int(text)
    if isinstance(value, int) and not isinstance(value, bool):
        for name, num in members.items():
            if num == value:
                return name
    raise ArgumentValidationError(
        f"{label} must be one of {type_name} {sorted(members, key=members.get)} (name or number), got {value!r}"
    )


def jsonable(value: Any, *, max_items: int) -> tuple[Any, bool]:
    """Make an output JSON-safe and cap array lengths. Returns (value, truncated)."""
    truncated = False
    if isinstance(value, float):
        return (None if math.isnan(value) or math.isinf(value) else value), False
    if isinstance(value, (list, tuple)):
        items = list(value)
        if len(items) > max_items:
            items = items[:max_items]
            truncated = True
        out = []
        for v in items:
            jv, t = jsonable(v, max_items=max_items)
            truncated = truncated or t
            out.append(jv)
        return out, truncated
    if isinstance(value, dict):
        out_d = {}
        for k, v in value.items():
            jv, t = jsonable(v, max_items=max_items)
            truncated = truncated or t
            out_d[k] = jv
        return out_d, truncated
    return value, False


def build_result(
    method: MethodSig,
    path: str,
    return_value: Any,
    outputs: dict[str, Any],
    *,
    max_items: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {"call": path}
    if method.returns_status:
        result["ret"] = return_value
        result["ok"] = return_value == 0
    elif method.returns != "Void":
        result["value"], _ = jsonable(return_value, max_items=max_items)
    if outputs:
        result["outputs"], truncated = jsonable(outputs, max_items=max_items)
        if truncated:
            result["truncated"] = True
            result["note"] = f"Arrays were capped at {max_items} items (ETABS_MCP_MAX_ARRAY_ITEMS)."
    return result
