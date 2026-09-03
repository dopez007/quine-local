"""The ONE model primitive: a thin LiteLLM passthrough using kernel-held keys.

Used by (a) the agent runtime to drive self-modification, and (b) the `llm_call`
syscall so agent-built app features can use models WITHOUT ever holding provider keys.
Everything above this — subagents, RAG, routing — is built by the agent, not here.
"""

from __future__ import annotations

import functools
from typing import Any

import litellm

from kernel import metering

# Be forgiving of provider-specific parameter differences, and quiet the banner.
litellm.drop_params = True
litellm.suppress_debug_info = True

# A DEFINITIVE credential rejection (as opposed to a transient/network failure) — built
# defensively so a litellm version that renames/drops one of these can't break the probe;
# an empty tuple simply never matches (everything degrades to "unknown"/warn). Used by probe().
_AUTH_ERRORS: tuple[type[BaseException], ...] = tuple(
    e for e in (getattr(litellm, "AuthenticationError", None),
                getattr(litellm, "PermissionDeniedError", None))
    if isinstance(e, type)
)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


@functools.lru_cache(maxsize=1)
def provider_catalog() -> list[dict]:
    """litellm's provider→models catalog for the Settings agent/model picker — just provider
    names + their known model ids (NO secrets; keys live in secrets.env). Common chat providers
    first, then the long tail alphabetically. Cached for the process."""
    by_provider: dict = getattr(litellm, "models_by_provider", {}) or {}
    names: set[str] = set()
    for p in getattr(litellm, "provider_list", []) or []:
        names.add(p.value if hasattr(p, "value") else str(p))
    names.update(by_provider.keys())
    preferred = ("anthropic", "openai", "gemini", "deepseek", "groq", "mistral", "xai",
                 "openrouter", "together_ai", "cohere", "ollama")
    rank = {n: i for i, n in enumerate(preferred)}
    ordered = sorted(names, key=lambda n: (rank.get(n, len(preferred)), n))
    return [{"name": n, "models": sorted(by_provider.get(n) or [])} for n in ordered]


async def chat(model: str, messages: list[dict], tools: list[dict] | None = None, **kw: Any) -> dict:
    # Spend cap (P2.4): block once this month's recorded spend hits the configured budget, then
    # record this call's cost. Enforced here — the one primitive — so app/agent code cannot bypass.
    metering.assert_within_budget()
    resp: Any = await litellm.acompletion(model=model, messages=messages, tools=tools, **kw)
    data = resp.model_dump()
    metering.record_response(model, data)
    return data


async def probe(model: str) -> dict:
    """One-shot credential/connectivity check for `model` using the kernel-held key.

    Returns {"status": "valid"|"invalid"|"unknown", "error": str|None}. `invalid` means the
    provider DEFINITIVELY rejected the credentials (auth/permission) — a bad or expired key, so
    callers block. Any other failure (network, timeout, rate-limit, unroutable model id) is
    `unknown`: the key may be fine, so callers warn rather than block. Deliberately bypasses
    metering — a preflight probe should not count against the configured spend budget."""
    try:
        await litellm.acompletion(
            model=model, messages=[{"role": "user", "content": "ping"}], max_tokens=1)
        return {"status": "valid", "error": None}
    except Exception as exc:  # noqa: BLE001 — classify, never propagate, from a preflight probe
        status = "invalid" if (_AUTH_ERRORS and isinstance(exc, _AUTH_ERRORS)) else "unknown"
        return {"status": status, "error": f"{type(exc).__name__}: {exc}"}


async def embed(model: str, inputs: Any) -> dict:
    metering.assert_within_budget()
    resp = await litellm.aembedding(model=model, input=inputs)
    data = resp.model_dump()
    metering.record_response(model, data)
    return data


async def chat_stream(model: str, messages: list[dict], tools: list[dict] | None = None, **kw: Any):
    """Streamed counterpart of chat(): yields normalized delta chunks
    {"text": str|None, "tool_calls": [...], "finish_reason": str|None} as they arrive.

    Tool-call deltas carry an `index` so the caller can accumulate fragmented
    arguments across chunks (OpenAI/LiteLLM streaming semantics)."""
    metering.assert_within_budget()  # spend cap (P2.4) — see chat()
    kw.setdefault("stream_options", {"include_usage": True})
    response: Any = await litellm.acompletion(
        model=model, messages=messages, tools=tools, stream=True, **kw
    )
    usage = None
    async for chunk in response:
        u = getattr(chunk, "usage", None)
        if u is not None:
            try:
                usage = u.model_dump() if hasattr(u, "model_dump") else dict(u)
            except Exception:
                usage = None
        choices = _get(chunk, "choices", []) or []
        if not choices:
            continue
        choice = choices[0]
        delta = _get(choice, "delta")

        text = _get(delta, "content")
        # Reasoning/extended-thinking deltas (DeepSeek `reasoning_content`, others
        # `reasoning`) so the app can stream the model's thoughts, not just its answer.
        reasoning = None
        if delta is not None:
            reasoning = _get(delta, "reasoning_content") or _get(delta, "reasoning")
        tool_calls = []
        for tc in (_get(delta, "tool_calls", []) or []) if delta else []:
            fn = _get(tc, "function")
            tool_calls.append({
                "index": _get(tc, "index", 0),
                "id": _get(tc, "id"),
                "name": _get(fn, "name") if fn else None,
                "arguments": _get(fn, "arguments") if fn else None,
            })
        yield {
            "text": text,
            "reasoning": reasoning,
            "tool_calls": tool_calls,
            "finish_reason": _get(choice, "finish_reason"),
        }
    if usage is not None:
        metering.record_response(model, {"usage": usage})  # record the stream's tokens (P2.4)
        yield {"usage": usage}  # final event exposes token usage for logging/analysis
