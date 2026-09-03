"""The low-memory harness blocks accidental external effects."""

from __future__ import annotations

import socket
import subprocess

import pytest

from tests.support.fakes import FakeClock

pytestmark = pytest.mark.unit


def test_fake_clock_advances_without_sleeping() -> None:
    clock = FakeClock(now_value=10.0)
    assert clock.now() == 10.0


def test_unit_lane_blocks_process_and_network_calls() -> None:
    with pytest.raises(AssertionError, match="subprocess.run is forbidden"):
        subprocess.run(["python", "--version"], check=False)
    with pytest.raises(AssertionError, match="socket.create_connection is forbidden"):
        socket.create_connection(("203.0.113.1", 9))
