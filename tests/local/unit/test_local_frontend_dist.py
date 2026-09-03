"""Committed local UI assets keep their relative deployment contract."""

from __future__ import annotations

import pathlib

import pytest

from tests.support.frontend_dist import generated_references, parse_frontend_html

ROOT = pathlib.Path(__file__).resolve().parents[3]
FRONTEND = ROOT / "app" / "frontend"
DIST = FRONTEND / "dist"

pytestmark = pytest.mark.unit


def test_app_frontend_dist_uses_relative_existing_assets() -> None:
    index = DIST / "index.html"
    assert index.is_file()

    text = index.read_text(encoding="utf-8")
    references = generated_references(parse_frontend_html(index))
    assert references
    assert "/src/" not in text

    for reference in references:
        assert reference.startswith("./assets/"), reference
        assert not reference.startswith("/assets/"), reference
        asset = DIST / "assets" / reference.removeprefix("./assets/")
        assert asset.is_file(), reference
        if asset.suffix == ".js":
            assert b"||=" not in asset.read_bytes(), reference


def test_app_frontend_keeps_vite5_browser_targets() -> None:
    config = (FRONTEND / "vite.config.js").read_text(encoding="utf-8")
    for target in ("es2020", "chrome87", "edge88", "firefox78", "safari14"):
        assert f'"{target}"' in config
