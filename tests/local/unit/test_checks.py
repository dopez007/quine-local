"""Deterministic verification-check schema and data helpers."""

from __future__ import annotations

import pytest

from kernel import checks

pytestmark = pytest.mark.unit


def _spec() -> dict:
    return {
        "name": "health",
        "steps": [
            {
                "method": "GET",
                "path": "/health",
                "expect": {"status": 200, "json_subset": {"ok": True}},
                "save": {"request_id": "$.request.id"},
            }
        ],
    }


def test_validate_spec_accepts_documented_shape() -> None:
    assert checks.validate_spec(_spec()) == (True, "")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda spec: spec.update(extra=True), "unknown spec keys"),
        (lambda spec: spec.update(name=""), "name must be"),
        (lambda spec: spec.update(steps=[]), "steps must be"),
        (lambda spec: spec["steps"][0].update(method="TRACE"), "method must be"),
        (lambda spec: spec["steps"][0].update(path="https://example.com"), "local path"),
        (lambda spec: spec["steps"][0].update(timeout=0), "timeout must be"),
        (lambda spec: spec["steps"][0].update(expect={}), "expect is required"),
    ],
)
def test_validate_spec_rejects_unsafe_or_empty_shapes(mutate, message: str) -> None:
    spec = _spec()
    mutate(spec)
    ok, error = checks.validate_spec(spec)
    assert ok is False
    assert message in error


def test_substitute_preserves_raw_exact_value_and_formats_embedded_value() -> None:
    variables = {"id": 7, "name": "Quine"}
    assert checks._substitute("{id}", variables) == 7
    assert checks._substitute("item-{id}-{name}", variables) == "item-7-Quine"
    assert checks._substitute({"items": ["{id}"]}, variables) == {"items": [7]}


@pytest.mark.parametrize(
    ("path", "expected"),
    [("$", {"a": [1, 2]}), ("$.a.0", 1), ("a.-1", 2)],
)
def test_extract_supports_root_dict_and_list_paths(path: str, expected) -> None:
    ok, value = checks._extract({"a": [1, 2]}, path)
    assert ok is True
    assert value == expected


def test_extract_reports_missing_path() -> None:
    assert checks._extract({"a": []}, "a.0") == (False, None)


def test_json_subset_matches_nested_dicts_and_unordered_list_members() -> None:
    actual = {"meta": {"ok": True, "extra": 1}, "items": [{"id": 2}, {"id": 1}]}
    expected = {"meta": {"ok": True}, "items": [{"id": 1}]}
    assert checks._json_subset(expected, actual) is True
    assert checks._json_subset({"items": [{"id": 3}]}, actual) is False
