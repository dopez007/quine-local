"""Minimal candidate-kernel contract used by gated kernel self-update validation."""

from __future__ import annotations

import pytest

from kernel import policy, state_store

pytestmark = pytest.mark.contract


def test_candidate_policy_keeps_edits_inside_app_workspace(tmp_path) -> None:
    staging = tmp_path / "app"
    staging.mkdir()
    assert policy.resolve_within(staging, "main.py") == staging / "main.py"
    assert policy.resolve_within(staging, "../kernel/core.py") is None


def test_candidate_requires_runnable_app_shape(tmp_path) -> None:
    staging = tmp_path / "app"
    staging.mkdir()
    assert policy.check_staging(staging)[0] is False
    (staging / "main.py").write_text("app = object()\n", encoding="utf-8")
    (staging / "app_manifest.json").write_text("{}\n", encoding="utf-8")
    assert policy.check_staging(staging) == (True, [])


def test_candidate_secret_environment_strips_well_known_keys(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(state_store, "SECRETS_ENV", tmp_path / "secrets.env")
    base = {
        "PATH": "safe",
        "ANTHROPIC_API_KEY": "secret",
        state_store.keycrypt.SECRET_KEY_ENV: "master",
    }
    assert state_store.stripped_env(base) == {"PATH": "safe"}
