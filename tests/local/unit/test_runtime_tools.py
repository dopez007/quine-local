"""Self-mod runtime file tools stay scoped and return actionable diagnostics."""

from __future__ import annotations

import pathlib

import pytest

from app.runtime import sdk, tools

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def staging(tmp_path, monkeypatch) -> pathlib.Path:
    monkeypatch.setattr(sdk, "STAGING", tmp_path)
    return tmp_path


def test_read_file_is_line_based_and_pageable(staging: pathlib.Path) -> None:
    (staging / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    assert tools._read_file("a.txt", offset=2, limit=1) == "[lines 2-2 of 3] two\n"


def test_file_tools_reject_workspace_escape() -> None:
    assert tools._read_file("../secret.txt").startswith("ERROR")
    assert tools._write_file("../secret.txt", "x").startswith("ERROR")


def test_write_and_exact_edit_roundtrip(staging: pathlib.Path) -> None:
    assert tools._write_file("nested/a.txt", "hello world") == "wrote 11 bytes to nested/a.txt"
    assert tools._edit_file("nested/a.txt", "world", "Quine") == "replaced 1 occurrence(s) in nested/a.txt"
    assert (staging / "nested" / "a.txt").read_text(encoding="utf-8") == "hello Quine"


def test_edit_rejects_missing_and_ambiguous_match(staging: pathlib.Path) -> None:
    (staging / "a.txt").write_text("x x", encoding="utf-8")
    assert "not found" in tools._edit_file("a.txt", "y", "z")
    assert "found 2 occurrences" in tools._edit_file("a.txt", "x", "z")


def test_call_decodes_json_drops_unknown_and_reports_missing() -> None:
    def handler(path: str, count: int = 1) -> str:
        return f"{path}:{count}"

    assert tools._call(handler, '{"path":"a","count":2,"extra":true}') == "a:2"
    missing = tools._call(handler, {})
    assert "missing required argument(s): path" in missing
    assert "Re-call it" in missing


def test_execute_preserves_tool_call_identity(monkeypatch) -> None:
    monkeypatch.setattr(tools, "_run_one", lambda name, args: f"{name}:{args['value']}")
    result = tools.execute([{"id": "call-1", "name": "demo", "args": {"value": 3}}])
    assert result == [{"role": "tool", "tool_call_id": "call-1", "content": "demo:3"}]


def test_search_finds_text_and_respects_glob(staging: pathlib.Path) -> None:
    (staging / "a.py").write_text("needle\n", encoding="utf-8")
    (staging / "b.txt").write_text("needle\n", encoding="utf-8")
    result = tools._search("needle", "*.py")
    assert "a.py:1: needle" in result
    assert "b.txt" not in result
