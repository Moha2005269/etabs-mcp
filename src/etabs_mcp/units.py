"""Unit-aware length inputs: agents may pass 6, "6 m", "20 ft", "250mm" or "12'6\"" wherever a curated tool
takes a length; values are converted to the model's present length unit."""

from __future__ import annotations

import re

from .errors import ArgumentValidationError

TO_METRES = {
    "m": 1.0, "meter": 1.0, "meters": 1.0, "metre": 1.0, "metres": 1.0,
    "cm": 0.01, "mm": 0.001,
    "ft": 0.3048, "feet": 0.3048, "foot": 0.3048, "'": 0.3048,
    "in": 0.0254, "inch": 0.0254, "inches": 0.0254, '"': 0.0254,
}
# Length unit of each ETABS eUnits value (force_length_temperature).
_UNIT_RE = re.compile(r"^[A-Za-z]+_(?P<len>[a-z]+)_[CF]$")
_QTY_RE = re.compile(r"^\s*(?P<num>[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?)\s*(?P<unit>[A-Za-z'\"]*)\s*$")
_FT_IN_RE = re.compile(r"^\s*(?P<ft>[-+]?\d+(\.\d+)?)\s*'\s*(?P<inch>\d+(\.\d+)?)\s*\"?\s*$")

Length = float | str


def length_unit(present_units: str | None) -> str:
    """'kN_m_C' -> 'm', 'kip_ft_F' -> 'ft'. Defaults to metres when unknown."""
    if present_units:
        m = _UNIT_RE.match(present_units)
        if m and m.group("len") in TO_METRES:
            return m.group("len")
    return "m"


def to_length(value: Length, target: str, label: str = "length") -> float:
    """Convert a number (already in target units) or a quantity string to the target length unit."""
    if isinstance(value, bool):
        raise ArgumentValidationError(f"{label} must be a length, got a boolean")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    m = _FT_IN_RE.match(text)
    if m:
        sign = -1.0 if text.startswith("-") else 1.0
        metres = sign * (abs(float(m.group("ft"))) * 0.3048 + float(m.group("inch")) * 0.0254)
        return metres / TO_METRES[target]
    m = _QTY_RE.match(text)
    if not m:
        raise ArgumentValidationError(
            f"{label}: cannot read {value!r} as a length.", hint="Use a number in present units or e.g. '6 m', '20 ft', '250 mm'."
        )
    unit = m.group("unit").lower() or target
    if unit not in TO_METRES:
        raise ArgumentValidationError(f"{label}: unknown length unit {m.group('unit')!r}.",
                                      hint=f"Known units: {', '.join(sorted({'m', 'cm', 'mm', 'ft', 'in'}))}.")
    return float(m.group("num")) * TO_METRES[unit] / TO_METRES[target]


class LengthConverter:
    """Bound to a session's present units for the duration of one tool call."""

    def __init__(self, present_units: str | None):
        self.unit = length_unit(present_units)

    def __call__(self, value: Length | None, label: str = "length") -> float | None:
        return None if value is None else to_length(value, self.unit, label)

    def point(self, xyz: list[Length], label: str = "point") -> list[float]:
        return [to_length(v, self.unit, f"{label}[{i}]") for i, v in enumerate(xyz)]
