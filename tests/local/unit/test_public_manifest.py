"""Integrity contract for a generated public release tree."""

from __future__ import annotations

import pathlib

import pytest

from tests.support.public_manifest import verify_public_manifest

ROOT = pathlib.Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "PUBLIC-MANIFEST.json"
PRIVATE_EXPORTER = ROOT / "scripts" / "package_public.py"

pytestmark = pytest.mark.unit


def test_public_manifest_matches_checked_out_files() -> None:
    if not MANIFEST.is_file():
        if PRIVATE_EXPORTER.is_file():
            pytest.skip("public manifest exists only in generated release trees")
        pytest.fail("generated public release is missing PUBLIC-MANIFEST.json")

    errors = verify_public_manifest(ROOT)
    assert not errors, "\n".join(errors)
