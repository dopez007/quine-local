"""Spend metering and an optional monthly budget circuit-breaker (P2.4).

Every model call funnels through kernel/llm.py — the one primitive — so usage recording and cap
enforcement cannot be bypassed by mutable app code. The kernel stores the current calendar month's
UTC spend in a small state JSON file. KERNEL_SPEND_CAP_USD sets the optional ceiling; 0 or unset
means unlimited.

The check runs before a call against spend already recorded. The call that crosses the cap still
completes because its cost is unknown up front; the next call is blocked. Read and write errors
fail open: metering is a spend safeguard, not a security boundary.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading

from kernel import state_store

# Redirectable in tests (mirrors the state_store path-constant idiom).
SPEND_JSON = state_store.STATE_DIR / "spend.json"

_LOCK = threading.Lock()


class BudgetExceeded(RuntimeError):
    """Raised by the model primitive when the monthly spend cap is reached."""


def cap_usd() -> float:
    """The monthly spend ceiling in USD (0 = unlimited)."""
    try:
        return max(0.0, float(os.environ.get("KERNEL_SPEND_CAP_USD", "0") or 0))
    except (TypeError, ValueError):
        return 0.0


def _period() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m")


def _read() -> dict:
    """Load this month's counters, resetting transparently when the month rolls over."""
    try:
        data = json.loads(SPEND_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if data.get("period") != _period():
        return {"period": _period(), "spend_usd": 0.0, "calls": 0}
    return {"period": data["period"],
            "spend_usd": float(data.get("spend_usd") or 0.0),
            "calls": int(data.get("calls") or 0)}


def _write(data: dict) -> None:
    try:
        state_store.atomic_write_text(SPEND_JSON, json.dumps(data))
    except OSError:
        pass  # best-effort: a metering write must never break a model call


def current_spend() -> float:
    """USD recorded so far this calendar month."""
    return _read()["spend_usd"]


def remaining() -> float | None:
    """USD left under the cap this month, or None when unlimited."""
    cap = cap_usd()
    if cap <= 0:
        return None
    return max(0.0, cap - current_spend())


def assert_within_budget() -> None:
    """Raise BudgetExceeded if this month's recorded spend has reached the cap.

    No-op when unlimited or when the meter cannot be read (fail-open on a transient glitch).
    """
    cap = cap_usd()
    if cap <= 0:
        return
    try:
        spent = current_spend()
    except Exception:
        return  # fail-open on read errors
    if spent >= cap:
        raise BudgetExceeded(
            f"monthly spend cap reached: ${spent:.4f} of ${cap:.2f} used this month")


def record(cost_usd: float) -> float:
    """Add `cost_usd` to this month's running total and return the new total. Never raises."""
    if not cost_usd or cost_usd < 0:
        cost_usd = max(0.0, cost_usd or 0.0)
    with _LOCK:
        data = _read()
        data["spend_usd"] = round(data["spend_usd"] + float(cost_usd), 8)
        data["calls"] += 1
        _write(data)
        return data["spend_usd"]


def cost_of_response(model: str, resp: dict | None) -> float:
    """Best-effort USD cost of one completion, via LiteLLM's pricing tables. 0.0 if it can't be
    determined (unknown model price, missing usage) — we'd rather under-count than crash."""
    if not resp:
        return 0.0
    # LiteLLM may stamp the computed cost on the response; prefer it when present.
    hidden = resp.get("_hidden_params") if isinstance(resp, dict) else None
    if isinstance(hidden, dict) and hidden.get("response_cost") is not None:
        try:
            return max(0.0, float(hidden["response_cost"]))
        except (TypeError, ValueError):
            pass
    usage = resp.get("usage") if isinstance(resp, dict) else None
    if not isinstance(usage, dict):
        return 0.0
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    try:
        import litellm
        prompt_cost, completion_cost = litellm.cost_per_token(
            model=model, prompt_tokens=prompt, completion_tokens=completion)
        return max(0.0, float(prompt_cost) + float(completion_cost))
    except Exception:
        return 0.0


def record_response(model: str, resp: dict | None) -> float:
    """Compute one completion's cost and add it to the month's total. Returns the cost recorded."""
    cost = cost_of_response(model, resp)
    if cost:
        record(cost)
    return cost


def snapshot() -> dict:
    """Current budget state: period, spend, cap, remaining, and call count."""
    data = _read()
    cap = cap_usd()
    return {"period": data["period"], "spend_usd": round(data["spend_usd"], 6),
            "calls": data["calls"], "cap_usd": cap,
            "remaining_usd": remaining(), "unlimited": cap <= 0}
