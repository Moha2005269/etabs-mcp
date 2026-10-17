import pytest

from etabs_mcp.catalog import ApiCatalog
from etabs_mcp.errors import ApiNotFoundError


@pytest.fixture(scope="module")
def cat() -> ApiCatalog:
    return ApiCatalog.load_bundled()


def test_bundled_spec_covers_the_full_api(cat):
    assert len(cat.interfaces) >= 140
    assert len(cat.enums) >= 50
    assert sum(1 for _ in cat.iter_methods()) >= 1290
    assert "cSapModel" in cat.interfaces and "cOAPI" in cat.interfaces


@pytest.mark.parametrize(
    "path,expected",
    [
        ("FrameObj.GetNameList", "SapModel.FrameObj.GetNameList"),
        ("SapModel.FrameObj.GetNameList", "SapModel.FrameObj.GetNameList"),
        ("frameobj.getnamelist", "SapModel.FrameObj.GetNameList"),
        ("cFrameObj.GetNameList", "SapModel.FrameObj.GetNameList"),
        ("LoadCases.StaticLinear.SetCase", "SapModel.LoadCases.StaticLinear.SetCase"),
        ("cCaseStaticLinear.SetCase", "SapModel.LoadCases.StaticLinear.SetCase"),
        ("Results.Setup.SetCaseSelectedForOutput", "SapModel.Results.Setup.SetCaseSelectedForOutput"),
        ("App.Hide", "App.Hide"),
        ("GetPresentUnits", "SapModel.GetPresentUnits"),
    ],
)
def test_resolve_paths(cat, path, expected):
    assert cat.resolve(path).path == expected


def test_resolve_suggests_close_names(cat):
    with pytest.raises(ApiNotFoundError) as e:
        cat.resolve("FrameObj.GetNameLst")
    assert "GetNameList" in (e.value.hint or "")
    with pytest.raises(ApiNotFoundError) as e:
        cat.resolve("FrameObject.GetNameList")
    assert "FrameObj" in (e.value.hint or "")


def test_property_is_not_a_method(cat):
    with pytest.raises(ApiNotFoundError) as e:
        cat.resolve("LoadCases.StaticLinear")
    assert "property" in (e.value.hint or "")


def test_status_semantics(cat):
    assert cat.resolve("FrameObj.AddByCoord").methods[0].returns_status
    assert not cat.resolve("FrameObj.Count").methods[0].returns_status
    assert not cat.resolve("GetPresentUnits").methods[0].returns_status


def test_search_ranks_name_matches_first(cat):
    results = cat.search("story drifts", limit=5)
    assert results and results[0]["path"].endswith("StoryDrifts")


def test_describe_lists_enum_values_and_byref(cat):
    d = cat.describe("LoadPatterns.Add")
    params = {p["name"]: p for p in d["overloads"][0]["params"]}
    assert "Dead" in params["MyType"]["enum_values"]
    out = cat.describe("FrameObj.GetNameList")["overloads"][0]
    assert all("in/out" in p["direction"] for p in out["params"])
    assert out["safety"] == "read"


def test_interface_paths_are_reachable(cat):
    assert cat.interface_path("cDesignSteel") == "SapModel.DesignSteel"
    assert cat.interface_path("cCaseResponseSpectrum") == "SapModel.LoadCases.ResponseSpectrum"
