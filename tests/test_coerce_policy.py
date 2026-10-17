import math

import pytest

from etabs_mcp import policy
from etabs_mcp.catalog import ApiCatalog
from etabs_mcp.coerce import DEFAULT, PLACEHOLDER, build_result, coerce_arguments, jsonable
from etabs_mcp.errors import ArgumentValidationError, ConfirmationRequired, PolicyError


@pytest.fixture(scope="module")
def cat() -> ApiCatalog:
    return ApiCatalog.load_bundled()


def sig(cat, path):
    return cat.resolve(path).methods[0]


def test_named_args_defaults_and_placeholders(cat):
    s = sig(cat, "FrameObj.AddByCoord")
    values = coerce_arguments(s, {"xi": 0, "YI": 0, "ZI": 0, "XJ": 0, "YJ": 0, "ZJ": "3.5"}, cat)
    assert values[:6] == [0.0, 0.0, 0.0, 0.0, 0.0, 3.5]
    assert values[6] is PLACEHOLDER  # ref String Name
    assert values[7:] == [DEFAULT, DEFAULT, DEFAULT]


def test_positional_args(cat):
    s = sig(cat, "FrameObj.SetSection")
    assert coerce_arguments(s, ["1", "W14X90"], cat)[:2] == ["1", "W14X90"]


def test_enum_by_name_number_and_qualified(cat):
    s = sig(cat, "LoadPatterns.Add")
    assert coerce_arguments(s, {"Name": "D", "MyType": "dead"}, cat)[1] == "Dead"
    assert coerce_arguments(s, {"Name": "D", "MyType": 3}, cat)[1] == "Live"
    assert coerce_arguments(s, {"Name": "D", "MyType": "eLoadPatternType.Wind"}, cat)[1] == "Wind"
    with pytest.raises(ArgumentValidationError) as e:
        coerce_arguments(s, {"Name": "D", "MyType": "Gravity"}, cat)
    assert "Dead" in e.value.message


def test_arrays_and_booleans(cat):
    s = sig(cat, "PointObj.SetRestraint")
    vals = coerce_arguments(s, {"Name": "1", "Value": [True, "true", 1, 0, "no", False]}, cat)
    assert vals[1] == [True, True, True, False, False, False]
    with pytest.raises(ArgumentValidationError):
        coerce_arguments(s, {"Name": "1", "Value": True}, cat)


def test_validation_messages(cat):
    s = sig(cat, "FrameObj.SetSection")
    with pytest.raises(ArgumentValidationError) as e:
        coerce_arguments(s, {"Name": "1"}, cat)
    assert "PropName" in e.value.message and "Signature" in (e.value.hint or "")
    with pytest.raises(ArgumentValidationError) as e:
        coerce_arguments(s, {"Name": "1", "PropName": "X", "Bogus": 1}, cat)
    assert "Bogus" in e.value.message
    with pytest.raises(ArgumentValidationError):
        coerce_arguments(sig(cat, "File.NewGridOnly"), {"NumberStorys": 2.5}, cat)
    with pytest.raises(ArgumentValidationError):
        coerce_arguments(sig(cat, "File.NewGridOnly"), {"NumberStorys": True}, cat)


def test_results_are_json_safe_and_capped(cat):
    value, truncated = jsonable([1.0, math.nan, [math.inf]], max_items=10)
    assert value == [1.0, None, [None]] and not truncated
    r = build_result(sig(cat, "FrameObj.GetNameList"), "FrameObj.GetNameList", 0,
                     {"NumberNames": 5, "MyName": list("abcde")}, max_items=2)
    assert r["ok"] and r["outputs"]["MyName"] == ["a", "b"] and r["truncated"]
    r = build_result(sig(cat, "FrameObj.Count"), "FrameObj.Count", 7, {}, max_items=2)
    assert r == {"call": "FrameObj.Count", "value": 7}


def test_policy_levels(cat):
    assert policy.classify(sig(cat, "FrameObj.GetNameList")) == "read"
    assert policy.classify(sig(cat, "FrameObj.SetSection")) == "write"
    assert policy.classify(sig(cat, "FrameObj.Delete")) == "destructive"
    assert policy.classify(sig(cat, "File.OpenFile")) == "destructive"
    assert policy.classify(sig(cat, "App.InternalExec")) == "blocked"
    assert policy.classify(sig(cat, "App.Visible")) == "read"


def test_policy_enforcement(cat):
    write, destroy = sig(cat, "FrameObj.SetSection"), sig(cat, "FrameObj.Delete")
    with pytest.raises(PolicyError):
        policy.check(write, read_only=True, allow_destructive=False, confirm=False)
    with pytest.raises(ConfirmationRequired):
        policy.check(destroy, read_only=False, allow_destructive=False, confirm=False)
    assert policy.check(destroy, read_only=False, allow_destructive=False, confirm=True) == "destructive"
    assert policy.check(destroy, read_only=False, allow_destructive=True, confirm=False) == "destructive"
    with pytest.raises(PolicyError):
        policy.check(sig(cat, "App.InternalExec"), read_only=False, allow_destructive=True, confirm=True)
