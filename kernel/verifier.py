"""The Verification Gate's derivation half (ring 0): request → executable checks.

Turns a change request into acceptance checks (the `kernel/checks.py` DSL) that the booted
candidate must pass before promotion. The deriver sees the ORIGINAL user prompt plus the
staged diff (read-only evidence of which routes exist), and is instructed to verify the
user-visible behavior the request demanded — never implementation details. It runs in the
kernel with kernel-held keys (`kernel.llm.chat`, so calls are metered and budget-capped),
out of reach of the self-modifying agent.

Failure policy: derivation is fail-open by default — a provider outage or an unverifiable
request ("tidy the comments") must not block every self-mod, so the version promotes
stamped "unverified" (config `verifier.strict` fail-closes this). A failing CHECK, by
contrast, always blocks promotion; that enforcement lives in `kernel/core.py`.

Offline: with the scripted engine there is no model, so derivation parses a literal
`__VERIFY_CHECK__ <json>` marker out of the prompt (one spec or a list of specs) — the
same test affordance pattern as `__GAMEDAY_BREAK__` — letting the whole gate run in the
keyless test suite.
"""

from __future__ import annotations

import json
from typing import Any

from kernel import checks, llm

_MARKER = "__VERIFY_CHECK__"
_PROMPT_MAX = 4000    # request chars shown to the deriver
_DIFF_MAX = 20000     # diff chars shown to the deriver

_DERIVE_SYSTEM = """\
You derive executable acceptance checks for a self-modifying app harness. Given a user's
change request and the diff that claims to implement it, output checks that PROVE the
requested user-visible behavior works on the running app. Checks are JSON only — never
code.

A check: {"name": "<short behavior statement>", "steps": [<1-10 steps>]}
A step: {"method": "GET|POST|PUT|PATCH|DELETE", "path": "/local/path",
         "json": <optional request body>, "expect": {<assertions>},
         "save": {<var>: "$.dotted.path"}, "timeout": <optional seconds 1-60>}
Assertions (at least one per step):
  "status": <int>                 exact HTTP status
  "contains": "<substring>"       response body contains it
  "json_subset": {...}            response JSON deeply contains this subset
  "llm_judge": {"rubric": "..."}  ONLY when nothing deterministic can express it
Variables saved with "save" substitute into later paths/bodies as {var}.

Rules:
- Verify what the REQUEST asked for, as a user would observe it — not internals of the diff.
- Prefer deterministic assertions (status/contains/json_subset) over llm_judge.
- Checks run against a live app sharing the real data partition: use clearly-marked test
  data (e.g. strings containing "verify-check") and add cleanup steps (DELETE) when the
  API allows.
- Each check must be self-contained and repeatable (it becomes a permanent regression check).
- At most {max_checks} checks; fewer, focused checks beat many shallow ones.

Output STRICT JSON, nothing else:
  {"checks": [<check>, ...]}
or, when the request has no behavior verifiable over HTTP (pure refactor, comments, docs):
  {"skippable": true, "reason": "<why>"}
"""

_JUDGE_SYSTEM = """\
You are a strict verifier. Decide whether the HTTP response body satisfies the rubric.
First line: exactly PASS or FAIL. Second line: a one-sentence reason.
"""


def _cfg(config: dict) -> dict:
    return config.get("verifier", {}) or {}


def enabled(config: dict) -> bool:
    return bool(_cfg(config).get("enabled", False))


def strict(config: dict) -> bool:
    return bool(_cfg(config).get("strict", False))


def deadline(config: dict) -> float:
    return float(_cfg(config).get("timeout_seconds", 120))


def _model(config: dict) -> str:
    return (_cfg(config).get("model") or "").strip() or config.get("agent", {}).get("model", "")


def _scripted(config: dict) -> bool:
    return config.get("agent", {}).get("engine") == "scripted"


def _extract_json(text: str) -> Any:
    """First JSON value in a model reply (tolerates markdown fences / prose around it)."""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                value, _ = decoder.raw_decode(text[i:])
                return value
            except ValueError:
                continue
    raise ValueError("no JSON found in reply")


def _validate_all(raw_specs: list[Any], max_checks: int) -> tuple[list[dict], str]:
    specs: list[dict] = []
    for spec in raw_specs[:max_checks]:
        ok, err = checks.validate_spec(spec)
        if not ok:
            return [], f"invalid check spec: {err}"
        specs.append(spec)
    return specs, ""


