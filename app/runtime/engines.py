"""Model engines. Each engine's step(messages, tools) -> (text, calls) where calls is a
list of {"id","name","args"}. ADD ENGINES HERE if you want different model behavior.

  • ScriptedEngine — deterministic, keyless; exercises the full pipeline offline.
  • LiteLLMEngine  — any provider, via the kernel's /llm_call (no keys in this process).
"""

from __future__ import annotations

import json
import re
import uuid

from . import sdk


def _uid() -> str:
    return "call_" + uuid.uuid4().hex[:10]


class ScriptedEngine:
    """Read main.py → patch APP_BUILD + append REQUESTS.md → propose. No model calls."""

    def __init__(self, prompt: str) -> None:
        self.prompt = prompt
        self.i = 0

    def step(self, messages, tools):
        # step(...) -> (text, calls, reasoning, usage) to match LiteLLMEngine; scripted
        # has no real reasoning trace, so reasoning is None and usage is empty.
        self.i += 1
        if self.i == 1:
            return "Reading main.py", [{"id": _uid(), "name": "read_file", "args": {"path": "main.py"}}], None, {}
        if self.i == 2:
            main_txt = (sdk.STAGING / "main.py").read_text(encoding="utf-8")
            if "__GAMEDAY_BREAK__" in self.prompt:
                # Offline test affordance: emit code that fails the pre-commit validation
                # gate, so the full pipeline's safety (reject → don't promote → stay
                # healthy) can be exercised by the named serial gameday system scenario.
                patched = main_txt + "\n\ndef __gameday_broken__( this is not valid python\n"
            else:
                label = "auto: " + " ".join(self.prompt.split())[:40]
                patched = re.sub(r'APP_BUILD = ".*?"', f'APP_BUILD = "{label}"', main_txt, count=1)
            req = sdk.STAGING / "REQUESTS.md"
            existing = req.read_text(encoding="utf-8") if req.exists() else "# Change requests\n"
            return "Applying change", [
                {"id": _uid(), "name": "write_file", "args": {"path": "main.py", "content": patched}},
                {"id": _uid(), "name": "write_file",
                 "args": {"path": "REQUESTS.md", "content": existing + f"\n- {self.prompt}\n"}},
            ], None, {}
        return "Proposing commit", [
            {"id": _uid(), "name": "propose_commit", "args": {"message": "scripted: " + self.prompt[:60]}}
        ], None, {}


class LiteLLMEngine:
    """Drives any provider via the kernel's /llm_call syscall (kernel holds the keys)."""

    def __init__(self, model: str, temperature: float = 0.0) -> None:
        self.model = model
        self.temperature = temperature

    def step(self, messages, tools):
        resp = sdk.llm_call(self.model, messages, tools=tools, temperature=self.temperature)
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "llm_call failed"))
        choice = resp["response"]["choices"][0]
        msg = choice["message"]
        # Extract usage info from the response (OpenAI-compatible format) and normalize a
        # cached-prompt-token count across providers (DeepSeek prompt_cache_hit_tokens,
        # OpenAI prompt_tokens_details.cached_tokens, Anthropic cache_read_input_tokens),
        # so the Usage tab can show how much of the (cheap) prefix was a cache hit.
        usage = dict(resp["response"].get("usage", {}) or {})
        _details = usage.get("prompt_tokens_details") or {}
        usage["cached_tokens"] = int(
            usage.get("prompt_cache_hit_tokens")
            or (_details.get("cached_tokens") if isinstance(_details, dict) else 0)
            or usage.get("cache_read_input_tokens")
            or 0
        )
        calls = []
        for tc in msg.get("tool_calls") or []:
            raw_args = tc["function"].get("arguments")
            args_error = None
            try:
                parsed = json.loads(raw_args or "{}")
                if not isinstance(parsed, dict):
                    parsed, args_error = {}, "arguments were not a JSON object"
            except json.JSONDecodeError as e:
                # Do NOT silently swallow this into {} — malformed arguments almost always
                # mean the response was cut off at the output-token limit (a large
                # write_file content). Surfacing it lets the loop tell the model to resend a
                # smaller call instead of looping on a baffling "missing argument" error.
                parsed, args_error = {}, f"not valid JSON ({e})"
            call = {"id": tc.get("id") or _uid(), "name": tc["function"]["name"], "args": parsed}
            if args_error:
                call["args_error"] = args_error
                call["raw_args_len"] = len(raw_args or "")
            calls.append(call)
        # Surface the model's reasoning (extended-thinking / DeepSeek `reasoning_content`)
        # when present, so the live log can show the agent's thoughts, not just actions.
        reasoning = msg.get("reasoning_content") or msg.get("reasoning")
        # Return (text, calls, reasoning, usage) — usage is a dict with prompt/completion/total tokens
        return msg.get("content"), calls, reasoning, usage
