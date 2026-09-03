"""Verification checks: the executable acceptance/regression DSL + its protected store.

The Verification Gate's enforcement half (ring 0). A *check* is a small JSON spec of HTTP
steps with assertions — never generated code — that the kernel executes against a booted
candidate (directly on its slot port, like the watchdog) after the health gate and before
promotion. Checks that pass at promotion are *frozen* into `state/checks.json` as the
regression suite every future candidate must also pass, so the safety net compounds with
use. The store lives in protected `state/` and this module in the immutable kernel, so the
self-modifying agent can never edit its own grader.

Derivation of checks from a change request is the verifier's job (`kernel/verifier.py`);
this module is deliberately LLM-free mechanism — an optional async `judge` callable is
injected for the one fuzzy assertion kind (`llm_judge`), keeping the runner deterministic
and fully offline-testable.

Spec shape (validated by `validate_spec` before storage or execution):

    {"name": "bookmarks persist", "steps": [
      {"method": "POST", "path": "/api/bookmarks", "json": {"url": "https://x"},
       "expect": {"status": 200}, "save": {"bid": "$.id"}},
      {"method": "GET", "path": "/api/bookmarks/{bid}",
       "expect": {"status": 200, "contains": "https://x"}}]}

Assertions: `status` (exact code), `contains` (substring of the body), `json_subset`
(deep subset of the JSON body), `llm_judge` ({"rubric": …} graded by the injected judge).
`save` captures values out of a JSON response (dotted path, optional `$.` prefix) into
variables; `{var}` substitutes into later paths and JSON string values.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from typing import Any, Awaitable, Callable

import httpx

from kernel import state_store

# judge(rubric, response_text) -> (passed, detail). Provided by kernel/verifier.py.
Judge = Callable[[str, str], Awaitable[tuple[bool, str]]]

_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_STEP_KEYS = {"method", "path", "json", "expect", "save", "timeout"}
_EXPECT_KEYS = {"status", "contains", "json_subset", "llm_judge"}
_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VAR_REF_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_MAX_STEPS = 10
_MAX_NAME = 120
_DEFAULT_STEP_TIMEOUT = 10.0
_BODY_SNIPPET = 400      # how much of an unexpected response to quote in failure details
_JUDGE_BODY_MAX = 4000   # how much body the llm_judge sees

# All store mutations are read-modify-write on one JSON file; serialize them (writes
# themselves are atomic via state_store) — same pattern as the version registry.
_LOCK = threading.Lock()


# ── spec validation ────────────────────────────────────────────────────────────────────
def validate_spec(spec: Any) -> tuple[bool, str]:
    """Structural validation of one check spec. Strict — unknown keys are rejected, so a
    malformed derivation fails loudly here instead of silently asserting nothing."""
    if not isinstance(spec, dict):
        return False, "spec must be an object"
    unknown = set(spec) - {"name", "steps"}
    if unknown:
        return False, f"unknown spec keys {sorted(unknown)}"
    name = spec.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > _MAX_NAME:
        return False, f"name must be a non-empty string (max {_MAX_NAME} chars)"
    steps = spec.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= _MAX_STEPS:
        return False, f"steps must be a list of 1..{_MAX_STEPS} steps"
    for i, step in enumerate(steps):
        ok, err = _validate_step(step)
        if not ok:
            return False, f"step {i + 1}: {err}"
    return True, ""


def _validate_step(step: Any) -> tuple[bool, str]:
    if not isinstance(step, dict):
        return False, "must be an object"
    unknown = set(step) - _STEP_KEYS
    if unknown:
        return False, f"unknown keys {sorted(unknown)}"
    if step.get("method") not in _METHODS:
        return False, f"method must be one of {list(_METHODS)}"
    path = step.get("path")
    if not isinstance(path, str) or not path.startswith("/") or "://" in path:
        return False, "path must be a local path starting with '/'"
    if "json" in step and not isinstance(step["json"], (dict, list)):
        return False, "json body must be an object or array"
    if "timeout" in step:
        if not isinstance(step["timeout"], (int, float)) or not 1 <= step["timeout"] <= 60:
            return False, "timeout must be 1..60 seconds"
    expect = step.get("expect")
    if not isinstance(expect, dict) or not expect:
        return False, "expect is required and must be a non-empty object"
    unknown = set(expect) - _EXPECT_KEYS
    if unknown:
        return False, f"unknown expect keys {sorted(unknown)}"
    if "status" in expect and (not isinstance(expect["status"], int)
                               or not 100 <= expect["status"] <= 599):
        return False, "expect.status must be an HTTP status code"
    if "contains" in expect and (not isinstance(expect["contains"], str) or not expect["contains"]):
        return False, "expect.contains must be a non-empty string"
    if "json_subset" in expect and not isinstance(expect["json_subset"], (dict, list)):
        return False, "expect.json_subset must be an object or array"
    if "llm_judge" in expect:
        judge = expect["llm_judge"]
        if (not isinstance(judge, dict) or set(judge) != {"rubric"}
                or not isinstance(judge.get("rubric"), str) or not judge["rubric"].strip()):
            return False, 'expect.llm_judge must be {"rubric": "<non-empty>"}'
    save = step.get("save")
    if save is not None:
        if not isinstance(save, dict) or not save:
            return False, "save must be a non-empty object of {var: path}"
        for var, path_expr in save.items():
            if not isinstance(var, str) or not _VAR_NAME_RE.match(var):
                return False, f"save variable {var!r} must be a simple identifier"
            if not isinstance(path_expr, str) or not path_expr.strip():
                return False, f"save.{var} must be a dotted path string"
    return True, ""


# ── execution ──────────────────────────────────────────────────────────────────────────
def _substitute(value: Any, variables: dict[str, Any]) -> Any:
    """Replace {var} references. A string that is EXACTLY one reference yields the raw
    saved value (so numeric ids survive round-trips); otherwise textual substitution.
    Unknown variables are left as-is — the assertion failure that follows names them."""
    if isinstance(value, str):
        exact = _VAR_REF_RE.fullmatch(value)
        if exact and exact.group(1) in variables:
            return variables[exact.group(1)]
        return _VAR_REF_RE.sub(
            lambda m: str(variables.get(m.group(1), m.group(0))), value)
    if isinstance(value, dict):
        return {k: _substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, variables) for v in value]
    return value


def _extract(data: Any, path_expr: str) -> tuple[bool, Any]:
    """Resolve a dotted path (optional `$.` prefix; int segments index lists)."""
    expr = path_expr.strip()
    if expr.startswith("$."):
        expr = expr[2:]
    elif expr == "$":
        return True, data
    node = data
    for part in expr.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.lstrip("-").isdigit():
            try:
                node = node[int(part)]
            except IndexError:
                return False, None
        else:
            return False, None
    return True, node


def _json_subset(expected: Any, actual: Any) -> bool:
    """Deep subset: dicts by key, list items each match SOME actual item, scalars equal."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _json_subset(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and all(
            any(_json_subset(e, a) for a in actual) for e in expected)
    return expected == actual