def _derive_scripted(prompt: str, max_checks: int) -> dict[str, Any]:
    """Parse `__VERIFY_CHECK__ <json>` markers (a spec or a list of specs) from the
    prompt. No marker ⇒ skipped (regression checks still run at promotion)."""
    decoder = json.JSONDecoder()
    raw: list[Any] = []
    idx = prompt.find(_MARKER)
    while idx != -1:
        rest = prompt[idx + len(_MARKER):].lstrip()
        try:
            value, _ = decoder.raw_decode(rest)
        except ValueError:
            return {"ok": False, "checks": [],
                    "skipped": False, "reason": f"unparseable {_MARKER} payload"}
        raw.extend(value if isinstance(value, list) else [value])
        idx = prompt.find(_MARKER, idx + len(_MARKER))
    if not raw:
        return {"ok": True, "checks": [], "skipped": True, "reason": "no verify marker"}
    specs, err = _validate_all(raw, max_checks)
    if err:
        return {"ok": False, "checks": [], "skipped": False, "reason": err}
    return {"ok": True, "checks": specs, "skipped": False, "reason": ""}


async def derive_checks(prompt: str, diff: str, config: dict) -> dict[str, Any]:
    """Derive acceptance checks for a change. Returns
    {ok, checks, skipped, reason} — ok=False is a DERIVATION failure (blocks only under
    `verifier.strict`); skipped=True means the request isn't verifiable over HTTP."""
    max_checks = int(_cfg(config).get("max_checks", 3))
    if _scripted(config):
        return _derive_scripted(prompt, max_checks)
    system = _DERIVE_SYSTEM.replace("{max_checks}", str(max_checks))
    user = (f"Change request:\n{prompt[:_PROMPT_MAX]}\n\n"
            f"Diff of the candidate version:\n{diff[:_DIFF_MAX]}")
    try:
        resp = await llm.chat(_model(config),
                              [{"role": "system", "content": system},
                               {"role": "user", "content": user}],
                              temperature=0.0)
        content = resp["choices"][0]["message"]["content"] or ""
        data = _extract_json(content)
    except Exception as exc:
        return {"ok": False, "checks": [], "skipped": False,
                "reason": f"derivation failed: {type(exc).__name__}: {exc}"}
    if isinstance(data, dict) and data.get("skippable"):
        return {"ok": True, "checks": [], "skipped": True,
                "reason": str(data.get("reason") or "not verifiable over HTTP")}
    raw = data.get("checks") if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        return {"ok": False, "checks": [], "skipped": False,
                "reason": "deriver returned no checks"}
    specs, err = _validate_all(raw, max_checks)
    if err:
        return {"ok": False, "checks": [], "skipped": False, "reason": err}
    return {"ok": True, "checks": specs, "skipped": False, "reason": ""}


def make_judge(config: dict) -> checks.Judge | None:
    """The async grader for `llm_judge` assertions (None with the scripted engine —
    offline checks must stick to deterministic assertions)."""
    if _scripted(config):
        return None
    model = _model(config)

    async def judge(rubric: str, body: str) -> tuple[bool, str]:
        resp = await llm.chat(model,
                              [{"role": "system", "content": _JUDGE_SYSTEM},
                               {"role": "user",
                                "content": f"Rubric: {rubric}\n\nResponse body:\n{body}"}],
                              temperature=0.0)
        content = (resp["choices"][0]["message"]["content"] or "").strip()
        first, _, rest = content.partition("\n")
        return first.strip().upper().startswith("PASS"), rest.strip() or first.strip()

    return judge


async def verify_candidate(port: int, acceptance: list[dict[str, Any]], config: dict,
                           *, include_regressions: bool = True,
                           exclude_origins: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    """Run a candidate's acceptance checks + the frozen regression suite against its slot
    port. Returns the combined `checks.run_checks` report (report["ok"] gates promotion);
    stored checks get their `last_result` stamped by the caller via `record_results`."""
    items: list[dict[str, Any]] = [{"spec": spec, "kind": "acceptance"} for spec in acceptance]
    if include_regressions:
        items += [{"spec": c["spec"], "id": c["id"], "origin": c.get("origin"),
                   "kind": "regression"} for c in checks.active_checks(exclude_origins)]
    if not items:
        return {"ok": True, "total": 0, "passed": 0, "results": [], "failed": []}
    return await checks.run_checks(port, items, deadline=deadline(config),
                                   judge=make_judge(config))
