"""Tools that give agents a better view of the model: offscreen rendering, engineering queries, change
tracking, checkpoints, full-model export, design reports and label assignment."""

from __future__ import annotations

import math
import shutil
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP, Image
from pydantic import BaseModel, Field

from ..errors import ITEM_ERRORS, ArgumentValidationError, EtabsCallFailed
from ..render import Member, ModelData, RenderOptions, render
from ..session import EtabsSession
from ..units import Length, LengthConverter, length_unit
from .analysis import case_status, design_summary
from .common import (
    DESTRUCTIVE,
    OBJ_INTERFACE,
    READ,
    WRITE,
    batch_item_error,
    model_file,
    names_list,
    require_confirm,
    targets,
)
from .geometry import _areas, _frames, _points
from .tables import read_table, select_cases_for_display

CHECKPOINT_DIR = ".etabs-mcp-checkpoints"


class Box(BaseModel):
    xmin: Length | None = None
    xmax: Length | None = None
    ymin: Length | None = None
    ymax: Length | None = None
    zmin: Length | None = None
    zmax: Length | None = None


# --------------------------------------------------------------------------- shared model collection

def frame_role(f: dict[str, Any]) -> str:
    (x1, y1, z1), (x2, y2, z2) = f["start"], f["end"]
    if abs(z1 - z2) < 1e-6:
        return "beam"
    if abs(x1 - x2) < 1e-6 and abs(y1 - y2) < 1e-6:
        return "column"
    return "brace"


def design_ratios(session: EtabsSession, errors: dict[str, str] | None = None) -> dict[str, float]:
    """Governing D/C per frame from steel design, plus concrete column PMM ratios where available.

    Members ETABS could not check (design errors, e.g. sections not seismically compact under AISC 341) report a
    ratio of 0; their messages are collected into `errors` so callers never mistake them for lightly loaded."""
    out: dict[str, float] = {}
    if not session.value("GetModelIsLocked"):
        return out
    steel = design_summary(session, "steel", 10 ** 6)
    for m in steel.get("members", []):
        if m["ratio"] is not None:
            out[m["frame"]] = max(out.get(m["frame"], 0.0), m["ratio"])
        if m.get("error") and errors is not None:
            errors[m["frame"]] = m["error"]
    conc = design_summary(session, "concrete", 10 ** 6)
    for r in (conc.get("columns") or {}).get("rows", []) if isinstance(conc.get("columns"), dict) else []:
        if r.get("pmm_ratio") is not None:
            out[r["frame"]] = max(out.get(r["frame"], 0.0), r["pmm_ratio"])
    return out


def joint_displacements(session: EtabsSession, case: str) -> dict[str, tuple[float, float, float]]:
    """{point name: (Ux, Uy, Uz)} for one load case or combination (envelopes take the max magnitude)."""
    if not session.value("GetModelIsLocked"):
        raise ArgumentValidationError("Deformed shapes need analysis results.", hint="Run etabs_run_analysis first.")
    combos = set(names_list(session, "RespCombo.GetNameList"))
    select_cases_for_display(session, [] if case in combos else [case], [case] if case in combos else [])
    res = read_table(session, "Joint Displacements", limit=10 ** 7)
    out: dict[str, tuple[float, float, float]] = {}
    for r in res["rows"]:
        if r.get("OutputCase") != case:
            continue
        v = (float(r.get("Ux") or 0), float(r.get("Uy") or 0), float(r.get("Uz") or 0))
        prev = out.get(r["UniqueName"])
        if prev is None or sum(c * c for c in v) > sum(c * c for c in prev):
            out[r["UniqueName"]] = v
    if not out:
        raise ArgumentValidationError(f"No joint displacements for '{case}'.",
                                      hint="Check the case/combination name and that it was analysed.")
    return out


# --------------------------------------------------------------------------- snapshots for change tracking

