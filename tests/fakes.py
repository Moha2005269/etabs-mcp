"""A fake ETABS backend for offline tests.

It runs on the real bundled API catalog, so every call a tool makes is resolved and coerced against the true
ETABSv1.dll signatures; a wrong method or parameter name fails the test exactly as it would against ETABS.
A little state (objects, tables, lock) makes the curated workflows meaningful.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from etabs_mcp.catalog import ApiCatalog, MethodSig
from etabs_mcp.coerce import DEFAULT, PLACEHOLDER

_BUNDLED = ApiCatalog.load_bundled()


def _empty(type_name: str, catalog: ApiCatalog) -> Any:
    if type_name.endswith("[]"):
        return []
    if type_name == "String":
        return ""
    if type_name == "Boolean":
        return False
    if type_name in ("Double", "Single"):
        return 0.0
    if type_name in catalog.enums:
        return min(catalog.enums[type_name], key=catalog.enums[type_name].get)
    return 0


class FakeEtabs:
    """Minimal model state."""

    def __init__(self) -> None:
        self.frames: dict[str, dict[str, Any]] = {}
        self.points: dict[str, tuple[float, float, float]] = {}
        self.areas: dict[str, list[tuple[float, float, float]]] = {}
        self.locked = False
        self.filename = ""
        self.units = "kN_m_C"
        self.patterns = ["Dead", "Live"]
        self.cases = ["Dead", "Live", "Modal"]
        self.combos: list[str] = []
        self.frame_props: list[str] = ["ConcBm", "ConcCol"]
        self.materials: list[str] = ["4000Psi", "A992Fy50"]
        self.tables: dict[str, tuple[list[str], list[list[str]]]] = {
            "Story Drifts": (["Story", "OutputCase", "Direction", "Drift"], [["Story1", "Dead", "X", "0.0012"],
                                                                              ["Story2", "Dead", "X", "0.0021"]]),
            "Frame Assignments - Section Properties": (["UniqueName", "Story", "Section"],
                                                       [["1", "Story1", "C1"], ["2", "Story1", "B1"]]),
        }
        self.staged: tuple[str, list[str], list[str]] | None = None

    def point(self, xyz: tuple[float, float, float]) -> str:
        for name, p in self.points.items():
            if p == xyz:
                return name
        name = str(len(self.points) + 1)
        self.points[name] = xyz
        return name


class FakeBackend:
    def __init__(self, handlers: dict[str, Callable[[dict[str, Any]], tuple[Any, dict]]] | None = None):
        self.catalog: ApiCatalog | None = _BUNDLED
        self.state = FakeEtabs()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.handlers = {**self._default_handlers(), **(handlers or {})}
        self._connected = False
        self.pid = None

    # --- Backend protocol -----------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self, **kwargs: Any) -> dict[str, Any]:
        self._connected = True
        return self.info()

    def disconnect(self, *, close_app: bool, save: bool) -> None:
        self._connected = False

    def is_alive(self) -> bool:
        return self._connected

    def info(self) -> dict[str, Any]:
        return {"connected": self._connected, "etabs_version": "23.3.2", "api_catalog": "fake"}

    def root(self, name: str) -> str:
        return name

    def get_property(self, obj: Any, name: str) -> str:
        return f"{obj}.{name}"

    def invoke(self, target: Any, sig: MethodSig, values: list[Any]) -> tuple[Any, dict[str, Any]]:
        args = {p.name: v for p, v in zip(sig.params, values)}
        self.calls.append((sig.qualified_name, args))
        handler = self.handlers.get(sig.qualified_name)
        outputs = {
            p.name: (_empty(p.type, _BUNDLED) if args[p.name] in (PLACEHOLDER, DEFAULT) else args[p.name])
            for p in sig.params
            if p.by_ref
        }
        if handler is not None:
            ret, extra = handler(args)
            outputs.update(extra)
            return ret, outputs
        if sig.returns_status:
            return 0, outputs
        return _empty(sig.returns, _BUNDLED) if sig.returns != "Void" else None, outputs

    # --- stateful behaviour -----------------------------------------------------------------

    def _default_handlers(self) -> dict[str, Callable[[dict[str, Any]], tuple[Any, dict]]]:
        s = lambda: self.state  # noqa: E731

        def add_frame(a: dict[str, Any]) -> tuple[int, dict]:
            pi = s().point((a["XI"], a["YI"], a["ZI"]))
            pj = s().point((a["XJ"], a["YJ"], a["ZJ"]))
            name = a["UserName"] if a["UserName"] not in ("", DEFAULT) else str(len(s().frames) + 1)
            sec = a["PropName"] if a["PropName"] is not DEFAULT else "Default"
            s().frames[name] = {"i": pi, "j": pj, "section": sec}
            return 0, {"Name": name}

        def all_frames(a: dict[str, Any]) -> tuple[int, dict]:
            fr = s().frames
            pts = s().points
            names = list(fr)
            return 0, {
                "NumberNames": len(names), "MyName": names,
                "PropName": [fr[n]["section"] for n in names], "StoryName": ["Story1"] * len(names),
                "PointName1": [fr[n]["i"] for n in names], "PointName2": [fr[n]["j"] for n in names],
                "Point1X": [pts[fr[n]["i"]][0] for n in names], "Point1Y": [pts[fr[n]["i"]][1] for n in names],
                "Point1Z": [pts[fr[n]["i"]][2] for n in names], "Point2X": [pts[fr[n]["j"]][0] for n in names],
                "Point2Y": [pts[fr[n]["j"]][1] for n in names], "Point2Z": [pts[fr[n]["j"]][2] for n in names],
                "Angle": [0.0] * len(names),
            }

        def all_points(a: dict[str, Any]) -> tuple[int, dict]:
            names = list(s().points)
            return 0, {"NumberNames": len(names), "MyName": names,
                       "X": [s().points[n][0] for n in names], "Y": [s().points[n][1] for n in names],
                       "Z": [s().points[n][2] for n in names]}

        def add_area(a: dict[str, Any]) -> tuple[int, dict]:
            name = str(len(s().areas) + 1)
            s().areas[name] = list(zip(a["X"], a["Y"], a["Z"]))
            for p in s().areas[name]:
                s().point(p)
            return 0, {"Name": name}

        def all_areas(a: dict[str, Any]) -> tuple[int, dict]:
            names, delim, pnames, xs, ys, zs = list(s().areas), [], [], [], [], []
            for n in names:
                for p in s().areas[n]:
                    pnames.append(s().point(p))
                    xs.append(p[0])
                    ys.append(p[1])
                    zs.append(p[2])
                delim.append(len(pnames) - 1)
            return 0, {"NumberNames": len(names), "MyName": names, "DesignOrientation": ["Floor"] * len(names),
                       "NumberBoundaryPts": len(pnames), "PointDelimiter": delim, "PointNames": pnames,
                       "PointX": xs, "PointY": ys, "PointZ": zs}

        def count(kind: str) -> Callable[[dict[str, Any]], tuple[int, dict]]:
            return lambda a: (len(getattr(s(), kind)), {})

        def name_list(items: Callable[[], list[str]]) -> Callable[[dict[str, Any]], tuple[int, dict]]:
            return lambda a: (0, {"NumberNames": len(items()), "MyName": list(items())})

        def save(a: dict[str, Any]) -> tuple[int, dict]:
            if a["FileName"] not in ("", DEFAULT):
                s().filename = a["FileName"]
            return (0 if s().filename else 1), {}

        def run_analysis(a: dict[str, Any]) -> tuple[int, dict]:
            s().locked = True
            return 0, {}

        def set_locked(a: dict[str, Any]) -> tuple[int, dict]:
            s().locked = bool(a["Lockit"])
            return 0, {}

        def case_status(a: dict[str, Any]) -> tuple[int, dict]:
            return 0, {"NumberItems": len(s().cases), "CaseName": list(s().cases),
                       "Status": [4 if s().locked else 1] * len(s().cases)}

        def run_flags(a: dict[str, Any]) -> tuple[int, dict]:
            return 0, {"NumberItems": len(s().cases), "CaseName": list(s().cases), "Run": [True] * len(s().cases)}

        def all_tables(a: dict[str, Any]) -> tuple[int, dict]:
            keys = list(s().tables)
            return 0, {"NumberTables": len(keys), "TableKey": keys, "TableName": keys,
                       "ImportType": [0, 2][: len(keys)], "IsEmpty": [False] * len(keys)}

        def fields_in_table(a: dict[str, Any]) -> tuple[int, dict]:
            fields, _ = s().tables.get(a["TableKey"], ([], []))
            units = ["" if f != "Drift" else "unitless" for f in fields]
            return 0, {"TableVersion": 1, "NumberFields": len(fields), "FieldKey": fields, "FieldName": fields,
                       "Description": fields, "UnitsString": units, "IsImportable": [True] * len(fields)}

        def table_display(a: dict[str, Any]) -> tuple[int, dict]:
            if a["TableKey"] not in s().tables:
                return 1, {}
            fields, rows = s().tables[a["TableKey"]]
            return 0, {"TableVersion": 1, "FieldsKeysIncluded": fields, "NumberRecords": len(rows),
                       "TableData": [v for r in rows for v in r]}

        def table_edit_get(a: dict[str, Any]) -> tuple[int, dict]:
            return table_display(a)

        def table_edit_set(a: dict[str, Any]) -> tuple[int, dict]:
            s().staged = (a["TableKey"], list(a["FieldsKeysIncluded"]), list(a["TableData"]))
            return 0, {}

        def table_apply(a: dict[str, Any]) -> tuple[int, dict]:
            if s().staged:
                key, fields, flat = s().staged
                n = len(fields)
                s().tables[key] = (fields, [flat[i:i + n] for i in range(0, len(flat), n)])
                s().staged = None
            return 0, {"NumFatalErrors": 0, "NumErrorMsgs": 0, "NumWarnMsgs": 0, "NumInfoMsgs": 1,
                       "ImportLog": "Import completed."}

        def steel_summary(a: dict[str, Any]) -> tuple[int, dict]:
            return 0, {"NumberItems": 2, "FrameName": ["1", "2"], "Ratio": [0.62, 1.08], "RatioType": [1, 1],
                       "Location": [0.0, 3.0], "ComboName": ["DStlS1", "DStlS2"], "ErrorSummary": ["", ""],
                       "WarningSummary": ["", ""]}

        def combo_add(a: dict[str, Any]) -> tuple[int, dict]:
            s().combos.append(a["Name"])
            return 0, {}

        def add_prop(a: dict[str, Any]) -> tuple[int, dict]:
            if a["Name"] not in s().frame_props:
                s().frame_props.append(a["Name"])
            return 0, {}

        def add_material(a: dict[str, Any]) -> tuple[int, dict]:
            name = a["Name"] if a["Name"] not in s().materials else a["Name"] + "-1"
            s().materials.append(name)
            return 0, {"Name": name}

        def rename(items: Callable[[], list[str]]) -> Callable[[dict[str, Any]], tuple[int, dict]]:
            def run(a: dict[str, Any]) -> tuple[int, dict]:
                lst = items()
                if a["NewName"] in lst or a["Name"] not in lst:
                    return 1, {}
                lst[lst.index(a["Name"])] = a["NewName"]
                return 0, {}
            return run

        return {
            "cPropFrame.GetNameList": name_list(lambda: s().frame_props),
            "cPropFrame.ImportProp": add_prop,
            "cPropFrame.ChangeName": rename(lambda: s().frame_props),
            "cPropMaterial.GetNameList": name_list(lambda: s().materials),
            "cPropMaterial.AddMaterial": add_material,
            "cPropMaterial.ChangeName": rename(lambda: s().materials),
            "cFrameObj.AddByCoord": add_frame,
            "cFrameObj.GetAllFrames": all_frames,
            "cFrameObj.Count": count("frames"),
            "cPointObj.GetAllPoints": all_points,
            "cPointObj.Count": count("points"),
            "cPointObj.AddCartesian": lambda a: (0, {"Name": s().point((a["X"], a["Y"], a["Z"]))}),
            "cAreaObj.AddByCoord": add_area,
            "cAreaObj.GetAllAreas": all_areas,
            "cAreaObj.Count": count("areas"),
            "cFrameObj.GetLabelFromName": lambda a: (0, {"Label": f"C{a['Name']}", "Story": "Story1"}),
            "cAreaObj.GetLabelFromName": lambda a: (0, {"Label": f"F{a['Name']}", "Story": "Story1"}),
            "cPointObj.GetLabelFromName": lambda a: (0, {"Label": a["Name"], "Story": "Base"}),
            "cFrameObj.GetNameListOnStory": lambda a: (0, {"NumberNames": len(s().frames), "MyName": list(s().frames)}),
            "cSapModel.GetModelFilename": lambda a: (s().filename, {}),
            "cSapModel.GetPresentUnits": lambda a: (s().units, {}),
            "cSapModel.SetPresentUnits": lambda a: (setattr(s(), "units", a["Units"]) or 0, {}),
            "cSapModel.GetModelIsLocked": lambda a: (s().locked, {}),
            "cSapModel.SetModelIsLocked": set_locked,
            "cFile.Save": save,
            "cAnalyze.RunAnalysis": run_analysis,
            "cAnalyze.GetCaseStatus": case_status,
            "cAnalyze.GetRunCaseFlag": run_flags,
            "cLoadPatterns.GetNameList": name_list(lambda: s().patterns),
            "cLoadCases.GetNameList": name_list(lambda: s().cases),
            "cCombo.GetNameList": name_list(lambda: s().combos),
            "cCombo.Add": combo_add,
            "cStory.GetStories_2": lambda a: (0, {
                "BaseElevation": 0.0, "NumberStories": 2, "StoryNames": ["Story1", "Story2"],
                "StoryElevations": [3.0, 6.0], "StoryHeights": [3.0, 3.0], "IsMasterStory": [False, True],
                "SimilarToStory": ["Story2", ""], "SpliceAbove": [False, False], "SpliceHeight": [0.0, 0.0],
                "color": [0, 0]}),
            "cDatabaseTables.GetAllTables": all_tables,
            "cDatabaseTables.GetAllFieldsInTable": fields_in_table,
            "cDatabaseTables.GetTableForDisplayArray": table_display,
            "cDatabaseTables.GetTableForEditingArray": table_edit_get,
            "cDatabaseTables.SetTableForEditingArray": table_edit_set,
            "cDatabaseTables.ApplyEditedTables": table_apply,
            "cDesignSteel.GetSummaryResults": steel_summary,
            "cSelect.GetSelected": lambda a: (0, {"NumberItems": 2, "ObjectType": [2, 1], "ObjectName": ["1", "1"]}),
            "cOAPI.Visible": lambda a: (True, {}),
        }
