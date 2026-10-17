"""Material and section property tools (Define > Material / Section Properties)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..errors import ITEM_ERRORS, ArgumentValidationError, EtabsCallFailed
from ..session import EtabsSession
from ..units import LengthConverter
from .common import READ, WRITE, names_list

MaterialType = Literal["Steel", "Concrete", "Rebar", "Tendon", "Aluminum", "ColdFormed", "NoDesign", "Masonry"]
FrameShape = Literal["rectangle", "circle", "i_section", "tube", "pipe", "from_library"]


def _settle_name(session: EtabsSession, iface: str, actual: str, wanted: str) -> tuple[str, str | None]:
    """Rename an item ETABS created under a substitute name back to the requested one, if ETABS allows it."""
    if actual == wanted:
        return actual, None
    try:
        session.call(f"{iface}.ChangeName", {"Name": actual, "NewName": wanted}, _internal=True)
        return wanted, None
    except ITEM_ERRORS:
        return actual, (
            f"ETABS created it as '{actual}' because '{wanted}' is reserved in this model; use '{actual}' "
            "in later calls."
        )


def _require(**values: Any) -> None:
    missing = [k for k, v in values.items() if v is None]
    if missing:
        raise ArgumentValidationError(f"Missing: {', '.join(missing)}.")


def register(mcp: FastMCP, session: EtabsSession) -> None:
    @mcp.tool(annotations=READ)
    async def etabs_list_properties(
        kind: Literal["materials", "frame_sections", "area_sections", "all"] = "all",
    ) -> dict[str, Any]:
        """List materials (with type), frame sections (with shape and dimensions in present units) and area
        properties (slabs, decks, walls)."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            out: dict[str, Any] = {"units": session.value("GetPresentUnits")}
            if kind in ("materials", "all"):
                mats = []
                for n in names_list(session, "PropMaterial.GetNameList"):
                    try:
                        mats.append({"name": n, "type": session.out("PropMaterial.GetMaterial", Name=n).get("MatType")})
                    except ITEM_ERRORS:
                        mats.append({"name": n})
                out["materials"] = mats
            if kind in ("frame_sections", "all"):
                o = session.out("PropFrame.GetAllFrameProperties")
                names = o.get("MyName") or []
                out["frame_sections"] = [
                    {
                        "name": names[i],
                        "shape": o["PropType"][i],
                        "t3_depth": o["t3"][i],
                        "t2_width": o["t2"][i],
                        "tf": o["tf"][i],
                        "tw": o["tw"][i],
                        "t2b": o["t2b"][i],
                        "tfb": o["tfb"][i],
                    }
                    for i in range(len(names))
                ]
            if kind in ("area_sections", "all"):
                out["area_sections"] = names_list(session, "PropArea.GetNameList")
            return out

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_define_material(
        name: str,
        material_type: MaterialType,
        region: Annotated[str | None, Field(description="Library material: region, e.g. 'United States', 'Europe', 'India'.")] = None,
        standard: Annotated[str | None, Field(description="Library material: standard, e.g. 'ASTM A992', 'EN 1992-1-1'.")] = None,
        grade: Annotated[str | None, Field(description="Library material: grade, e.g. 'Grade 50', 'C30/37'.")] = None,
        elastic_modulus: Annotated[float | None, Field(description="Custom material: E (present force/length^2).")] = None,
        poisson_ratio: float = 0.2,
        thermal_coefficient: float = 1e-5,
        unit_weight: Annotated[float | None, Field(description="Weight per unit volume (present units).")] = None,
        fc: Annotated[float | None, Field(description="Concrete compressive strength f'c.")] = None,
        fy: Annotated[float | None, Field(description="Steel/rebar yield strength.")] = None,
        fu: Annotated[float | None, Field(description="Steel/rebar tensile strength (default 1.25 fy).")] = None,
    ) -> dict[str, Any]:
        """Define a material, either from the ETABS library (region + standard + grade, the same lists as the
        'Add Material' dialog) or as a custom isotropic material from E, weight and strength."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            if region or standard or grade:
                _require(region=region, standard=standard, grade=grade)
                if name in names_list(session, "PropMaterial.GetNameList"):
                    return {"material": name, "source": "existing", "already_existed": True,
                            "note": f"'{name}' was already defined and was kept unchanged."}
                o = session.out(
                    "PropMaterial.AddMaterial",
                    Name=name, MatType=material_type, Region=region, Standard=standard, Grade=grade, UserName=name,
                )
                final, note = _settle_name(session, "PropMaterial", o.get("Name") or name, name)
                return {"material": final, "source": "library", **({"note": note} if note else {})}
            _require(elastic_modulus=elastic_modulus)
            session.call("PropMaterial.SetMaterial", {"Name": name, "MatType": material_type}, _internal=True)
            session.call(
                "PropMaterial.SetMPIsotropic",
                {"Name": name, "E": elastic_modulus, "U": poisson_ratio, "A": thermal_coefficient},
                _internal=True,
            )
            if unit_weight is not None:
                session.call("PropMaterial.SetWeightAndMass", {"Name": name, "MyOption": 1, "Value": unit_weight}, _internal=True)
            if material_type == "Concrete" and fc is not None:
                session.call(
                    "PropMaterial.SetOConcrete_1",
                    {"Name": name, "Fc": fc, "IsLightweight": False, "FcsFactor": 0, "SSType": 1, "SSHysType": 2,
                     "StrainAtFc": 0.0022, "StrainUltimate": 0.0052, "FinalSlope": -0.1},
                    _internal=True,
                )
            if material_type in ("Steel", "Rebar", "ColdFormed", "Tendon") and fy is not None:
                fu_val = fu if fu is not None else 1.25 * fy
                session.call(
                    "PropMaterial.SetOSteel_1",
                    {"Name": name, "Fy": fy, "Fu": fu_val, "EFy": 1.1 * fy, "EFu": 1.1 * fu_val, "SSType": 1,
                     "SSHysType": 1, "StrainAtHardening": 0.02, "StrainAtMaxStress": 0.1, "StrainAtRupture": 0.2,
                     "FinalSlope": -0.1},
                    _internal=True,
                )
            return {"material": name, "source": "custom"}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_define_frame_section(
        name: str,
        shape: FrameShape,
        material: Annotated[str, Field(description="Material name (see etabs_list_properties).")],
        depth: Annotated[float | str | None, Field(description="t3: overall depth (rectangle, i_section, tube) or outside diameter (circle, pipe).")] = None,
        width: Annotated[float | str | None, Field(description="t2: width (rectangle, tube) or top flange width (i_section).")] = None,
        flange_thickness: float | str | None = None,
        web_thickness: Annotated[float | str | None, Field(description="Web thickness (i_section, tube) or wall thickness (pipe).")] = None,
        bottom_flange_width: Annotated[float | str | None, Field(description="i_section only; defaults to width.")] = None,
        bottom_flange_thickness: Annotated[float | str | None, Field(description="i_section only; defaults to flange_thickness.")] = None,
        library_file: Annotated[str | None, Field(description="from_library: section database file, e.g. 'AISC16.xml', 'Euro.xml', 'Indian.xml'.")] = None,
        library_section: Annotated[str | None, Field(description="from_library: section name in that file, e.g. 'W14X90'.")] = None,
    ) -> dict[str, Any]:
        """Define a frame section: parametric concrete/steel shapes, or a steel shape imported from an ETABS
        section library. Dimensions are in present length units."""

        def work() -> dict[str, Any]:
            session.ensure_connected()
            L = LengthConverter(session.value("GetPresentUnits"))
            t3, t2 = L(depth, "depth"), L(width, "width")
            tf, tw = L(flange_thickness, "flange_thickness"), L(web_thickness, "web_thickness")
            t2b, tfb = L(bottom_flange_width, "bottom_flange_width"), L(bottom_flange_thickness, "bottom_flange_thickness")
            base = {"Name": name, "MatProp": material}
            if shape == "rectangle":
                _require(depth=t3, width=t2)
                session.call("PropFrame.SetRectangle", {**base, "T3": t3, "T2": t2}, _internal=True)
            elif shape == "circle":
                _require(depth=t3)
                session.call("PropFrame.SetCircle", {**base, "T3": t3}, _internal=True)
            elif shape == "i_section":
                _require(depth=t3, width=t2, flange_thickness=tf, web_thickness=tw)
                session.call(
                    "PropFrame.SetISection",
                    {**base, "T3": t3, "T2": t2, "Tf": tf, "Tw": tw,
                     "T2b": t2b or t2, "Tfb": tfb or tf},
                    _internal=True,
                )
            elif shape == "tube":
                _require(depth=t3, width=t2, flange_thickness=tf, web_thickness=tw)
                session.call(
                    "PropFrame.SetTube",
                    {**base, "T3": t3, "T2": t2, "Tf": tf, "Tw": tw},
                    _internal=True,
                )
            elif shape == "pipe":
                _require(depth=t3, web_thickness=tw)
                session.call("PropFrame.SetPipe", {**base, "T3": t3, "TW": tw}, _internal=True)
            else:
                _require(library_file=library_file, library_section=library_section)
                before = set(names_list(session, "PropFrame.GetNameList"))
                if name in before:
                    return {"section": name, "shape": shape, "already_existed": True,
                            "note": f"'{name}' was already defined and was kept unchanged."}
                session.call(
                    "PropFrame.ImportProp",
                    {**base, "FileName": library_file, "PropName": library_section},
                    _internal=True,
                )
                # ETABS reports success but may import under another name (e.g. 'W14X90-1') when the
                # requested one collides with an internal name; find what it really created.
                created = [n for n in names_list(session, "PropFrame.GetNameList") if n not in before]
                if not created:
                    raise EtabsCallFailed(
                        f"ETABS did not import '{library_section}' from {library_file}.",
                        path="PropFrame.ImportProp",
                        hint="Check the section name and file (e.g. AISC16.xml, Euro.xml) and that the material exists.",
                    )
                final, note = _settle_name(session, "PropFrame", created[0], name)
                return {"section": final, "shape": shape, "material": material, **({"note": note} if note else {})}
            return {"section": name, "shape": shape, "material": material}

        return await session.run(work)

    @mcp.tool(annotations=WRITE)
    async def etabs_define_area_section(
        name: str,
        kind: Literal["slab", "wall"],
        material: str,
        thickness: Annotated[float | str, Field(description="Number in present units or e.g. '200 mm'.")],
        slab_type: Literal["Slab", "Drop", "Ribbed", "Waffle", "Mat", "Footing"] = "Slab",
        shell_type: Literal["ShellThin", "ShellThick", "Membrane", "Layered"] = "ShellThin",
    ) -> dict[str, Any]:
        """Define a slab or wall property (Define > Section Properties > Slab/Wall Sections)."""

        def work() -> dict[str, Any]:
            t = LengthConverter(session.value("GetPresentUnits"))(thickness, "thickness")
            if t <= 0:
                raise ArgumentValidationError("thickness must be positive.")
            if kind == "slab":
                session.call(
                    "PropArea.SetSlab",
                    {"Name": name, "SlabType": slab_type, "ShellType": shell_type, "MatProp": material, "Thickness": t},
                    _internal=True,
                )
            else:
                session.call(
                    "PropArea.SetWall",
                    {"Name": name, "WallPropType": "Specified", "ShellType": shell_type, "MatProp": material,
                     "Thickness": t},
                    _internal=True,
                )
            return {"property": name, "kind": kind, "thickness": t}

        return await session.run(work)
