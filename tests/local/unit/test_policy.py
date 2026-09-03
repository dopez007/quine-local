"""App-workspace containment and structural policy."""

from __future__ import annotations

import pytest

from kernel import policy

pytestmark = pytest.mark.unit


def test_resolve_within_accepts_nested_path(tmp_path) -> None:
    staging = tmp_path / "app"
    staging.mkdir()
    resolved = policy.resolve_within(staging, "features/tool.py")
    assert resolved == staging / "features" / "tool.py"


@pytest.mark.parametrize("relative", ["../state/secrets.env", "../../kernel/core.py"])
def test_resolve_within_rejects_escape(tmp_path, relative: str) -> None:
    staging = tmp_path / "app"
    staging.mkdir()
    assert policy.resolve_within(staging, relative) is None


def test_check_staging_names_missing_contract_files(tmp_path) -> None:
    ok, errors = policy.check_staging(tmp_path)
    assert ok is False
    assert errors == [
        "main.py is missing (the app must keep an entry point)",
        "app_manifest.json is missing",
    ]


def test_check_staging_accepts_minimal_app(tmp_path) -> None:
    (tmp_path / "main.py").write_text("app = object()\n", encoding="utf-8")
    (tmp_path / "app_manifest.json").write_text("{}\n", encoding="utf-8")
    assert policy.check_staging(tmp_path) == (True, [])
