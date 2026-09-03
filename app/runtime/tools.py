"""The agent's tools. ADD TOOLS HERE — define a schema in TOOL_SCHEMAS and handle it in
execute(). Tools act on the staging clone (sdk.STAGING) and may call kernel syscalls
via sdk. New tools need no kernel changes.
"""

from __future__ import annotations

import inspect
import json
import pathlib
import re
import subprocess
import time

from . import sdk

_RESULT_LIMIT = 120000  # max chars from a tool result (high ceiling; page big files via offset/limit)


def _within(rel: str) -> pathlib.Path | None:
    """Resolve `rel` under the staging dir; None if it escapes."""
    try:
        full = (sdk.STAGING / rel).resolve()
        full.relative_to(sdk.STAGING.resolve())
        return full
    except (ValueError, OSError):
        return None


def _dev_within(rel: str) -> pathlib.Path | None:
    """Resolve `rel` under the development dir; None if it escapes."""
    try:
        dev = sdk.STAGING / "development"
        dev.mkdir(parents=True, exist_ok=True)
        full = (dev / rel).resolve()
        full.relative_to(dev.resolve())
        return full
    except (ValueError, OSError):
        return None


TOOL_SCHEMAS: list[dict] = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the workspace (or a line range). "
                       "Use offset + limit to read large files in chunks; omitting both "
                       "returns the whole file (only a very large file is capped — page it).",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "offset": {"type": "integer", "description": "1-based line number to start reading from"},
                                      "limit": {"type": "integer", "description": "max lines to read (omit for rest-of-file)"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Create or overwrite a file in the workspace.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Patch a file in place by replacing an exact substring — prefer this "
                       "over rewriting a whole file. `old` must match exactly and (unless "
                       "`count`>1) be unique; errors if it is missing or ambiguous.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "old": {"type": "string", "description": "exact text to find"},
                                      "new": {"type": "string", "description": "replacement text"},
                                      "count": {"type": "integer", "default": 1,
                                                "description": "max replacements; 0 = all"}},
                       "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {
        "name": "search",
        "description": "Search file contents across the workspace; returns matching "
                       "`path:line: text` hits (capped). Use this to LOCATE code before "
                       "reading/editing — far cheaper in tokens than reading whole files.",
        "parameters": {"type": "object",
                       "properties": {"pattern": {"type": "string", "description": "text (or regex) to find"},
                                      "glob": {"type": "string", "description": "optional filename glob, e.g. **/*.py"},
                                      "regex": {"type": "boolean", "default": False}},
                       "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "list_dir", "description": "List the entries of a directory in the workspace.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "default": "."}}}}},
    {"type": "function", "function": {
        "name": "run_shell", "description": "Run a shell command in the workspace (use `uv pip install X` / `npm install X`).",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}, "timeout": {"type": "integer", "default": 120}},
                       "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "run_tests", "description": "Validate the workspace (syntax + import + structure + frontend build).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "propose_commit", "description": "Finish: request the new version be committed & rebooted.",
        "parameters": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}}},
    {"type": "function", "function": {
        "name": "get_errors",
        "description": "Read the harness error tracker: runtime errors recorded by the live app "
                       "(unhandled exceptions, tool failures, manual reports) grouped by "
                       "fingerprint, plus versions that failed their boot health check (with the "
                       "crash log). Use it to see what broke in the current or previous versions "
                       "before/while fixing. Pass a fingerprint for full tracebacks of one group.",
        "parameters": {"type": "object",
                       "properties": {"fingerprint": {"type": "string", "description": "show full occurrences of this group"},
                                      "version": {"type": "string", "description": "only errors seen in this version (sha prefix)"},
                                      "include_resolved": {"type": "boolean", "default": False},
                                      "limit": {"type": "integer", "default": 20}}}}},
    {"type": "function", "function": {
        "name": "resolve_error",
        "description": "Mark an error-tracker group as resolved (call after your fix for it ships).",
        "parameters": {"type": "object",
                       "properties": {"fingerprint": {"type": "string"},
                                      "note": {"type": "string", "description": "optional: what fixed it"}},
                       "required": ["fingerprint"]}}},

    # ── Development sandbox tools (scoped to development/ directory) ────────────────
    {"type": "function", "function": {
        "name": "dev_read_file",
        "description": "Read a UTF-8 text file from the development/ workspace (or a line range). "
                       "Use offset + limit to read large files in chunks; omitting both "
                       "returns the whole file (only a very large file is capped — page it).",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "offset": {"type": "integer", "description": "1-based line number to start reading from"},
                                      "limit": {"type": "integer", "description": "max lines to read (omit for rest-of-file)"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "dev_write_file",
        "description": "Create or overwrite a file in the development/ workspace (the sandbox for building arbitrary software projects).",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "dev_edit_file",
        "description": "Patch a file in place in the development/ workspace by replacing an exact substring.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "old": {"type": "string", "description": "exact text to find"},
                                      "new": {"type": "string", "description": "replacement text"},
                                      "count": {"type": "integer", "default": 1,
                                                "description": "max replacements; 0 = all"}},
                       "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {
        "name": "dev_search",
        "description": "Search file contents in the development/ workspace; returns "
                       "`path:line: text` hits (capped). Locate code before reading/editing.",
        "parameters": {"type": "object",
                       "properties": {"pattern": {"type": "string", "description": "text (or regex) to find"},
                                      "glob": {"type": "string", "description": "optional filename glob"},
                                      "regex": {"type": "boolean", "default": False}},
                       "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "dev_list_dir",
        "description": "List the entries of a directory in the development/ workspace.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "default": "."}}}}},
    {"type": "function", "function": {
        "name": "dev_run_shell",
        "description": "Run a shell command inside the development/ workspace (the sandbox for building arbitrary software projects). Use for compiling, installing deps, running tests of your created software.",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}, "timeout": {"type": "integer", "default": 120}},
                       "required": ["command"]}}},
]


