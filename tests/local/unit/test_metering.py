"""Spend metering is bounded, monthly, and fail-open on storage faults."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from kernel import metering

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_meter(tmp_path, monkeypatch):
    monkeypatch.setattr(metering, "SPEND_JSON", tmp_path / "spend.json")
    monkeypatch.setattr(metering, "_period", lambda: "2026-08")
    monkeypatch.delenv("KERNEL_SPEND_CAP_USD", raising=False)


def test_invalid_or_missing_cap_is_unlimited(monkeypatch) -> None:
    assert metering.cap_usd() == 0.0
    assert metering.remaining() is None
    monkeypatch.setenv("KERNEL_SPEND_CAP_USD", "bad")
    assert metering.cap_usd() == 0.0


def test_record_accumulates_nonnegative_cost_and_calls() -> None:
    assert metering.record(0.25) == 0.25
    assert metering.record(0.5) == 0.75
    assert metering.record(-2) == 0.75
    assert metering.snapshot()["calls"] == 3


def test_period_mismatch_resets_stale_data(tmp_path) -> None:
    metering.SPEND_JSON.write_text(
        json.dumps({"period": "2026-07", "spend_usd": 99, "calls": 2}), encoding="utf-8"
    )
    assert metering.current_spend() == 0.0
    assert metering.snapshot()["period"] == "2026-08"


def test_budget_blocks_only_after_recorded_spend_reaches_cap(monkeypatch) -> None:
    monkeypatch.setenv("KERNEL_SPEND_CAP_USD", "1")
    metering.record(1.2)
    assert metering.remaining() == 0.0
    with pytest.raises(metering.BudgetExceeded, match="monthly spend cap reached"):
        metering.assert_within_budget()


def test_hidden_response_cost_is_preferred_and_clamped() -> None:
    assert metering.cost_of_response("model", {"_hidden_params": {"response_cost": "0.12"}}) == 0.12
    assert metering.cost_of_response("model", {"_hidden_params": {"response_cost": -2}}) == 0.0
    assert metering.cost_of_response("model", None) == 0.0


def test_record_response_ignores_unknown_cost(monkeypatch) -> None:
    def unknown_cost(**_kwargs):
        raise ValueError("unknown model")

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(cost_per_token=unknown_cost))
    assert metering.record_response("unknown", {"usage": {}}) == 0.0
    assert metering.snapshot()["calls"] == 0
