"""Fail-closed, serial fixtures for the low-memory Quine test suite."""

from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Generator

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_PRESERVED_STATE_HOME = (
    os.environ.get("QUINE_STATE_HOME") if os.environ.get("QUINE_KERNEL_VALIDATION") else None
)
_OWNS_SESSION_HOME = not bool(_PRESERVED_STATE_HOME)
_SESSION_HOME = (
    pathlib.Path(tempfile.mkdtemp(prefix="quine-lowmem-"))
    if _OWNS_SESSION_HOME
    else pathlib.Path(_PRESERVED_STATE_HOME).resolve()
)
_STATE_HOME = _SESSION_HOME / "state-home" if _OWNS_SESSION_HOME else _SESSION_HOME
_SESSION_STAGING = _SESSION_HOME / "staging"
_SESSION_STAGING.mkdir(parents=True, exist_ok=True)
os.environ["QUINE_STATE_HOME"] = str(_STATE_HOME)
os.environ["QUINE_STAGING_DIR"] = str(_SESSION_STAGING)
os.environ["QUINE_DATA_DIR"] = str(_SESSION_HOME / "data")
os.environ["QUINE_SYSCALL_URL"] = "http://quine.invalid/api/syscall"
os.environ.setdefault("NO_COLOR", "1")
os.environ.setdefault("PY_COLORS", "0")
os.environ.pop("FORCE_COLOR", None)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    del session, exitstatus
    if _OWNS_SESSION_HOME:
        shutil.rmtree(_SESSION_HOME, ignore_errors=True)


def _blocked(kind: str):
    def blocker(*_args, **_kwargs):
        raise AssertionError(
            f"{kind} is forbidden in unit/contract tests; move this behavior to an "
            "explicit integration/system lane or inject a fake boundary"
        )

    return blocker


def _is_loopback(address: object) -> bool:
    if not isinstance(address, tuple) or not address:
        return False
    return str(address[0]).strip("[]").lower() in {"127.0.0.1", "::1", "localhost"}


def _guard_connect(original, kind: str):
    def guarded(*args, **kwargs):
        address = args[-1] if args else kwargs.get("address")
        if _is_loopback(address):
            return original(*args, **kwargs)
        return _blocked(kind)(*args, **kwargs)

    return guarded


@pytest.fixture(autouse=True)
def forbid_unmarked_external_effects(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Prevent accidental process and network use outside explicit heavy lanes."""
    heavy_lane = request.node.get_closest_marker("integration") or request.node.get_closest_marker(
        "system"
    )
    allow_process = heavy_lane or request.node.get_closest_marker("allow_process")
    allow_network = heavy_lane or request.node.get_closest_marker("allow_network")

    if not allow_process:
        monkeypatch.setattr(subprocess, "Popen", _blocked("subprocess.Popen"))
        monkeypatch.setattr(subprocess, "run", _blocked("subprocess.run"))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _blocked("asyncio subprocess"))
        monkeypatch.setattr(asyncio, "create_subprocess_shell", _blocked("asyncio subprocess"))
        monkeypatch.setattr(os, "system", _blocked("os.system"))

    if not allow_network:
        monkeypatch.setattr(
            socket.socket,
            "connect",
            _guard_connect(socket.socket.connect, "socket.connect"),
        )
        monkeypatch.setattr(
            socket,
            "create_connection",
            _guard_connect(socket.create_connection, "socket.create_connection"),
        )

    yield


@pytest.fixture
def isolated_home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Give a test an explicit state root before it imports stateful modules."""
    home = tmp_path / "quine-home"
    monkeypatch.setenv("QUINE_STATE_HOME", str(home))
    return home
