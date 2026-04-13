import json
import math

from json_util import sanitize_for_json


def test_sanitize_nan_inf_become_null_roundtrip():
    raw = {"a": 1.0, "b": float("nan"), "c": float("inf"), "n": {"z": float("-inf")}}
    clean = sanitize_for_json(raw)
    s = json.dumps(clean)
    assert "NaN" not in s
    assert json.loads(s)["b"] is None