def snapshot(session: EtabsSession) -> dict[str, Any]:
    snap: dict[str, Any] = {"taken": time.time()}
    snap["frames"] = {f["name"]: (f["section"], tuple(map(_r, f["start"])), tuple(map(_r, f["end"])))
                      for f in _frames(session)}
    areas = {}
    for a in _areas(session):
        prop = None
        try:
            prop = session.out("AreaObj.GetProperty", Name=a["name"]).get("PropName")
        except ITEM_ERRORS:
            pass
        areas[a["name"]] = (prop, tuple(tuple(map(_r, c)) for c in a["coordinates"]))
    snap["areas"] = areas
    snap["points"] = {p["name"]: (_r(p["x"]), _r(p["y"]), _r(p["z"])) for p in _points(session)}
    for key, path in (("load_patterns", "LoadPatterns.GetNameList"), ("load_cases", "LoadCases.GetNameList"),
                      ("load_combinations", "RespCombo.GetNameList"), ("materials", "PropMaterial.GetNameList"),
                      ("frame_sections", "PropFrame.GetNameList"), ("area_sections", "PropArea.GetNameList"),
                      ("groups", "GroupDef.GetNameList"), ("stories", "Story.GetNameList")):
        try:
            snap[key] = sorted(names_list(session, path))
        except ITEM_ERRORS:
            snap[key] = []
    snap["units"] = session.value("GetPresentUnits")
    snap["locked"] = session.value("GetModelIsLocked")
    snap["file"] = model_file(session)
    return snap


def _r(v: float) -> float:
    return round(float(v), 4)


def diff_snapshots(old: dict[str, Any], new: dict[str, Any], limit: int) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for kind in ("frames", "areas", "points"):
        a, b = old.get(kind, {}), new.get(kind, {})
        added = sorted(set(b) - set(a))
        removed = sorted(set(a) - set(b))
        modified = []
        for n in sorted(set(a) & set(b)):
            if a[n] != b[n]:
                entry: dict[str, Any] = {"name": n}
                if kind in ("frames", "areas") and a[n][0] != b[n][0]:
                    entry["property"] = {"from": a[n][0], "to": b[n][0]}
                if kind == "points" or a[n][1:] != b[n][1:]:
                    entry["geometry_changed"] = True
                modified.append(entry)
        if added or removed or modified:
            changes[kind] = {"added": added[:limit], "removed": removed[:limit], "modified": modified[:limit],
                             "counts": {"added": len(added), "removed": len(removed), "modified": len(modified)}}
    for key in ("load_patterns", "load_cases", "load_combinations", "materials", "frame_sections",
                "area_sections", "groups", "stories"):
        a, b = set(old.get(key, [])), set(new.get(key, []))
        if a != b:
            changes[key] = {"added": sorted(b - a), "removed": sorted(a - b)}
    for key in ("units", "locked", "file"):
        if old.get(key) != new.get(key):
            changes[key] = {"from": old.get(key), "to": new.get(key)}
    return changes


# --------------------------------------------------------------------------- register

