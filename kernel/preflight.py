"""Boot-time provider-key preflight (ring-0 mechanism, not a feature).

Probes the configured model's credentials ONCE at kernel start so a dead/expired/typo'd key is
caught before the harness serves traffic — instead of silently surfacing as a 502 on the first
real request (see kernel.gateway's llm_call handler). This is the availability counterpart to
`state_store.enforce_secret_hardening`'s security preflight.

Failure policy: block only on a DEFINITIVE auth rejection (a genuinely bad key); a transient or
undeterminable error (network, rate-limit, timeout, unroutable model) warns and boots, so a
provider blip can't brick a live deploy. No-op for the scripted (keyless) engine, which keeps the
offline test suite and local scripted dev completely unaffected.
"""

from __future__ import annotations

import os

from kernel import llm


def _disabled() -> bool:
    """Opt-out escape hatch, mirroring the KERNEL_REQUIRE_AUTH / QUINE_KERNEL_HARDENED switches."""
    return os.environ.get("KERNEL_VALIDATE_KEYS", "").strip().lower() in ("0", "false", "no", "off")


async def check_provider_key(cfg: dict) -> tuple[str, str | None]:
    """Return (status, detail) where status is "skip" | "valid" | "invalid" | "unknown".

    Skips WITHOUT any provider call when the active engine is scripted, when validation is
    disabled (KERNEL_VALIDATE_KEYS=0), or when no model is configured — so callers that gate boot
    on the result never touch the network in the offline/keyless paths.
    """
    agent = cfg.get("agent", {}) if isinstance(cfg, dict) else {}
    if agent.get("engine") == "scripted" or _disabled():
        return "skip", None
    model = (agent.get("model") or "").strip()
    if not model:
        return "skip", None
    res = await llm.probe(model)
    return res.get("status", "unknown"), res.get("error")
