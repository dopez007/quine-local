"""Acceptance-check derivation remains explicit and fail-closed shaped."""

from __future__ import annotations

import pytest

from kernel import verifier

pytestmark = pytest.mark.contract


def _check(name: str = "health") -> dict:
    return {
        "name": name,
        "steps": [{"method": "GET", "path": "/health", "expect": {"status": 200}}],
    }


def test_config_helpers_read_verifier_section_and_agent_fallback() -> None:
    config = {
        "agent": {"engine": "litellm", "model": "provider/default"},
        "verifier": {"enabled": True, "strict": True, "timeout_seconds": 9},
    }
    assert verifier.enabled(config) is True
    assert verifier.strict(config) is True
    assert verifier.deadline(config) == 9
    assert verifier._model(config) == "provider/default"


def test_extract_json_tolerates_fence_and_prose() -> None:
    assert verifier._extract_json("result:\n```json\n{\"ok\": true}\n```") == {"ok": True}
    with pytest.raises(ValueError, match="no JSON"):
        verifier._extract_json("no structured result")


@pytest.mark.asyncio
async def test_scripted_prompt_without_marker_is_skipped() -> None:
    result = await verifier.derive_checks("refactor comments", "", {"agent": {"engine": "scripted"}})
    assert result == {
        "ok": True,
        "checks": [],
        "skipped": True,
        "reason": "no verify marker",
    }


@pytest.mark.asyncio
async def test_scripted_marker_yields_valid_check() -> None:
    prompt = f"change it\n__VERIFY_CHECK__ {_check()}".replace("'", '"').replace("True", "true")
    result = await verifier.derive_checks(prompt, "", {"agent": {"engine": "scripted"}})
    assert result["ok"] is True
    assert result["skipped"] is False
    assert result["checks"][0]["name"] == "health"


@pytest.mark.asyncio
async def test_scripted_bad_or_invalid_marker_is_a_derivation_failure() -> None:
    bad = await verifier.derive_checks(
        "__VERIFY_CHECK__ not-json", "", {"agent": {"engine": "scripted"}}
    )
    assert bad["ok"] is False
    invalid = await verifier.derive_checks(
        '__VERIFY_CHECK__ {"name":"x","steps":[]}',
        "",
        {"agent": {"engine": "scripted"}},
    )
    assert invalid["ok"] is False
    assert "invalid check spec" in invalid["reason"]


@pytest.mark.asyncio
async def test_scripted_markers_are_capped() -> None:
    import json

    prompt = "\n".join(
        f"__VERIFY_CHECK__ {json.dumps(_check(str(index)))}" for index in range(4)
    )
    result = await verifier.derive_checks(
        prompt,
        "",
        {"agent": {"engine": "scripted"}, "verifier": {"max_checks": 2}},
    )
    assert [item["name"] for item in result["checks"]] == ["0", "1"]


@pytest.mark.asyncio
async def test_verify_candidate_short_circuits_when_no_checks(monkeypatch) -> None:
    monkeypatch.setattr(verifier.checks, "active_checks", lambda _excluded: [])
    result = await verifier.verify_candidate(1234, [], {"agent": {"engine": "scripted"}})
    assert result == {"ok": True, "total": 0, "passed": 0, "results": [], "failed": []}