def execute(calls: list[dict]) -> list[dict]:
    """Execute tool calls and return results. Each result is
    `{"role":"tool", "tool_call_id":..., "content":str}`.
    """
    results = []
    for c in calls:
        name = c.get("name", "")
        args = c.get("args", {})
        tool_call_id = c.get("id", "")
        outcome = _run_one(name, args)
        results.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": outcome,
        })
    return results


def _call(fn, args) -> str:
    """Invoke a tool handler defensively. The model sometimes emits empty, string, or
    partial arguments; rather than crash with a raw `TypeError: missing N positional
    arguments` (which the agent can't act on), coerce args to a dict, report any MISSING
    required field with an actionable message, and drop unknown keys so a stray field
    never breaks the call."""
    if isinstance(args, str):  # some providers hand back the arguments JSON unparsed
        try:
            args = json.loads(args)
        except Exception:
            args = {}
    if not isinstance(args, dict):
        args = {}
    params = inspect.signature(fn).parameters
    required = [n for n, p in params.items()
                if p.default is p.empty
                and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
    missing = [n for n in required if args.get(n) is None]
    if missing:
        return (f"ERROR: missing required argument(s): {', '.join(missing)}. "
                f"This tool expects: {', '.join(required) or '(none)'}. "
                f"Re-call it with every required field inside the arguments object.")
    if not any(p.kind is p.VAR_KEYWORD for p in params.values()):
        args = {k: v for k, v in args.items() if k in params}  # drop unknown keys
    return fn(**args)


def _run_one(name: str, args: dict | str) -> str:
    """Run a single tool; return string content (truncated to _MAX)."""
    try:
        if name == "run_tests":
            return _run_tests()
        if name == "propose_commit":
            return "delegated to agent loop"
        handler = _HANDLERS.get(name)
        if handler is None:
            return f"ERROR: unknown tool '{name}'"
        return _call(handler, args)
    except Exception as e:
        return f"ERROR in {name}: {e}"


# ── Tool implementations ──────────────────────────────────────────────────────────


def _read_file(path: str, offset: int | None = None, limit: int | None = None) -> str:
    resolved = _within(path)
    if resolved is None:
        return f"ERROR: path '{path}' escapes workspace"
    if not resolved.exists():
        return f"ERROR: file not found: {path}"
    if not resolved.is_file():
        return f"ERROR: not a file: {path}"
    try:
        # Line-based slicing: read the whole file as text first, so a chunk boundary can
        # never split a multi-byte UTF-8 char (which used to crash on box-drawing/emoji).
        # errors="replace" keeps a genuinely corrupt byte from raising.
        full = resolved.read_text(encoding="utf-8", errors="replace")
        lines = full.splitlines(keepends=True)
        total = len(lines)
        if offset is None and limit is None:
            text, info = full, ""
        else:
            start = max((offset or 1) - 1, 0)  # offset is a 1-based line number
            end = start + limit if limit is not None else total
            text = "".join(lines[start:end])
            info = f"[lines {start + 1}-{min(end, total)} of {total}] "
        trunc = ""
        if len(text) > _RESULT_LIMIT:
            text = text[:_RESULT_LIMIT]
            trunc = f"\n…[truncated to {_RESULT_LIMIT} chars — read more with offset/limit]"
        return info + text + trunc
    except Exception as e:
        return f"ERROR reading {path}: {e}"


def _write_file(path: str, content: str) -> str:
    resolved = _within(path)
    if resolved is None:
        return f"ERROR: path '{path}' escapes workspace"
    resolved.parent.mkdir(parents=True, exist_ok=True)
    try:
        resolved.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"ERROR writing {path}: {e}"


def _edit_file(path: str, old: str, new: str, count: int = 1) -> str:
    resolved = _within(path)
    if resolved is None:
        return f"ERROR: path '{path}' escapes workspace"
    if not resolved.exists():
        return f"ERROR: file not found: {path}"
    try:
        content = resolved.read_text(encoding="utf-8")
        n = content.count(old)
        if n == 0:
            return f"ERROR: substring not found in {path}"
        if count != 0 and n > count and count > 0:
            return (f"ERROR: found {n} occurrences but count={count}. "
                    f"Use count={n} or count=0 to replace all.")
        # n is the real number replaced: the guard above ensures n<=count when count>0,
        # and count==0 replaces all n. (Don't recount via `new` — when `new` contains
        # `old`, e.g. inserting a sibling line, that double-counts and misleads the agent.)
        did = n if count == 0 else min(n, count)
        replaced = content.replace(old, new, count if count != 0 else -1)
        resolved.write_text(replaced, encoding="utf-8")
        return f"replaced {did} occurrence(s) in {path}"
    except Exception as e:
        return f"ERROR editing {path}: {e}"


def _list_dir(path: str = ".") -> str:
    resolved = _within(path)
    if resolved is None:
        return f"ERROR: path '{path}' escapes workspace"
    if not resolved.is_dir():
        return f"ERROR: not a directory: {path}"
    try:
        entries = sorted(
            e.name + ("/" if e.is_dir() else "")
            for e in resolved.iterdir()
        )
        return "\n".join(entries) if entries else "(empty)"
    except Exception as e:
        return f"ERROR listing {path}: {e}"


def _run_shell(command: str, timeout: int = 120) -> str:
    try:
        r = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=sdk.STAGING,
        )
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0:
            out += f"\n[exit code {r.returncode}]"
        if len(out) > _RESULT_LIMIT:
            out = out[:_RESULT_LIMIT] + f"\n…[+{len(out) - _RESULT_LIMIT} chars truncated]"
        return out.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except Exception as e:
        return f"ERROR: {e}"


