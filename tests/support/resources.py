"""Resource budgets for the serial Quine test lanes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LaneBudget:
    max_rss_mb: int
    max_children: int
    timeout_seconds: int


# On Windows a virtual-environment launcher starts the real interpreter as one child.
# Budgets therefore allow that single baseline child; any additional quick/contract child fails.
BUDGETS = {
    "quick": LaneBudget(max_rss_mb=256, max_children=1, timeout_seconds=90),
    "contract": LaneBudget(max_rss_mb=384, max_children=1, timeout_seconds=180),
    "integration": LaneBudget(max_rss_mb=512, max_children=2, timeout_seconds=600),
    "system": LaneBudget(max_rss_mb=1024, max_children=9, timeout_seconds=1200),
}