def _snippet(text: str) -> str:
    text = text.strip()
    return text[:_BODY_SNIPPET] + ("…" if len(text) > _BODY_SNIPPET else "")


async def _run_step(client: httpx.AsyncClient, base: str, step: dict,
                    variables: dict[str, Any], judge: Judge | None,
                    timeout: float) -> tuple[bool, str]:
    method = step["method"]
    path = _substitute(step["path"], variables)
    body = _substitute(step["json"], variables) if "json" in step else None
    try:
        resp = await client.request(method, base + path, json=body, timeout=timeout)
    except httpx.HTTPError as exc:
        return False, f"{method} {path}: request failed ({type(exc).__name__}: {exc})"

    expect = step["expect"]
    if "status" in expect and resp.status_code != expect["status"]:
        return False, (f"{method} {path}: expected status {expect['status']}, "
                       f"got {resp.status_code} — {_snippet(resp.text)}")
    if "contains" in expect:
        needle = _substitute(expect["contains"], variables)
        if str(needle) not in resp.text:
            return False, (f"{method} {path}: body does not contain {str(needle)!r} — "
                           f"{_snippet(resp.text)}")
    if "json_subset" in expect or step.get("save"):
        try:
            data = resp.json()
        except ValueError:
            return False, f"{method} {path}: response is not JSON — {_snippet(resp.text)}"
        if "json_subset" in expect:
            expected = _substitute(expect["json_subset"], variables)
            if not _json_subset(expected, data):
                return False, (f"{method} {path}: JSON does not contain expected subset "
                               f"{json.dumps(expected)[:200]} — {_snippet(resp.text)}")
        for var, path_expr in (step.get("save") or {}).items():
            found, value = _extract(data, path_expr)
            if not found:
                return False, f"{method} {path}: save path {path_expr!r} not found in response"
            variables[var] = value
    if "llm_judge" in expect:
        if judge is None:
            return False, f"{method} {path}: llm_judge assertion but no judge is available"
        rubric = expect["llm_judge"]["rubric"]
        try:
            passed, detail = await judge(rubric, resp.text[:_JUDGE_BODY_MAX])
        except Exception as exc:  # a judge outage must fail the CHECK, never crash the gate
            return False, f"{method} {path}: llm_judge errored ({exc})"
        if not passed:
            return False, f"{method} {path}: llm_judge failed rubric {rubric!r}: {detail}"
    return True, "ok"


