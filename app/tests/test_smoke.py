"""Smoke tests — part of POST. The kernel runs these in staging before committing a
new version, and they double as a sanity check on the running app."""

from fastapi.testclient import TestClient

import main


def test_health_ok():
    client = TestClient(main.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_index_renders():
    client = TestClient(main.app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Quine" in resp.text


def test_error_tracker_answers():
    client = TestClient(main.app)
    resp = client.get("/api/errors")
    assert resp.status_code == 200
    assert "groups" in resp.json()
