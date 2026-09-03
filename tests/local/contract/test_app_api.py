"""Local app contracts run in-process without uvicorn or sockets."""

from __future__ import annotations

import pathlib
import sys

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import main as app_main  # noqa: E402

pytestmark = pytest.mark.contract


@pytest.fixture(autouse=True)
def isolated_app_data(tmp_path, monkeypatch) -> pathlib.Path:
    data = tmp_path / "data"
    monkeypatch.setattr(app_main, "DATA_DIR", data)
    monkeypatch.setattr(app_main, "CONVO_DIR", data / "conversations")
    monkeypatch.setattr(app_main, "NOTES_DIR", data / "notes")
    monkeypatch.setattr(app_main, "DEV_DIR", data / "development")
    monkeypatch.setattr(app_main, "CONFIG_PATH", data / "backend_config.json")
    monkeypatch.setattr(app_main, "SETTINGS_PATH", data / "settings.json")
    app_main._RUN_HUBS.clear()
    return data


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_main.app),
        base_url="http://quine.test",
    )


@pytest.mark.asyncio
async def test_health_and_info_are_available_without_external_services(client) -> None:
    async with client:
        health = await client.get("/health")
        info = await client.get("/api/app/info")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert info.status_code == 200
    assert info.json()["build"] == app_main.APP_BUILD


@pytest.mark.asyncio
async def test_settings_roundtrip_redacts_secret_and_keeps_blank_existing_value(client) -> None:
    async with client:
        stored = await client.put(
            "/api/settings/provider",
            json={"api_key": "secret-value", "model": "demo"},
        )
        kept = await client.put(
            "/api/settings/provider",
            json={"api_key": "", "model": "next"},
        )
        loaded = await client.get("/api/settings/provider")
        deleted = await client.delete("/api/settings/provider")
    assert stored.json()["settings"] == {"api_key": "••••••", "model": "demo"}
    assert kept.json()["settings"] == {"api_key": "••••••", "model": "next"}
    assert loaded.json()["settings"] == {"api_key": "••••••", "model": "next"}
    assert deleted.json() == {"ok": True}


@pytest.mark.asyncio
async def test_conversation_create_and_list_use_isolated_data(client) -> None:
    async with client:
        created = await client.post("/api/agent/conversations")
        listed = await client.get("/api/agent/conversations")
    assert created.status_code == 200
    item = created.json()
    assert item["id"].startswith("c")
    assert listed.json()["conversations"][0]["id"] == item["id"]
