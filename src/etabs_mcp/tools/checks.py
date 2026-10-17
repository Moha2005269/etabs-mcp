"""Code-check helpers: the questions engineers ask after every analysis, answered in one call each, with the
clause, the numbers and a pass/fail. Thresholds follow ASCE 7-16 and are parameters, so other codes can be
checked by changing them."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..errors import ArgumentValidationError, EtabsCallFailed
from ..session import EtabsSession
from .common import DESTRUCTIVE, READ, names_list, require_confirm
from .tables import read_table, select_cases_for_display

Check = Literal["drift", "torsion", "soft_story", "weight", "modal_mass", "rs_scaling"]
ALL_CHECKS: list[str] = ["drift", "torsion", "soft_story", "weight", "modal_mass", "rs_scaling"]


def _table(session: EtabsSession, key: str, cases: list[str] | None = None) -> list[dict[str, Any]]:
    combos = set(names_list(session, "RespCombo.GetNameList"))
    if cases is None:
        select_cases_for_display(session, None, None)
    else:
        select_cases_for_display(session, [c for c in cases if c not in combos], [c for c in cases if c in combos])
    try:
        return read_table(session, key, limit=10 ** 7)["rows"]
    except EtabsCallFailed:
        return []


def _lateral_cases(session: EtabsSession) -> list[str]:
    """Load cases built from Quake/Wind patterns plus response-spectrum cases."""
    out = []
    for c in names_list(session, "LoadCases.GetNameList"):
        try:
            o = session.out("LoadCases.GetTypeOAPI_1", Name=c)
        except EtabsCallFailed:
            continue
        if str(o.get("CaseType")) == "ResponseSpectrum" or str(o.get("DesignType")) in ("Quake", "Wind"):
            out.append(c)
    return out


def check_drift(session: EtabsSession, cases: list[str], limit: float, amplification: float) -> dict[str, Any]:
    rows = [r for r in _table(session, "Story Drifts", cases) if r.get("OutputCase") in cases]
    worst: dict[tuple, dict] = {}
    for r in rows:
        if not isinstance(r.get("Drift"), float):
            continue
        k = (r["OutputCase"], r["Direction"])
        if k not in worst or r["Drift"] > worst[k]["Drift"]:
            worst[k] = r
    items = []
    for (case, direction), r in sorted(worst.items()):
        design = r["Drift"] * amplification
        items.append({"case": case, "direction": direction, "story": r["Story"], "elastic_drift": r["Drift"],
                      "design_drift": design, "limit": limit, "ratio": round(design / limit, 4),
                      "pass": design <= limit})
    return {"check": "drift", "clause": "ASCE 7-16 12.12.1 (Δ = Cd·δe/Ie ≤ Δa)", "amplification": amplification,
            "pass": all(i["pass"] for i in items) if items else None, "items": items}


def check_torsion(session: EtabsSession, cases: list[str], t1: float, t2: float) -> dict[str, Any]:
    rows = [r for r in _table(session, "Story Max Over Avg Drifts", cases)
            if r.get("OutputCase") in cases and isinstance(r.get("Ratio"), float)]
    items = []
    for r in sorted(rows, key=lambda r: -r["Ratio"]):
        level = "extreme (1b)" if r["Ratio"] > t2 else "torsional (1a)" if r["Ratio"] > t1 else "none"
        items.append({"case": r["OutputCase"], "story": r["Story"], "direction": r.get("Direction"),
                      "ratio": r["Ratio"], "irregularity": level})
    flagged = [i for i in items if i["irregularity"] != "none"]
    return {"check": "torsion", "clause": "ASCE 7-16 Table 12.3-1 Type 1a (>1.2) / 1b (>1.4)",
            "pass": not flagged if items else None, "worst": items[:1], "flagged": flagged[:50]}


def check_soft_story(session: EtabsSession, cases: list[str], ratio_above: float, ratio_avg: float) -> dict[str, Any]:
    rows = [r for r in _table(session, "Story Stiffness", cases) if r.get("OutputCase") in cases]
    items = []
    for case in cases:
        for d in ("X", "Y"):
            stories = [r for r in rows if r["OutputCase"] == case and isinstance(r.get(f"Stiff{d}"), float)
                       and r[f"Stiff{d}"] > 0 and (r.get("StepNumber") in (None, 1.0))]
            # Table lists the top story first.
            for i, r in enumerate(stories):
                above = stories[max(0, i - 3):i]
                if not above:
                    continue
                k = r[f"Stiff{d}"]
                k_above = above[-1][f"Stiff{d}"]
                k_avg3 = sum(a[f"Stiff{d}"] for a in above) / len(above)
                soft = k < ratio_above * k_above or (len(above) == 3 and k < ratio_avg * k_avg3)
                items.append({"case": case, "direction": d, "story": r["Story"], "stiffness": k,
                              "ratio_to_above": round(k / k_above, 3),
                              "ratio_to_avg3_above": round(k / k_avg3, 3) if len(above) == 3 else None,
                              "soft": soft})
    flagged = [i for i in items if i["soft"]]
    return {"check": "soft_story", "clause": f"ASCE 7-16 Table 12.3-2 Type 1a (k < {ratio_above:.0%} of above or "
                                             f"< {ratio_avg:.0%} of average of 3 above)",
            "pass": not flagged if items else None, "flagged": flagged, "stories_checked": len(items)}


def check_weight(session: EtabsSession, limit: float) -> dict[str, Any]:
    rows = [r for r in _table(session, "Mass Summary by Story") if isinstance(r.get("UX"), float)]
    rows = [r for r in rows if r["Story"] != "Base"]
    items = []
    for i, r in enumerate(rows):
        neighbours = [rows[j]["UX"] for j in (i - 1, i + 1) if 0 <= j < len(rows) and rows[j]["UX"] > 0]
        if not neighbours or r["UX"] <= 0:
            continue
        ratio = r["UX"] / min(neighbours)
        items.append({"story": r["Story"], "mass": r["UX"], "ratio_to_adjacent": round(ratio, 3),
                      "irregular": ratio > limit})
    flagged = [i for i in items if i["irregular"]]
    # The roof is exempt from the weight irregularity in ASCE 7-16.
    if rows and flagged and flagged[0]["story"] == rows[0]["Story"]:
        flagged = flagged[1:]
    return {"check": "weight", "clause": f"ASCE 7-16 Table 12.3-2 Type 2 (mass > {limit:.0%} of adjacent; roof exempt)",
            "pass": not flagged if items else None, "flagged": flagged, "stories": items}


def check_modal_mass(session: EtabsSession, target: float, modal_case: str) -> dict[str, Any]:
    rows = [r for r in _table(session, "Modal Participating Mass Ratios", [modal_case]) if r.get("Case") == modal_case]
    if not rows:
        return {"check": "modal_mass", "pass": None, "note": f"No results for modal case '{modal_case}'."}
    last = rows[-1]
    out: dict[str, Any] = {"check": "modal_mass", "clause": f"ASCE 7-16 12.9.1.1 (≥ {target:.0%} participation)",
                           "modes": len(rows), "sum_ux": last.get("SumUX"), "sum_uy": last.get("SumUY")}
    for d in ("UX", "UY"):
        hit = next((r["Mode"] for r in rows if (r.get(f"Sum{d}") or 0) >= target), None)
        out[f"modes_to_reach_{d.lower()}"] = int(hit) if hit else None
    out["pass"] = (last.get("SumUX") or 0) >= target and (last.get("SumUY") or 0) >= target
    if not out["pass"]:
        out["hint"] = "Increase the number of modes with etabs_set_modal_case (or use Ritz vectors)."
    return out


def rs_scaling(session: EtabsSession, rs_case: str, elf_case: str, direction: str, target: float) -> dict[str, Any]:
    rows = _table(session, "Base Reactions", [rs_case, elf_case])
    key = f"F{direction}"
    rs = [abs(r[key]) for r in rows if r.get("OutputCase") == rs_case and isinstance(r.get(key), float)]
    elf = [abs(r[key]) for r in rows if r.get("OutputCase") == elf_case and isinstance(r.get(key), float)]
    if not rs or not elf:
        raise ArgumentValidationError(f"Base reactions for '{rs_case}' and '{elf_case}' are needed.",
                                      hint="Run the analysis with both cases.")
    v_rs, v_elf = max(rs), max(elf)
    ratio = v_rs / v_elf if v_elf else None
    factor = max(1.0, target * v_elf / v_rs) if v_rs else None
    return {"check": "rs_scaling", "clause": f"ASCE 7-16 12.9.1.4 (V_RS ≥ {target:.0%} V_ELF)", "rs_case": rs_case,
            "elf_case": elf_case, "direction": direction, "v_rs": v_rs, "v_elf": v_elf,
            "ratio": round(ratio, 4) if ratio else None, "pass": ratio is not None and ratio >= target - 1e-9,
            "required_scale_factor": round(factor, 4) if factor else None}


def register(mcp: FastMCP, session: EtabsSession) -> None:
    @mcp.tool(annotations=READ)
    async def etabs_code_checks(
        checks: Annotated[list[Check] | None, Field(description="Default: all.")] = None,
        cases: Annotated[list[str] | None, Field(description="Lateral cases/combos to check (default: every Quake, Wind and RS case).")] = None,
        drift_limit: Annotated[float, Field(gt=0, description="Allowable story drift ratio Δa/h (ASCE 7-16 Table 12.12-1: 0.020 for most Risk Category I/II buildings).")] = 0.020,
        drift_amplification: Annotated[float, Field(gt=0, description="Cd/Ie applied to elastic drifts (1.0 for wind or drifts already amplified).")] = 1.0,
        torsion_limits: Annotated[list[float], Field(min_length=2, max_length=2)] = [1.2, 1.4],  # noqa: B006
        soft_story_limits: Annotated[list[float], Field(min_length=2, max_length=2, description="[ratio to story above, ratio to average of 3 above].")] = [0.7, 0.8],  # noqa: B006
        weight_limit: float = 1.5,
        modal_target: float = 0.90,
        modal_case: str = "Modal",
        rs_case: str | None = None,
        elf_case: str | None = None,
        rs_direction: Literal["X", "Y"] = "X",
        rs_target: float = 1.0,
    ) -> dict[str, Any]:
        """Run the standard post-analysis checks in one call: story drift against the allowable, torsional
        irregularity, soft story, weight (mass) irregularity, modal mass participation and response-spectrum base
        shear vs ELF. Each check reports its clause, the governing numbers and pass/fail."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            if not session.value("GetModelIsLocked"):
                raise ArgumentValidationError("Code checks need analysis results.", hint="Run etabs_run_analysis first.")
            wanted = checks or ALL_CHECKS
            lat = cases or _lateral_cases(session)
            out: dict[str, Any] = {"cases": lat, "results": []}
            if "drift" in wanted:
                out["results"].append(check_drift(session, lat, drift_limit, drift_amplification))
            if "torsion" in wanted:
                out["results"].append(check_torsion(session, lat, *torsion_limits))
            if "soft_story" in wanted:
                out["results"].append(check_soft_story(session, lat, *soft_story_limits))
            if "weight" in wanted:
                out["results"].append(check_weight(session, weight_limit))
            if "modal_mass" in wanted:
                out["results"].append(check_modal_mass(session, modal_target, modal_case))
            if "rs_scaling" in wanted:
                rs = rs_case or next((c for c in lat if "RS" in c.upper()), None)
                elf = elf_case or next((c for c in lat if c.upper().startswith(("EQ", "E" + rs_direction))), None)
                if rs and elf:
                    out["results"].append(rs_scaling(session, rs, elf, rs_direction, rs_target))
                else:
                    out["results"].append({"check": "rs_scaling", "pass": None,
                                           "note": "Pass rs_case and elf_case to compare response-spectrum and ELF base shear."})
            verdicts = [r["pass"] for r in out["results"] if r.get("pass") is not None]
            out["summary"] = {r["check"]: ("pass" if r.get("pass") else "FAIL" if r.get("pass") is False else "n/a")
                              for r in out["results"]}
            out["all_pass"] = all(verdicts) if verdicts else None
            return out

        return await session.run(work)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def etabs_scale_response_spectrum(
        rs_case: str,
        elf_case: str,
        direction: Literal["X", "Y"] = "X",
        target: Annotated[float, Field(gt=0, le=1.5, description="Fraction of the ELF base shear to reach (ASCE 7-16: 1.0).")] = 1.0,
        rerun: Annotated[bool, Field(description="Re-run the analysis after scaling (the model is unlocked to edit the case).")] = True,
        confirm: Annotated[bool, Field(description="Scaling unlocks the model and deletes results. Ask the user first.")] = False,
    ) -> dict[str, Any]:
        """Scale a response-spectrum case so its base shear reaches the ELF base shear (ASCE 7-16 12.9.1.4),
        then optionally re-run and verify."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            before = rs_scaling(session, rs_case, elf_case, direction, target)
            factor = before["required_scale_factor"]
            if before["pass"] or not factor or factor <= 1.0 + 1e-9:
                return {"scaled": False, "reason": "Already meets the target.", "before": before}
            require_confirm(confirm, session, "Scaling unlocks the model, deletes results, and")
            o = session.out("LoadCases.ResponseSpectrum.GetLoads", Name=rs_case)
            sf = [v * factor for v in (o.get("SF") or [])]
            session.call("SetModelIsLocked", {"Lockit": False}, _internal=True)
            session.call("LoadCases.ResponseSpectrum.SetLoads",
                         {"Name": rs_case, "NumberLoads": o.get("NumberLoads", len(sf)), "LoadName": o.get("LoadName"),
                          "Func": o.get("Func"), "SF": sf, "CSys": o.get("CSys"), "Ang": o.get("Ang")}, _internal=True)
            out: dict[str, Any] = {"scaled": True, "factor": factor, "new_scale_factors": sf, "before": before}
            if rerun:
                session.call("Analyze.SetRunCaseFlag", {"Name": "", "Run": True, "All": True}, _internal=True)
                session.call("Analyze.RunAnalysis", _internal=True)
                out["after"] = rs_scaling(session, rs_case, elf_case, direction, target)
            return out

        return await session.run(work)