def _run_tests() -> str:
    from . import sdk as _sdk
    return json.dumps(_sdk.validate(), indent=2)


# ── search: find code without reading whole files (a token-saver) ───────────────
_SEARCH_SKIP = {".git", "node_modules", "dist", "__pycache__", ".vite"}


def _search_impl(root: pathlib.Path, pattern: str, glob: str = "",
                 regex: bool = False, max_results: int = 80) -> str:
    if not pattern:
        return "ERROR: pattern is required"
    matcher = None
    if regex:
        try:
            matcher = re.compile(pattern)
        except re.error as e:
            return f"ERROR: bad regex: {e}"
    hits: list[str] = []
    try:
        files = root.rglob(glob) if glob else root.rglob("*")
    except Exception as e:
        return f"ERROR: {e}"
    for p in sorted(files):
        if not p.is_file():
            continue
        try:
            rel_parts = p.relative_to(root).parts
        except ValueError:
            continue
        if any(part in _SEARCH_SKIP for part in rel_parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = p.relative_to(root).as_posix()
        for i, line in enumerate(text.splitlines(), 1):
            if (matcher.search(line) if matcher else (pattern in line)):
                hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                if len(hits) >= max_results:
                    return "\n".join(hits) + f"\n…[capped at {max_results} matches]"
    return "\n".join(hits) if hits else "(no matches)"


def _search(pattern: str, glob: str = "", regex: bool = False) -> str:
    return _search_impl(sdk.STAGING, pattern, glob, regex)


def _dev_search(pattern: str, glob: str = "", regex: bool = False) -> str:
    return _search_impl(sdk.STAGING / "development", pattern, glob, regex)


# ── Dev sandbox tools (same logic, scoped to development/) ──────────────────────


def _dev_read_file(path: str, offset: int | None = None, limit: int | None = None) -> str:
    resolved = _dev_within(path)
    if resolved is None:
        return f"ERROR: path '{path}' escapes development/ workspace"
    if not resolved.exists():
        return f"ERROR: file not found: {path}"
    if not resolved.is_file():
        return f"ERROR: not a file: {path}"
    try:
        # Line-based slicing (see _read_file) — never splits a multi-byte UTF-8 char.
        full = resolved.read_text(encoding="utf-8", errors="replace")
        lines = full.splitlines(keepends=True)
        total = len(lines)
        if offset is None and limit is None:
            text, info = full, ""
        else:
            start = max((offset or 1) - 1, 0)  # offset is a 1-based line number
            end = start + limit if limit is not None else total
            text = "".join(lines[start:end])
            info = f"[lines {start + 1}-{min(end, total)} of {total}] "
        trunc = ""
        if len(text) > _RESULT_LIMIT:
            text = text[:_RESULT_LIMIT]
            trunc = f"\n…[truncated to {_RESULT_LIMIT} chars — read more with offset/limit]"
        return info + text + trunc
    except Exception as e:
        return f"ERROR reading {path}: {e}"


def _dev_write_file(path: str, content: str) -> str:
    resolved = _dev_within(path)
    if resolved is None:
        return f"ERROR: path '{path}' escapes development/ workspace"
    resolved.parent.mkdir(parents=True, exist_ok=True)
    try:
        resolved.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"ERROR writing {path}: {e}"


def _dev_edit_file(path: str, old: str, new: str, count: int = 1) -> str:
    resolved = _dev_within(path)
    if resolved is None:
        return f"ERROR: path '{path}' escapes development/ workspace"
    if not resolved.exists():
        return f"ERROR: file not found: {path}"
    try:
        content = resolved.read_text(encoding="utf-8")
        n = content.count(old)
        if n == 0:
            return f"ERROR: substring not found in {path}"
        if count != 0 and n > count and count > 0:
            return (f"ERROR: found {n} occurrences but count={count}. "
                    f"Use count={n} or count=0 to replace all.")
        # n is the real number replaced: the guard above ensures n<=count when count>0,
        # and count==0 replaces all n. (Don't recount via `new` — when `new` contains
        # `old`, e.g. inserting a sibling line, that double-counts and misleads the agent.)
        did = n if count == 0 else min(n, count)
        replaced = content.replace(old, new, count if count != 0 else -1)
        resolved.write_text(replaced, encoding="utf-8")
        return f"replaced {did} occurrence(s) in {path}"
    except Exception as e:
        return f"ERROR editing {path}: {e}"


def _dev_list_dir(path: str = ".") -> str:
    resolved = _dev_within(path)
    if resolved is None:
        return f"ERROR: path '{path}' escapes development/ workspace"
    if not resolved.is_dir():
        return f"ERROR: not a directory: {path}"
    try:
        entries = sorted(
            e.name + ("/" if e.is_dir() else "")
            for e in resolved.iterdir()
        )
        return "\n".join(entries) if entries else "(empty)"
    except Exception as e:
        return f"ERROR listing {path}: {e}"


def _dev_run_shell(command: str, timeout: int = 120) -> str:
    dev_dir = sdk.STAGING / "development"
    try:
        r = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=dev_dir,
        )
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0:
            out += f"\n[exit code {r.returncode}]"
        if len(out) > _RESULT_LIMIT:
            out = out[:_RESULT_LIMIT] + f"\n…[+{len(out) - _RESULT_LIMIT} chars truncated]"
        return out.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except Exception as e:
        return f"ERROR: {e}"


