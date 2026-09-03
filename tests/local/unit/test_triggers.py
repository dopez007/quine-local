"""Pure autonomous-trigger rules use injected timestamps and bounded data."""

from __future__ import annotations

import datetime as dt

import pytest

from kernel import triggers

pytestmark = pytest.mark.unit


def test_webhook_signature_roundtrip_and_missing_secret_fail_closed() -> None:
    signature = triggers.sign("secret", b"body")
    assert triggers.verify_webhook("secret", b"body", signature) is True
    assert triggers.verify_webhook("secret", b"changed", signature) is False
    assert triggers.verify_webhook("", b"body", signature) is False
    assert triggers.verify_webhook("secret", b"body", None) is False


def test_interval_schedule_fires_initially_then_waits_for_interval() -> None:
    trigger = {"config": {"interval_minutes": 5}, "last_fired": None}
    assert triggers.schedule_due(trigger, 1000) is True
    trigger["last_fired"] = 1000
    assert triggers.schedule_due(trigger, 1299) is False
    assert triggers.schedule_due(trigger, 1300) is True


def test_daily_schedule_fires_once_after_target() -> None:
    now = dt.datetime(2026, 8, 22, 12, 0, tzinfo=dt.timezone.utc).timestamp()
    trigger = {"config": {"daily_at": "11:30"}, "last_fired": None}
    assert triggers.schedule_due(trigger, now) is True
    trigger["last_fired"] = now
    assert triggers.schedule_due(trigger, now + 60) is False


def test_spike_hits_filters_stale_resolved_and_cooled_groups() -> None:
    now = 10_000.0
    trigger = {
        "config": {"threshold": 3, "window_minutes": 10, "cooldown_hours": 1},
        "fired_fingerprints": {"cooled": now - 100},
    }
    groups = [
        {"fingerprint": "hit", "count": 3, "last_ts": now, "resolved": False},
        {"fingerprint": "small", "count": 2, "last_ts": now, "resolved": False},
        {"fingerprint": "stale", "count": 9, "last_ts": 0, "resolved": False},
        {"fingerprint": "done", "count": 9, "last_ts": now, "resolved": True},
        {"fingerprint": "cooled", "count": 9, "last_ts": now, "resolved": False},
        "bad",
    ]
    assert [group["fingerprint"] for group in triggers.spike_hits(trigger, groups, now)] == [
        "hit"
    ]


def test_advisor_hits_respects_cap_prompt_and_cooldown() -> None:
    trigger = {
        "kind": "advisor",
        "config": {"max_per_tick": 1, "cooldown_hours": 1},
        "fired_fingerprints": {"old": 9_900.0},
    }
    proposals = [
        {"id": "old", "prompt": "retry"},
        {"id": "empty", "prompt": ""},
        {"id": "new", "prompt": "fix it"},
        {"id": "later", "prompt": "later"},
    ]
    assert triggers.advisor_hits(trigger, proposals, 10_000.0) == [proposals[2]]


def test_render_template_preserves_unknown_and_bounds_payload() -> None:
    rendered = triggers.render_template(
        "{{ trigger.name }} {{ error.message }} {{ payload }} {{ unknown }}",
        trigger={"name": "nightly"},
        error={"message": "boom"},
        payload="x" * 20_000,
    )
    assert rendered.startswith("nightly boom ")
    assert "{{ unknown }}" in rendered
    assert len(rendered) < 17_000


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({}, "name must be"),
        ({"name": "ok", "kind": "bad", "prompt_template": "x"}, "kind must be"),
        (
            {"name": "ok", "kind": "schedule", "prompt_template": "x", "config": {}},
            "schedule needs",
        ),
        (
            {
                "name": "ok",
                "kind": "advisor",
                "prompt_template": "x",
                "config": {"max_per_tick": 0},
            },
            "max_per_tick",
        ),
    ],
)
def test_validate_trigger_rejects_invalid_specs(spec: dict, message: str) -> None:
    ok, error = triggers.validate_trigger(spec)
    assert ok is False
    assert message in error


def test_validate_trigger_accepts_schedule() -> None:
    spec = {
        "name": "nightly",
        "kind": "schedule",
        "prompt_template": "run",
        "config": {"daily_at": "03:30", "once": True},
    }
    assert triggers.validate_trigger(spec) == (True, "")
