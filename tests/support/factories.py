"""Test data factories with no external effects."""

from __future__ import annotations

import copy
from typing import Any


def scripted_config(default: dict[str, Any]) -> dict[str, Any]:
    config = copy.deepcopy(default)
    config["agent"]["engine"] = "scripted"
    config["agent"]["model"] = "scripted"
    config["watchdog"]["health_poll_interval"] = 0.01
    config["watchdog"]["health_timeout_seconds"] = 1
    return config