# ── error tracker (reads the live app's store under DATA_DIR — see app errorlog.py) ──
# Self-contained on purpose: the worker must not import app modules (the app tree it runs
# against is the STAGING copy being edited), so the small read/resolve logic is inlined.
def _errors_dir() -> pathlib.Path:
    return pathlib.Path(sdk.DATA_DIR) / "errors"


def _read_error_records() -> list[dict]:
    path = _errors_dir() / "errors.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for ln in path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
                if isinstance(rec, dict):
                    out.append(rec)
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def _read_error_resolved() -> dict:
    path = _errors_dir() / "resolved.json"
    try:
        d = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _boot_failures() -> list[dict]:
    """health_failed versions (with crash log tail) via the /versions syscall.
    Best-effort: [] when the kernel isn't reachable (e.g. offline unit tests)."""
    try:
        versions = sdk._get("/versions").get("versions") or []
    except Exception:
        return []
    out = []
    for v in versions:
        if v.get("status") == "health_failed":
            out.append(v)
        if len(out) >= 10:
            break
    return out


def _ts(t) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(t)))
    except (TypeError, ValueError):
        return "?"


def _get_errors(fingerprint: str | None = None, version: str | None = None,
                include_resolved: bool = False, limit: int = 20) -> str:
    if fingerprint:
        if fingerprint.startswith("boot-"):
            sha = fingerprint[len("boot-"):]
            for v in _boot_failures():
                if str(v.get("version", "")).startswith(sha):
                    health = v.get("health") or {}
                    return (f"BOOT FAILURE {v.get('version', '')[:12]} (v{v.get('seq', '?')}): "
                            f"{health.get('reason', '')}\n--- captured app output (tail) ---\n"
                            f"{health.get('log_tail') or '(no log captured)'}")
            return f"ERROR: no boot failure matching {fingerprint}"
        recs = [r for r in _read_error_records() if r.get("fingerprint") == fingerprint]
        if not recs:
            return f"ERROR: no error group {fingerprint}"
        parts = []
        for r in recs[-5:][::-1]:
            parts.append(
                f"[{_ts(r.get('ts'))}] {r.get('exc_type')}: {r.get('message')}\n"
                f"  source={r.get('source')} route={r.get('route')} "
                f"version={(r.get('version') or '?')[:12]}\n"
                f"{r.get('traceback') or '(no traceback)'}")
        return f"group {fingerprint} — {len(recs)} occurrence(s), newest first:\n\n" + "\n\n".join(parts)

    resolved = _read_error_resolved()
    groups: dict[str, dict] = {}
    for r in _read_error_records():
        fp = r.get("fingerprint") or ""
        g = groups.setdefault(fp, {"count": 0, "versions": []})
        g["count"] += 1
        g["last"] = r
        v = r.get("version") or ""
        if v and v not in g["versions"]:
            g["versions"].append(v)
    lines = []
    for v in _boot_failures():
        health = v.get("health") or {}
        if version and not str(v.get("version", "")).startswith(version):
            continue
        lines.append(f"boot-{str(v.get('version', ''))[:12]}  BOOT FAILURE v{v.get('seq', '?')}: "
                     f"{health.get('reason', '')} (pass this fingerprint for the crash log)")
    shown = 0
    for fp, g in sorted(groups.items(), key=lambda kv: kv[1]["last"].get("ts") or 0, reverse=True):
        is_resolved = fp in resolved
        if is_resolved and not include_resolved:
            continue
        if version and not any(x.startswith(version) for x in g["versions"]):
            continue
        if shown >= max(1, limit):
            lines.append(f"…more groups omitted (limit={limit})")
            break
        r = g["last"]
        vers = ",".join(x[:8] for x in g["versions"][-3:]) or "?"
        lines.append(
            f"{fp}  ×{g['count']}  {r.get('exc_type')}: {(r.get('message') or '')[:160]}"
            f"  [source={r.get('source')} route={r.get('route')} versions={vers} "
            f"last={_ts(r.get('ts'))}{' RESOLVED' if is_resolved else ''}]")
        shown += 1
    if not lines:
        return "no errors recorded" + (f" for version {version}" if version else "")
    return ("error groups (newest first; pass fingerprint for full tracebacks; "
            "resolve_error when fixed):\n" + "\n".join(lines))


