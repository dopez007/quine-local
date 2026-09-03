"""Operator authorization (opt-in): split the HUMAN from the mutable app at the gateway.

Why: every /api/syscall/* is reachable from ring 3 — the app process holds the edge token
for its own loopback calls — so promotion authority (approve/reject), the autonomy
switches (POST /config), and the trigger/check/eval stores were only *behaviorally*
off-limits to the self-modifying agent. With `operator_auth.enabled`, those syscalls
require a credential the app process never holds: the operator key
(`state/operator_key`, protected state, absent from every app environment), presented
either as an `X-Operator-Key` header (CLI / automation) or via a short-lived HttpOnly
browser session minted at the kernel-served `/operator` page (ring-0 HTML — deliberately
NOT app-authored, so the unlock flow can't be rewritten by a self-mod).

Residual risk, accepted deliberately: while an operator session is live in a browser,
app-served JS on the same origin can still *trigger* gated requests riding the HttpOnly
cookie (it can never read it). The gate's primary guarantees are therefore:
  • the APP PROCESS can never act as the operator (no key, no cookie), and
  • unattended change requests can never self-promote — a change_request submitted
    without operator credentials always HOLDS for approval (core._should_hold), the
    enforcement half of the trigger-rails posture.
Sessions are in-memory (a kernel restart re-locks — fail safe) and every unlock, lock,
and denial is audited.
"""

from __future__ import annotations

import hmac
import secrets
import time

from kernel import state_store

COOKIE = "quine_operator"
HEADER = "x-operator-key"

_sessions: dict[str, float] = {}  # session token → expiry epoch (kernel-process memory)


def _cfg(config: dict) -> dict:
    return config.get("operator_auth", {}) or {}


def enabled(config: dict) -> bool:
    return bool(_cfg(config).get("enabled", False))


def session_hours(config: dict) -> float:
    return float(_cfg(config).get("session_hours", 12))


def _prune(now: float) -> None:
    for tok, exp in list(_sessions.items()):
        if exp <= now:
            del _sessions[tok]


def unlock(presented: str, config: dict) -> str | None:
    """Exchange the operator key for a session token (None on a bad key). Audited."""
    key = state_store.ensure_operator_key()
    # Bytes compare: a non-ASCII candidate key must be a clean denial, not a TypeError.
    if not presented or not hmac.compare_digest(presented.strip().encode("utf-8"),
                                                key.encode("utf-8")):
        state_store.audit("operator_unlock_denied")
        return None
    now = time.time()
    _prune(now)
    token = secrets.token_urlsafe(32)
    _sessions[token] = now + session_hours(config) * 3600
    state_store.audit("operator_unlocked", sessions=len(_sessions))
    return token


def lock(token: str | None) -> None:
    if token and _sessions.pop(token, None) is not None:
        state_store.audit("operator_locked")


def session_valid(token: str | None) -> bool:
    if not token:
        return False
    now = time.time()
    _prune(now)
    return token in _sessions


def verify_request(request, config: dict) -> bool:
    """Whether a request carries operator authority. Always True while the gate is off
    (opt-in, fail-open by design for local/dev — mirrors the edge-auth posture)."""
    if not enabled(config):
        return True
    presented = request.headers.get(HEADER)
    # Bytes compare: a non-ASCII header value must be a clean denial, not a TypeError 500.
    if presented and hmac.compare_digest(presented.strip().encode("utf-8"),
                                         state_store.ensure_operator_key().encode("utf-8")):
        return True
    return session_valid(request.cookies.get(COOKIE))
