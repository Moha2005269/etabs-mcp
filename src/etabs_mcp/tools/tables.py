"""Interactive database tools: ETABS's tables expose nearly every model definition, assignment and result,
and importable tables can be edited and applied, the same way an engineer uses Edit > Interactive Database."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from ..errors import ITEM_ERRORS, ArgumentValidationError, EtabsCallFailed
from ..session import EtabsSession
from .common import READ, WRITE, names_list

IMPORT_TYPES = {0: "not importable", 1: "importable (not interactive)", 2: "importable", 3: "importable, interactive"}


class RowUpdate(BaseModel):
    match: dict[str, Any] = Field(description="Field values that identify the rows, e.g. {'Name': 'B1'}.")
    set: dict[str, Any] = Field(description="Field values to write on matching rows.")


# Columns that hold identifiers even when every value looks numeric (object names/labels such as "1").
_IDENTITY_TOKENS = ("name", "label", "story", "guid", "case", "combo", "type", "joint", "point", "frame", "area",
                    "link", "pier", "spandrel", "section", "material", "group", "element", "object", "direction",
                    "prop", "pattern", "diaphragm", "item", "color", "notes", "design")


def numeric_fields(fields: list[str], rows: list[dict[str, Any]], units: dict[str, str]) -> set[str]:
    """Fields to convert to numbers: those ETABS gives units for, plus unitless measures (drift ratios, mass
    ratios, periods...) whose every value parses as a number and whose name is not an identifier."""
    out = set(units)
    for f in fields:
        if f in out or any(tok in f.lower() for tok in _IDENTITY_TOKENS):
            continue
        values = [r.get(f) for r in rows if r.get(f) not in (None, "")]
        if values and all(isinstance(_to_number(v), float) for v in values):
            out.add(f)
    return out


def _to_number(text: str) -> float | str:
    try:
        return float(text)
    except (TypeError, ValueError):
        return text


def _equal(a: Any, b: Any) -> bool:
    sa, sb = str(a), str(b)
    if sa == sb:
        return True
    try:
        return abs(float(sa) - float(sb)) <= 1e-9 * max(1.0, abs(float(sb)))
    except ValueError:
        return sa.lower() == sb.lower()


def _rows(fields: list[str], flat: list[str], count: int) -> list[dict[str, str]]:
    n = len(fields)
    if n == 0:
        return []
    count = min(count, len(flat) // n)
    return [dict(zip(fields, flat[i * n:(i + 1) * n])) for i in range(count)]


def field_units(session: EtabsSession, table_key: str) -> dict[str, str]:
    try:
        o = session.out("DatabaseTables.GetAllFieldsInTable", TableKey=table_key)
    except ITEM_ERRORS:
        return {}
    return {k: u for k, u in zip(o.get("FieldKey") or [], o.get("UnitsString") or []) if u}


def select_cases_for_display(session: EtabsSession, cases: list[str] | None, combos: list[str] | None) -> dict[str, list[str]]:
    """Choose which load cases/combinations result tables show. None for both means every case and combo."""
    if cases is None and combos is None:
        cases = names_list(session, "LoadCases.GetNameList")
        combos = names_list(session, "RespCombo.GetNameList")
    cases, combos = cases or [], combos or []
    session.call("DatabaseTables.SetLoadCasesSelectedForDisplay", {"LoadCaseList": cases}, _internal=True)
    session.call("DatabaseTables.SetLoadCombinationsSelectedForDisplay", {"LoadCombinationList": combos}, _internal=True)
    return {"load_cases": cases, "load_combinations": combos}


def read_table(
    session: EtabsSession,
    table_key: str,
    *,
    fields: list[str] | None = None,
    group: str = "",
    where: dict[str, Any] | None = None,
    offset: int = 0,
    limit: int | None = None,
    numeric: bool = True,
) -> dict[str, Any]:
    try:
        o = session.out(
            "DatabaseTables.GetTableForDisplayArray",
            TableKey=table_key, FieldKeyList=fields or [], GroupName=group,
        )
    except EtabsCallFailed as exc:
        raise EtabsCallFailed(
            f"Table '{table_key}' could not be read.",
            path=exc.path, ret=exc.ret,
            hint="Check the key with etabs_list_tables. Result tables exist only after analysis (and design) has "
                 "run, and show only the load cases/combinations selected for display.",
        ) from exc
    keys = list(o.get("FieldsKeysIncluded") or [])
    rows = _rows(keys, list(o.get("TableData") or []), int(o.get("NumberRecords") or 0))
    units = field_units(session, table_key)
    if numeric:
        for k in numeric_fields(keys, rows, units):
            for r in rows:
                if r.get(k) not in (None, ""):
                    r[k] = _to_number(r[k])
    if where:
        rows = [r for r in rows if all(_equal(r.get(k), v) for k, v in where.items())]
    total = len(rows)
    lim = limit if limit is not None else session.settings.max_table_rows
    page = rows[offset: offset + lim]
    result = {
        "table": table_key,
        "fields": keys,
        "units": {k: v for k, v in units.items() if k in keys},
        "total_rows": total,
        "offset": offset,
        "returned": len(page),
        "rows": page,
    }
    if offset + len(page) < total:
        result["next_offset"] = offset + len(page)
    return result


def register(mcp: FastMCP, session: EtabsSession) -> None:
    @mcp.tool(annotations=READ)
    async def etabs_list_tables(
        contains: Annotated[str | None, Field(description="Case-insensitive filter on the table key, e.g. 'drift', 'frame assign', 'material'.")] = None,
        include_empty: Annotated[bool, Field(description="Include tables with no data in the current model.")] = False,
    ) -> dict[str, Any]:
        """List ETABS database tables (definitions, assignments, analysis and design results), with whether each
        has data and whether it can be edited. Result tables appear after analysis/design."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            o = session.out("DatabaseTables.GetAllTables")
            rows = []
            for key, name, imp, empty in zip(o.get("TableKey") or [], o.get("TableName") or [],
                                             o.get("ImportType") or [], o.get("IsEmpty") or []):
                if empty and not include_empty:
                    continue
                if contains and contains.lower() not in key.lower() and contains.lower() not in name.lower():
                    continue
                rows.append({"key": key, "editable": imp in (2, 3), "import_type": IMPORT_TYPES.get(imp, imp),
                             "has_data": not empty})
            return {"count": len(rows), "tables": rows}

        return await session.run(work)

    @mcp.tool(annotations=READ)
    async def etabs_get_table(
        table_key: Annotated[str, Field(description="Exact table key from etabs_list_tables, e.g. 'Story Drifts' or 'Frame Assignments - Section Properties'.")],
        fields: Annotated[list[str] | None, Field(description="Only these field keys (default: all).")] = None,
        load_cases: Annotated[list[str] | None, Field(description="Result tables: load cases to show. Omit both lists for all cases and combinations.")] = None,
        load_combinations: list[str] | None = None,
        group: Annotated[str, Field(description="Only objects in this group ('' = all).")] = "",
        where: Annotated[dict[str, Any] | None, Field(description="Keep rows whose fields equal these values, e.g. {'Story': 'Story3'}.")] = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int | None, Field(ge=1, le=20000)] = None,
        numeric: Annotated[bool, Field(description="Convert fields that carry units to numbers.")] = True,
    ) -> dict[str, Any]:
        """Read any ETABS database table as rows (Display > Show Tables). Values with units are in present units."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            shown = select_cases_for_display(session, load_cases, load_combinations)
            result = read_table(session, table_key, fields=fields, group=group, where=where,
                                offset=offset, limit=limit, numeric=numeric)
            result["cases_shown"] = shown
            return result

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_edit_table(
        table_key: Annotated[str, Field(description="An editable table key (etabs_list_tables shows editable=true).")],
        updates: Annotated[list[RowUpdate] | None, Field(description="Change fields on rows matching 'match'.")] = None,
        append_rows: Annotated[list[dict[str, Any]] | None, Field(description="New rows (field key -> value); missing fields are left blank.")] = None,
        delete_where: Annotated[list[dict[str, Any]] | None, Field(description="Remove rows matching any of these field filters.")] = None,
        replace_rows: Annotated[list[dict[str, Any]] | None, Field(description="Replace the entire table with these rows.")] = None,
        apply: Annotated[bool, Field(description="Apply the edits to the model. False validates and cancels (dry run).")] = True,
        group: str = "",
    ) -> dict[str, Any]:
        """Edit a model table the way the Interactive Database editor does: read it, change/append/delete rows,
        then apply. Returns ETABS's import log with error and warning counts."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            if not any((updates, append_rows, delete_where, replace_rows is not None)):
                raise ArgumentValidationError("Nothing to do: pass updates, append_rows, delete_where or replace_rows.")
            o = session.out("DatabaseTables.GetTableForEditingArray", TableKey=table_key, GroupName=group)
            fields = list(o.get("FieldsKeysIncluded") or [])
            rows = _rows(fields, list(o.get("TableData") or []), int(o.get("NumberRecords") or 0))
            before = len(rows)
            changed = 0
            if replace_rows is not None:
                rows = [dict(r) for r in replace_rows]
            if delete_where:
                rows = [r for r in rows if not any(all(_equal(r.get(k), v) for k, v in f.items()) for f in delete_where)]
            for u in updates or []:
                hit = False
                for r in rows:
                    if all(_equal(r.get(k), v) for k, v in u.match.items()):
                        r.update({k: v for k, v in u.set.items()})
                        hit = True
                        changed += 1
                if not hit:
                    raise ArgumentValidationError(f"No row matches {u.match} in '{table_key}'.")
            rows.extend(dict(r) for r in append_rows or [])
            unknown = sorted({k for r in rows for k in r if k not in fields})
            if unknown:
                raise ArgumentValidationError(
                    f"Unknown field(s) {unknown} for table '{table_key}'.", hint=f"Fields: {fields}"
                )
            flat = ["" if r.get(f) is None else str(r.get(f, "")) for r in rows for f in fields]
            session.call(
                "DatabaseTables.SetTableForEditingArray",
                {"TableKey": table_key, "TableVersion": o.get("TableVersion", 0), "FieldsKeysIncluded": fields,
                 "NumberRecords": len(rows), "TableData": flat},
                _internal=True,
            )
            summary = {"table": table_key, "rows_before": before, "rows_after": len(rows), "rows_updated": changed}
            if not apply:
                session.call("DatabaseTables.CancelTableEditing", _internal=True)
                return {**summary, "applied": False, "note": "Dry run: edits were staged and cancelled."}
            log = session.out("DatabaseTables.ApplyEditedTables", FillImportLog=True)
            fatal, errs = int(log.get("NumFatalErrors") or 0), int(log.get("NumErrorMsgs") or 0)
            return {
                **summary,
                "applied": fatal == 0,
                "fatal_errors": fatal,
                "errors": errs,
                "warnings": int(log.get("NumWarnMsgs") or 0),
                "import_log": (log.get("ImportLog") or "")[-6000:],
            }

        return await session.run(work)
