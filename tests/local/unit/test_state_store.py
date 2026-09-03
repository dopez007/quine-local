"""Kernel state configuration and secret boundaries remain fail-closed."""

from __future__ import annotations

import json

import pytest
import yaml

from kernel import state_store

pytestmark = pytest.mark.unit


@pytest.fixture
def state_paths(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(state_store, "STATE_DIR", state)
    monkeypatch.setattr(state_store, "CONFIG_YAML", state / "config.yaml")
    monkeypatch.setattr(state_store, "SECRETS_ENV", state / "secrets.env")
    return state


def test_deep_merge_preserves_unspecified_defaults() -> None:
    assert state_store._deep_merge(
        {"agent": {"model": "a", "steps": 3}, "other": 1},
        {"agent": {"model": "b"}},
    ) == {"agent": {"model": "b", "steps": 3}, "other": 1}


def test_load_config_seeds_then_merges_override(state_paths) -> None:
    seeded = state_store.load_config()
    assert seeded["agent"]["engine"]
    override = {"agent": {"model": "custom/model"}}
    state_store.CONFIG_YAML.write_text(yaml.safe_dump(override), encoding="utf-8")
    loaded = state_store.load_config()
    assert loaded["agent"]["model"] == "custom/model"
    assert loaded["watchdog"] == state_store.DEFAULT_CONFIG["watchdog"]


def test_update_config_is_all_or_nothing_and_coerces_values(state_paths) -> None:
    ok, errors, config = state_store.update_config(
        {"agent.max_steps": "7", "agent.require_approval": "true"}
    )
    assert ok is True and errors == []
    assert config["agent"]["max_steps"] == 7
    assert config["agent"]["require_approval"] is True

    before = state_store.CONFIG_YAML.read_text(encoding="utf-8")
    ok, errors, _ = state_store.update_config(
        {"agent.max_steps": 9999, "kernel.port": 1}
    )
    assert ok is False
    assert len(errors) == 2
    assert state_store.CONFIG_YAML.read_text(encoding="utf-8") == before


def test_parse_env_blob_handles_comments_quotes_and_semicolons() -> None:
    assert state_store._parse_env_blob('A=1; B="two"\n# comment\nC=three') == {
        "A": "1",
        "B": "two",
        "C": "three",
    }


def test_stripped_env_removes_declared_and_well_known_secrets(state_paths, monkeypatch) -> None:
    state_store.SECRETS_ENV.write_text("CUSTOM_SECRET=value\n", encoding="utf-8")
    base = {
        "CUSTOM_SECRET": "value",
        "ANTHROPIC_API_KEY": "provider",
        "QUINE_SECRET_KEY": "master",
        "PATH": "safe",
    }
    stripped = state_store.stripped_env(base)
    assert stripped == {"PATH": "safe"}


def test_atomic_write_replaces_complete_document(tmp_path) -> None:
    path = tmp_path / "data.json"
    path.write_text("old", encoding="utf-8")
    state_store.atomic_write_text(path, json.dumps({"ok": True}))
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
    assert not path.with_name("data.json.tmp").exists()
