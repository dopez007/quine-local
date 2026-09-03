"""Version registry keeps stable identifiers and explicit status history."""

from __future__ import annotations

import copy

import pytest

from kernel import registry

pytestmark = pytest.mark.unit


@pytest.fixture
def memory_registry(monkeypatch):
    data = {"next_seq": 1, "versions": {}}

    def read():
        return copy.deepcopy(data)

    def write(value):
        data.clear()
        data.update(copy.deepcopy(value))

    monkeypatch.setattr(registry.state_store, "read_registry", read)
    monkeypatch.setattr(registry.state_store, "write_registry", write)
    monkeypatch.setattr(registry, "_now", lambda: 1000.0)
    return data


def test_record_commit_is_monotonic_and_idempotent(memory_registry) -> None:
    first = registry.record_commit("a" * 40, parent=None, message="first", prompt="p" * 500)
    second = registry.record_commit("b" * 40, parent=first["sha"], message="second")
    repeated = registry.record_commit("a" * 40, parent=None, message="changed")
    assert (first["seq"], second["seq"]) == (1, 2)
    assert len(first["prompt"]) == 300
    assert repeated["message"] == "first"


def test_status_transition_updates_history_and_edges(memory_registry) -> None:
    sha = "a" * 40
    registry.record_commit(sha, parent=None, message="first")
    registry.set_status(sha, "reverted", by="b" * 40, verification={"ok": True})
    entry = registry.get(sha)
    assert entry is not None
    assert entry["status"] == "reverted"
    assert entry["reverted_by"] == "b" * 40
    assert entry["verification"] == {"ok": True}
    assert [item["status"] for item in entry["history"]] == ["committed", "reverted"]


def test_labels_are_unique_and_references_resolve(memory_registry) -> None:
    a, b = "a" * 40, "b" * 40
    registry.record_commit(a, parent=None, message="a")
    registry.record_commit(b, parent=a, message="b")
    assert registry.set_label(a, "stable") == (True, "stable")
    assert registry.set_label(b, "stable")[0] is False
    assert registry.lookup("stable") == a
    assert registry.lookup("v2") == b
    assert registry.lookup(a[:8]) == a
    assert registry.lookup("missing") is None


def test_reconcile_backfills_prunes_and_repairs_status(memory_registry) -> None:
    registry.record_commit("deadbeef" * 5, parent=None, message="stale")
    commits = [
        {"sha": "a" * 40, "parent": None, "message": "a", "date": "2026-01-01T00:00:00+00:00"},
        {"sha": "b" * 40, "parent": "a" * 40, "message": "b", "date": "2026-01-02T00:00:00+00:00"},
    ]
    result = registry.reconcile(commits, {"a" * 40}, {"b" * 40})
    assert result["added"] == 2
    assert result["pruned"] == 1
    promoted = registry.get("a" * 40)
    pending = registry.get("b" * 40)
    assert promoted is not None and promoted["status"] == "promoted"
    assert pending is not None and pending["status"] == "pending"