def register(mcp: FastMCP, session: EtabsSession) -> None:
    state: dict[str, Any] = {}

    @mcp.tool(annotations=READ)
    async def etabs_render_view(
        color_by: Annotated[
            Literal["none", "design_ratio", "section", "story", "displacement"],
            Field(description="design_ratio needs a design run; displacement needs deformed_case."),
        ] = "none",
        deformed_case: Annotated[str | None, Field(description="Load case or combination whose deflected shape to draw.")] = None,
        deformed_scale: Annotated[float | None, Field(description="Displacement multiplier; omit for automatic (largest deflection ≈ 4% of the model height).")] = None,
        view: Literal["iso", "iso_back", "front", "side", "top"] = "iso",
        azimuth: Annotated[float | None, Field(description="Override the camera azimuth in degrees.")] = None,
        elevation: Annotated[float | None, Field(description="Override the camera elevation in degrees.")] = None,
        stories: Annotated[list[str] | None, Field(description="Only draw these stories.")] = None,
        highlight: Annotated[list[str] | None, Field(description="Frame names to emphasise (e.g. from etabs_find_objects).")] = None,
        theme: Literal["dark", "light"] = "dark",
        show_slabs: bool = True,
        width: Annotated[int, Field(ge=400, le=2400)] = 1400,
        height: Annotated[int, Field(ge=300, le=1600)] = 900,
    ) -> list:
        """Render the model offscreen from its data: any angle, deformed shape of any case, members coloured by
        design ratio, displacement, section or storey. Works with ETABS hidden and never takes focus. Use it to
        look at what you built and at results the ETABS window cannot be switched to through the API."""

        def work() -> list:
            session.ensure_connected()
            frames = _frames(session)
            story_of = {}
            for f in frames:
                story_of[f["name"]] = f.get("story") or ""
            design_errors: dict[str, str] = {}
            ratios = design_ratios(session, design_errors) if color_by == "design_ratio" else {}
            if color_by == "design_ratio" and not ratios:
                raise ArgumentValidationError("No design ratios available.", hint="Run etabs_run_design first.")
            pts = {p["name"]: (p["x"], p["y"], p["z"]) for p in _points(session)}
            disp: dict[tuple, tuple] = {}
            if deformed_case or color_by == "displacement":
                if not deformed_case:
                    raise ArgumentValidationError("color_by='displacement' needs deformed_case.")
                for name, v in joint_displacements(session, deformed_case).items():
                    if name in pts:
                        disp[tuple(round(c, 3) for c in pts[name])] = v
            members = [Member(f["name"], tuple(f["start"]), tuple(f["end"]), f["section"], f["story"],
                              # A design error is a failed check: draw it in the over-capacity colour.
                              1.05 if f["name"] in design_errors else ratios.get(f["name"])) for f in frames
                       if not stories or f["story"] in stories]
            slabs = [[tuple(c) for c in a["coordinates"]] for a in _areas(session)]
            if stories:
                keep_z = {round(max(m.start[2], m.end[2]), 3) for m in members}
                slabs = [s for s in slabs if round(s[0][2], 3) in keep_z]
            scale = 0.0
            if deformed_case:
                peak = max((math.sqrt(sum(c * c for c in v)) for v in disp.values()), default=0.0)
                zs = [p[2] for p in pts.values()] or [1.0]
                height_m = max(max(zs) - min(zs), 1e-6)
                scale = deformed_scale if deformed_scale is not None else (0.04 * height_m / peak if peak else 0.0)
            title_bits = {"design_ratio": "Design ratios", "section": "Sections", "story": "Stories",
                          "displacement": "Displacement", "none": "Model"}
            title = title_bits[color_by] + (f" · deformed {deformed_case}" if deformed_case else "")
            sub = []
            if ratios:
                worst = max(ratios.values())
                sub.append(f"max D/C {worst:.2f} · {sum(1 for r in ratios.values() if r > 1)} over capacity")
            if design_errors:
                sub.append(f"{len(design_errors)} with design errors (shown as failing)")
            if disp:
                peak = max(math.sqrt(sum(c * c for c in v)) for v in disp.values())
                sub.append(f"max displacement {peak:.4g} {length_unit(session.value('GetPresentUnits'))}")
            png, meta = render(ModelData(members, slabs, disp), RenderOptions(
                color_by=color_by, view=view, azimuth=azimuth, elevation=elevation, deformed_scale=scale,
                highlight=set(highlight or []), width=width, height=height, theme=theme, title=title,
                subtitle="  ·  ".join(sub), show_slabs=show_slabs))
            summary = {**meta, "deformed_scale": scale, "color_by": color_by}
            if ratios:
                summary["max_ratio"] = round(max(ratios.values()), 4)
            if design_errors:
                summary["design_errors"] = len(design_errors)
                summary["example_error"] = next(iter(design_errors.values()))
            return [Image(data=png, format="png"), str(summary)]

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_find_objects(
        kind: Literal["frame", "area", "point"] = "frame",
        role: Annotated[Literal["column", "beam", "brace"] | None, Field(description="Frames only, from geometry.")] = None,
        stories: list[str] | None = None,
        sections: Annotated[list[str] | None, Field(description="Frame sections or area properties.")] = None,
        group: Annotated[str | None, Field(description="Only objects assigned to this group.")] = None,
        min_ratio: Annotated[float | None, Field(description="Frames whose design D/C ≥ this (needs design results).")] = None,
        max_ratio: float | None = None,
        min_length: Annotated[Length | None, Field(description="Frames at least this long (number in present units or e.g. '8 m').")] = None,
        max_length: Length | None = None,
        box: Annotated[Box | None, Field(description="Only objects whose centroid lies inside this box.")] = None,
        select: Annotated[bool, Field(description="Also select the matches in ETABS (clears the current selection).")] = False,
        limit: Annotated[int, Field(ge=1, le=10000)] = 500,
    ) -> dict[str, Any]:
        """Find objects by engineering criteria: 'columns on Story3', 'beams longer than 8 m', 'members with
        D/C over 0.9', 'everything in this box'. Returns names usable by every other tool; can select them."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            L = LengthConverter(session.value("GetPresentUnits"))
            in_group: set[str] | None = None
            if group:
                o = session.out("GroupDef.GetAssignments", Name=group)
                code = {"frame": 2, "area": 5, "point": 1}[kind]
                in_group = {n for t, n in zip(o.get("ObjectType") or [], o.get("ObjectName") or []) if t == code}
            if kind == "frame":
                rows = _frames(session)
                for r in rows:
                    r["role"] = frame_role(r)
                    r["length"] = math.dist(r["start"], r["end"])
                    r["centroid"] = [(a + b) / 2 for a, b in zip(r["start"], r["end"])]
                if min_ratio is not None or max_ratio is not None:
                    errs: dict[str, str] = {}
                    ratios = design_ratios(session, errs)
                    if not ratios:
                        raise ArgumentValidationError("Ratio filters need design results.", hint="Run etabs_run_design first.")
                    for r in rows:
                        r["ratio"] = ratios.get(r["name"])
                        if r["name"] in errs:
                            r["design_error"] = errs[r["name"]]
            elif kind == "area":
                rows = _areas(session)
                for r in rows:
                    cs = r["coordinates"]
                    r["centroid"] = [sum(c[i] for c in cs) / len(cs) for i in range(3)]
                    try:
                        r["section"] = session.out("AreaObj.GetProperty", Name=r["name"]).get("PropName")
                        lab = session.out("AreaObj.GetLabelFromName", Name=r["name"])
                        r["story"] = lab.get("Story")
                    except ITEM_ERRORS:
                        pass
            else:
                rows = _points(session)
                for r in rows:
                    r["centroid"] = [r["x"], r["y"], r["z"]]
                    if stories:
                        try:
                            r["story"] = session.out("PointObj.GetLabelFromName", Name=r["name"]).get("Story")
                        except ITEM_ERRORS:
                            pass
            lo_len, hi_len = L(min_length, "min_length"), L(max_length, "max_length")
            b = None
            if box:
                b = {k: L(getattr(box, k), k) for k in ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax")}

            def keep(r: dict[str, Any]) -> bool:
                if role and r.get("role") != role:
                    return False
                if stories and r.get("story") not in stories:
                    return False
                if sections and r.get("section") not in sections:
                    return False
                if in_group is not None and r["name"] not in in_group:
                    return False
                failed_check = "design_error" in r
                if min_ratio is not None and not failed_check and (r.get("ratio") is None or r["ratio"] < min_ratio):
                    return False
                if max_ratio is not None and (r.get("ratio") is None or r["ratio"] > max_ratio):
                    return False
                if lo_len is not None and r.get("length", 0) < lo_len - 1e-9:
                    return False
                if hi_len is not None and r.get("length", 0) > hi_len + 1e-9:
                    return False
                if b:
                    c = r["centroid"]
                    for i, ax in enumerate("xyz"):
                        if b[f"{ax}min"] is not None and c[i] < b[f"{ax}min"] - 1e-9:
                            return False
                        if b[f"{ax}max"] is not None and c[i] > b[f"{ax}max"] + 1e-9:
                            return False
                return True

            found = [r for r in rows if keep(r)]
            if select:
                session.call("SelectObj.ClearSelection", _internal=True)
                for r in found:
                    session.call(f"{OBJ_INTERFACE[kind]}.SetSelected", {"Name": r["name"], "Selected": True}, _internal=True)
            for r in found:
                r.pop("centroid", None)
                if "length" in r:
                    r["length"] = round(r["length"], 4)
            return {"kind": kind, "count": len(found), "names": [r["name"] for r in found][:limit],
                    "objects": found[:limit], "selected": select, "units": session.value("GetPresentUnits")}

        return await session.run(work)

    @mcp.tool(annotations=READ)
    async def etabs_what_changed(
        reset: Annotated[bool, Field(description="Only record a new baseline.")] = False,
        limit: Annotated[int, Field(ge=1, le=5000)] = 200,
    ) -> dict[str, Any]:
        """What changed in the model since the last call (or since the baseline): objects added, removed or
        modified, sections/loads/combos/groups/stories added or removed, units, lock and file. Picks up edits
        the engineer made by hand in the ETABS window, so the agent can stay in sync. The first call records
        the baseline."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            new = snapshot(session)
            old = state.get("snapshot")
            state["snapshot"] = new
            if old is None or reset:
                return {"baseline_recorded": True, "frames": len(new["frames"]), "areas": len(new["areas"]),
                        "points": len(new["points"])}
            changes = diff_snapshots(old, new, limit)
            return {"since_seconds": round(new["taken"] - old["taken"], 1), "changed": bool(changes),
                    "changes": changes}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_checkpoint(
        label: Annotated[str, Field(pattern=r"^[A-Za-z0-9_\-]{1,40}$", description="Short name, e.g. 'before-resize'.")],
    ) -> dict[str, Any]:
        """Save the model and keep a copy as a named checkpoint (an undo point before risky edits). Restore it
        with etabs_restore_checkpoint. Needs a saved model."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            current = model_file(session)
            if not current:
                raise ArgumentValidationError("Checkpoints need a saved model.", hint="Call etabs_save_model(path=...) first.")
            session.call("File.Save", {"FileName": current}, _internal=True)
            src = Path(current)
            folder = src.parent / CHECKPOINT_DIR
            folder.mkdir(exist_ok=True)
            dst = folder / f"{src.stem}__{label}__{time.strftime('%Y%m%d-%H%M%S')}.EDB"
            shutil.copy2(src, dst)
            return {"checkpoint": dst.name, "path": str(dst), "model": current}

        return await session.run(work)

    @mcp.tool(annotations=READ)
    async def etabs_list_checkpoints() -> dict[str, Any]:
        """Checkpoints saved for the open model, newest first."""

        def work() -> dict[str, Any]:
            current = model_file(session)
            if not current:
                return {"checkpoints": []}
            folder = Path(current).parent / CHECKPOINT_DIR
            files = sorted(folder.glob(f"{Path(current).stem}__*.EDB"), key=lambda p: p.stat().st_mtime, reverse=True) \
                if folder.is_dir() else []
            return {"checkpoints": [{"name": f.name, "label": f.stem.split("__")[1] if "__" in f.stem else f.stem,
                                     "saved": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.stat().st_mtime)),
                                     "size_kb": f.stat().st_size // 1024} for f in files]}

        return await session.run(work)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def etabs_restore_checkpoint(
        checkpoint: Annotated[str, Field(description="Checkpoint file name or label from etabs_list_checkpoints.")],
        confirm: Annotated[bool, Field(description="Restoring replaces the current model state. Ask the user first.")] = False,
    ) -> dict[str, Any]:
        """Roll the model back to a checkpoint. The model keeps its original file name."""

        def work() -> dict[str, Any]:
            require_confirm(confirm, session, "Restoring a checkpoint discards the current model state and")
            current = model_file(session)
            if not current:
                raise ArgumentValidationError("No saved model is open.")
            folder = Path(current).parent / CHECKPOINT_DIR
            stem = Path(current).stem
            cands = [p for p in folder.glob(f"{stem}__*.EDB")
                     if p.name == checkpoint or p.stem.split("__")[1:2] == [checkpoint]] if folder.is_dir() else []
            if not cands:
                raise ArgumentValidationError(f"No checkpoint '{checkpoint}' for {Path(current).name}.",
                                              hint="See etabs_list_checkpoints.")
            src = max(cands, key=lambda p: p.stat().st_mtime)
            if session.value("GetModelIsLocked"):
                session.call("SetModelIsLocked", {"Lockit": False}, _internal=True)
            session.call("File.OpenFile", {"FileName": str(src)}, _internal=True)
            session.call("File.Save", {"FileName": current}, _internal=True)
            state.pop("snapshot", None)
            return {"restored": src.name, "model": model_file(session)}

        return await session.run(work)

    @mcp.tool(annotations=READ)
    async def etabs_export_model(
        path: Annotated[str | None, Field(description="Where to write the .e2k text file (default: next to the model).")] = None,
        offset: Annotated[int, Field(ge=0, description="Character offset into the file, for paging.")] = 0,
        max_chars: Annotated[int, Field(ge=1000, le=200000)] = 40000,
        sections: Annotated[list[str] | None, Field(description="Only return these $-sections (prefix match), e.g. ['STORIES', 'FRAME ASSIGNS', 'LOAD PATTERNS'].")] = None,
    ) -> dict[str, Any]:
        """Export the whole model as ETABS's .e2k text and return it (paged). One read gives the complete
        definition (stories, grids, materials, sections, objects, assignments, loads, cases), ideal for audits."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            current = model_file(session)
            target = Path(path) if path else (Path(current).with_suffix(".e2k") if current else None)
            if target is None:
                raise ArgumentValidationError("The model is untitled; pass a path for the .e2k file.")
            target.parent.mkdir(parents=True, exist_ok=True)
            session.call("File.ExportFile", {"FileName": str(target), "FileType": "TextFile"}, _internal=True)
            text = target.read_text(errors="replace")
            # '$ <SECTION>' lines head each block; the first '$ File ... saved ...' line is only a stamp.
            index = [ln[2:].strip() for ln in text.splitlines()
                     if ln.startswith("$ ") and not ln[2:].lstrip().startswith("File ")]
            if sections:
                wanted = {s.upper() for s in sections}
                blocks, keep = [], False
                for ln in text.splitlines():
                    if ln.startswith("$ "):
                        head = ln[2:].strip().upper()
                        keep = any(head == w or head.startswith(w + " ") for w in wanted)
                    if keep:
                        blocks.append(ln)
                text = "\n".join(blocks)
            chunk = text[offset: offset + max_chars]
            out = {"path": str(target), "total_chars": len(text), "offset": offset, "returned_chars": len(chunk),
                   "sections": index, "text": chunk}
            if offset + len(chunk) < len(text):
                out["next_offset"] = offset + len(chunk)
            return out

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_design_report(
        path: Annotated[str | None, Field(description="Write the Markdown here (default: <model>_report.md next to the model).")] = None,
        top: Annotated[int, Field(ge=1, le=500, description="Critical members to list.")] = 15,
    ) -> dict[str, Any]:
        """Write an engineering summary in Markdown: model, loads, analysis status, base reactions, modal
        periods, drifts and the design check with the most critical members. Returns the text and the file."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            md = build_report(session, top)
            current = model_file(session)
            target = Path(path) if path else (Path(current).with_name(Path(current).stem + "_report.md") if current else None)
            if target:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(md, encoding="utf-8")
            return {"path": str(target) if target else None, "markdown": md}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_assign_label(
        label_type: Annotated[Literal["group", "pier", "spandrel"], Field(description="Groups organise selection and output; piers/spandrels drive wall design and forces.")],
        label: str,
        object_kind: Literal["frame", "area", "point"] = "area",
        names: list[str] | None = None,
        group: Annotated[str | None, Field(description="Assign to every object already in this group.")] = None,
        selected: bool = False,
        remove: Annotated[bool, Field(description="Groups only: remove the objects from the group instead.")] = False,
    ) -> dict[str, Any]:
        """Assign objects to a group, pier or spandrel label (created if missing), like Assign > ... in ETABS."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            if label_type != "group" and object_kind == "point":
                raise ArgumentValidationError("Piers and spandrels apply to frames and areas only.")
            if label_type == "group":
                if label not in names_list(session, "GroupDef.GetNameList"):
                    session.call("GroupDef.SetGroup", {"Name": label}, _internal=True)
            elif label_type == "pier":
                session.call("PierLabel.SetPier", {"Name": label}, _internal=True)
            else:
                session.call("SpandrelLabel.SetSpandrel", {"Name": label, "IsMultiStory": False}, _internal=True)
            iface = OBJ_INTERFACE[object_kind]
            ok, errors = 0, []
            for i, (name, item) in enumerate(targets(names, group, selected)):
                try:
                    if label_type == "group":
                        session.call(f"{iface}.SetGroupAssign",
                                     {"Name": name, "GroupName": label, "Remove": remove, "ItemType": item}, _internal=True)
                    elif label_type == "pier":
                        session.call(f"{iface}.SetPier", {"Name": name, "PierName": label, "ItemType": item}, _internal=True)
                    else:
                        session.call(f"{iface}.SetSpandrel", {"Name": name, "SpandrelName": label, "ItemType": item},
                                     _internal=True)
                    ok += 1
                except ITEM_ERRORS as exc:
                    errors.append(batch_item_error(i, exc, name))
            return {"label_type": label_type, "label": label, "assigned_calls_ok": ok, "errors": errors}

        return await session.run(work)


