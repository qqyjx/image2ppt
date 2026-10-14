import copy
import json

from conftest import EXAMPLES
from image2ppt import schema


def raw():
    return json.loads((EXAMPLES / "demo_layout.raw.json").read_text())


def test_demo_layout_is_valid():
    assert schema.validate_layout(raw()) == []


def test_patch_schema_accepts_review_patch():
    patch = {"ops": [{"op": "move", "id": "b1", "box": [1, 2, 3, 4], "why": "x"}], "verdict": "fix"}
    assert schema.validate(patch, schema.PATCH_SCHEMA) == []


def test_invalid_layouts_are_rejected():
    base = raw()
    cases = {}
    bad = copy.deepcopy(base)
    bad["elements"][0]["type"] = "circle"
    cases["bad enum"] = bad
    bad = copy.deepcopy(base)
    bad["elements"][1]["box"] = [1, 2, 3]
    cases["short box"] = bad
    bad = copy.deepcopy(base)
    del bad["background"]
    cases["missing required key"] = bad
    bad = copy.deepcopy(base)
    bad["elements"][1]["id"] = bad["elements"][2]["id"]
    cases["duplicate id"] = bad
    bad = copy.deepcopy(base)
    bad["elements"][-1]["to_id"] = "nope"
    cases["dangling reference"] = bad
    bad = copy.deepcopy(base)
    bad["elements"][-1]["points"] = [[1, 2]]
    cases["one-point line"] = bad
    bad = copy.deepcopy(base)
    bad["elements"][1]["text"] = 5
    cases["wrong text type"] = bad
    for name, lay in cases.items():
        assert schema.validate_layout(lay), name


def test_bool_is_not_a_number():
    assert schema.validate(True, {"type": "number"})