async def run_checks(port: int, checks: list[dict[str, Any]], *,
                     deadline: float, judge: Judge | None = None) -> dict[str, Any]:
    """Execute checks against a candidate on 127.0.0.1:`port` (same direct-port access as
    the watchdog). Each item needs `spec`; `id`/`origin`/`kind`/`name` ride into its result.
    A hung app can never wedge promotion: per-step timeouts are clamped to the remaining
    overall `deadline`, and hitting the deadline fails the run."""
    base = f"http://127.0.0.1:{port}"
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    ok_all = True
    async with httpx.AsyncClient() as client:
        for item in checks:
            spec = item["spec"]
            meta = {k: item[k] for k in ("id", "origin", "kind") if k in item}
            result: dict[str, Any] = {"name": spec.get("name", "check"), **meta}
            variables: dict[str, Any] = {}
            step_ok, detail, steps_run = True, "ok", 0
            for i, step in enumerate(spec["steps"]):
                remaining = deadline - (time.monotonic() - started)
                if remaining <= 0:
                    step_ok = False
                    detail = f"verification deadline exceeded ({deadline:.0f}s)"
                    break
                timeout = min(float(step.get("timeout", _DEFAULT_STEP_TIMEOUT)), remaining)
                step_ok, detail = await _run_step(client, base, step, variables, judge, timeout)
                steps_run = i + 1
                if not step_ok:
                    break
            result.update({"ok": step_ok, "detail": detail if not step_ok else "passed",
                           "steps_run": steps_run, "steps_total": len(spec["steps"])})
            results.append(result)
            ok_all = ok_all and step_ok
    failed = [r for r in results if not r["ok"]]
    return {"ok": ok_all, "total": len(results), "passed": len(results) - len(failed),
            "results": results, "failed": failed}


# ── the protected store (state/checks.json) ────────────────────────────────────────────
def _now() -> float:
    return round(time.time(), 3)


def list_checks() -> list[dict[str, Any]]:
    return state_store.read_check_store()


def active_checks(exclude_origins: set[str] | frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """The regression suite a candidate must pass. `exclude_origins` lets a revert skip
    the checks guarding the very version it removes."""
    return [c for c in state_store.read_check_store()
            if c.get("status") == "active" and c.get("origin") not in exclude_origins]


def freeze_checks(specs: list[dict[str, Any]], *, origin: str, task_id: str | None,
                  prompt: str | None) -> list[str]:
    """Freeze passing acceptance checks into the regression suite (idempotent per
    origin+name, so a re-promotion of the same version can't duplicate its checks)."""
    with _LOCK:
        checks = state_store.read_check_store()
        existing = {(c.get("origin"), c.get("spec", {}).get("name")) for c in checks}
        added: list[str] = []
        for spec in specs:
            if (origin, spec.get("name")) in existing:
                continue
            cid = "c" + uuid.uuid4().hex[:10]
            checks.append({
                "id": cid, "name": spec.get("name"), "origin": origin, "task": task_id,
                "prompt": (prompt or "")[:300] or None, "spec": spec,
                "status": "active", "disabled_by": None,
                "created_at": _now(), "last_result": None,
            })
            added.append(cid)
        if added:
            state_store.write_check_store(checks)
        return added


def set_check_enabled(check_id: str, enabled: bool) -> tuple[bool, str]:
    """Operator toggle. An operator-disable is sticky (lifecycle transitions never
    re-enable it); an operator-enable overrides a lifecycle-disable."""
    with _LOCK:
        checks = state_store.read_check_store()
        entry = next((c for c in checks if c.get("id") == check_id), None)
        if entry is None:
            return False, f"unknown check {check_id}"
        entry["status"] = "active" if enabled else "disabled"
        entry["disabled_by"] = None if enabled else "operator"
        state_store.write_check_store(checks)
        return True, entry["status"]


def sync_lifecycle(*, disable_origins: set[str] | frozenset[str] = frozenset(),
                   enable_origins: set[str] | frozenset[str] = frozenset()) -> dict[str, int]:
    """Mirror version-line transitions onto the suite: checks follow their origin version.
    Disable when the origin is reverted / falls off the active line; re-enable when it is
    restored/reapplied — but never override a sticky operator-disable."""
    disabled = enabled = 0
    with _LOCK:
        checks = state_store.read_check_store()
        for c in checks:
            if c.get("origin") in disable_origins and c.get("status") == "active":
                c["status"], c["disabled_by"] = "disabled", "lifecycle"
                disabled += 1
            elif (c.get("origin") in enable_origins and c.get("status") == "disabled"
                  and c.get("disabled_by") == "lifecycle"):
                c["status"], c["disabled_by"] = "active", None
                enabled += 1
        if disabled or enabled:
            state_store.write_check_store(checks)
    return {"disabled": disabled, "enabled": enabled}


def record_results(results: list[dict[str, Any]], version: str) -> None:
    """Stamp each stored check's `last_result` after a run (best-effort bookkeeping)."""
    by_id = {r["id"]: r for r in results if r.get("id")}
    if not by_id:
        return
    with _LOCK:
        checks = state_store.read_check_store()
        touched = False
        for c in checks:
            r = by_id.get(c.get("id"))
            if r is not None:
                c["last_result"] = {"ok": r["ok"], "detail": r["detail"],
                                    "t": _now(), "version": version[:12]}
                touched = True
        if touched:
            state_store.write_check_store(checks)
