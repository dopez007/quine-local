"""Kernel gateway authorization is verified in-process without booting an app."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from kernel import gateway

pytestmark = pytest.mark.contract


@pytest.fixture
def client() -> httpx.AsyncClient:
    gateway.app.state.kernel = SimpleNamespace(config={})
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app),
        base_url="http://gateway.test",
    )


@pytest.mark.asyncio
async def test_edge_token_protects_kernel_pages_and_accepts_header_or_query(client, monkeypatch) -> None:
    monkeypatch.setenv(gateway.KERNEL_AUTH_TOKEN_ENV, "edge-secret")
    async with client:
        denied = await client.get("/operator")
        header = await client.get("/operator", headers={"authorization": "Bearer edge-secret"})
        query = await client.get("/operator?token=edge-secret")
    assert denied.status_code == 401
    assert denied.json()["error"] == "unauthorized"
    assert header.status_code == 200
    assert query.status_code == 200


@pytest.mark.asyncio
async def test_edge_gate_is_open_when_token_is_unset(client, monkeypatch) -> None:
    monkeypatch.delenv(gateway.KERNEL_AUTH_TOKEN_ENV, raising=False)
    async with client:
        response = await client.get("/operator")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_operator_gate_blocks_privileged_post_before_handler(client, monkeypatch) -> None:
    monkeypatch.delenv(gateway.KERNEL_AUTH_TOKEN_ENV, raising=False)
    gateway.app.state.kernel = SimpleNamespace(config={"operator_auth": {"enabled": True}})
    monkeypatch.setattr(gateway.opauth, "verify_request", lambda _request, _config: False)
    async with client:
        response = await client.post("/api/syscall/config", json={"agent.max_steps": 7})
    assert response.status_code == 403
    assert "operator authorization required" in response.json()["reason"]