def _resolve_error(fingerprint: str, note: str = "") -> str:
    if not any(r.get("fingerprint") == fingerprint for r in _read_error_records()):
        return f"ERROR: no error group {fingerprint}"
    d = _read_error_resolved()
    d[fingerprint] = {"resolved_at": time.time(), "note": (note or "")[:500]}
    try:
        _errors_dir().mkdir(parents=True, exist_ok=True)
        (_errors_dir() / "resolved.json").write_text(json.dumps(d, indent=2), encoding="utf-8")
    except OSError as e:
        return f"ERROR writing resolution: {e}"
    return f"marked {fingerprint} resolved"


def unresolved_error_summary() -> tuple[int, int]:
    """(unresolved groups, of which seen in the active version) — for the task-start
    notice in agent.py. Active version via the /status syscall, best-effort."""
    resolved = _read_error_resolved()
    groups: dict[str, list[str]] = {}
    for r in _read_error_records():
        fp = r.get("fingerprint") or ""
        if fp in resolved:
            continue
        v = r.get("version") or ""
        groups.setdefault(fp, [])
        if v:
            groups[fp].append(v)
    active = ""
    try:
        active = ((sdk._get("/status").get("active") or {}).get("version")) or ""
    except Exception:
        active = ""
    in_active = sum(1 for vs in groups.values() if active and any(x == active for x in vs))
    return len(groups), in_active


# name -> handler, consumed by _run_one via _call (defined after the impls so the names
# above are all bound). run_tests / propose_commit are handled directly in _run_one.
_HANDLERS = {
    "read_file": _read_file, "write_file": _write_file, "edit_file": _edit_file,
    "list_dir": _list_dir, "search": _search, "run_shell": _run_shell,
    "get_errors": _get_errors, "resolve_error": _resolve_error,
    "dev_read_file": _dev_read_file, "dev_write_file": _dev_write_file,
    "dev_edit_file": _dev_edit_file, "dev_list_dir": _dev_list_dir,
    "dev_search": _dev_search, "dev_run_shell": _dev_run_shell,
}