# --------------------------------------------------------------------------- report

def _table_or_none(session: EtabsSession, key: str, cases: list[str] | None, combos: list[str] | None, limit: int):
    try:
        select_cases_for_display(session, cases, combos)
        return read_table(session, key, limit=limit)
    except (EtabsCallFailed, ArgumentValidationError):
        return None


def _md_table(rows: list[dict[str, Any]], fields: list[str]) -> str:
    def fmt(v: Any) -> str:
        if isinstance(v, float):
            return f"{v:.4g}"
        return "" if v is None else str(v)

    head = "| " + " | ".join(fields) + " |\n| " + " | ".join("---" for _ in fields) + " |\n"
    return head + "".join("| " + " | ".join(fmt(r.get(f)) for f in fields) + " |\n" for r in rows)


def build_report(session: EtabsSession, top: int) -> str:
    units = session.value("GetPresentUnits")
    current = model_file(session) or "(untitled)"
    locked = session.value("GetModelIsLocked")
    frames = _frames(session)
    roles: dict[str, int] = {}
    for f in frames:
        roles[frame_role(f)] = roles.get(frame_role(f), 0) + 1
    lines = [f"# ETABS design summary — {Path(current).stem}", "",
             f"*Generated by etabs-mcp on {time.strftime('%Y-%m-%d %H:%M')} · units {units}*", "",
             "## Model", "",
             f"- File: `{current}`",
             f"- Frames: {len(frames)} ({', '.join(f'{v} {k}s' for k, v in sorted(roles.items()))}); "
             f"areas: {session.value('AreaObj.Count')}; joints: {session.value('PointObj.Count')}",
             f"- Stories: {', '.join(names_list(session, 'Story.GetNameList'))}",
             f"- Load patterns: {', '.join(names_list(session, 'LoadPatterns.GetNameList'))}",
             f"- Combinations: {len(names_list(session, 'RespCombo.GetNameList'))}", ""]
    if not locked:
        lines += ["## Analysis", "", "The model has not been analysed (no results).", ""]
        return "\n".join(lines)
    status = case_status(session)
    lines += ["## Analysis", "", _md_table(status, ["case", "status"]), ""]
    cases = [s["case"] for s in status if s["status"] == "finished"]
    base = _table_or_none(session, "Base Reactions", cases, [], 200)
    if base and base["rows"]:
        # Modal cases report one reaction row per mode; they are not loads, so leave them out.
        base["rows"] = [r for r in base["rows"] if "modal" not in str(r.get("CaseType", "")).lower()
                        and not str(r.get("CaseType", "")).startswith("LinMod")]
    if base and base["rows"]:
        lines += ["## Base reactions", "", _md_table(base["rows"], [f for f in ("OutputCase", "FX", "FY", "FZ", "MX", "MY", "MZ") if f in base["fields"]]), ""]
    modal = _table_or_none(session, "Modal Participating Mass Ratios", cases, [], 12)
    if modal and modal["rows"]:
        lines += ["## Modal", "", _md_table(modal["rows"][:12], [f for f in ("Mode", "Period", "UX", "UY", "RZ", "SumUX", "SumUY") if f in modal["fields"]]), ""]
    drifts = _table_or_none(session, "Story Drifts", None, None, 100000)
    if drifts and drifts["rows"]:
        worst: dict[str, dict] = {}
        for r in drifts["rows"]:
            key = f"{r.get('OutputCase')}·{r.get('Direction')}"
            if isinstance(r.get("Drift"), float) and (key not in worst or r["Drift"] > worst[key]["Drift"]):
                worst[key] = r
        rows = sorted(worst.values(), key=lambda r: -r["Drift"])[:10]
        lines += ["## Maximum story drifts", "", _md_table(rows, [f for f in ("OutputCase", "Direction", "Story", "Drift", "Drift/") if f in drifts["fields"]]), ""]
    steel = design_summary(session, "steel", 10 ** 6)
    if steel.get("members"):
        lines += ["## Steel design", "",
                  f"{steel['members_designed']} members checked · max D/C **{steel['max_ratio']:.3f}** · "
                  f"**{steel['failing']}** over capacity", "",
                  _md_table(steel["members"][:top], ["frame", "ratio", "ratio_type", "combo", "location", "error"]), ""]
    conc = design_summary(session, "concrete", top)
    cols = conc.get("columns") if isinstance(conc.get("columns"), dict) else None
    if cols and cols.get("rows"):
        lines += ["## Concrete columns", "", _md_table(cols["rows"], ["frame", "pmm_ratio", "pmm_area", "pmm_combo", "error"]), ""]
    beams = conc.get("beams") if isinstance(conc.get("beams"), dict) else None
    if beams and beams.get("rows"):
        lines += ["## Concrete beams", "", _md_table(beams["rows"], ["frame", "top_area", "bottom_area", "shear_area", "error"]), ""]
    return "\n".join(lines)
