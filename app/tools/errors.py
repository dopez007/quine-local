"""Error-tracker tools — let the Run-tab chat agent inspect (and resolve) the harness's
recorded errors: unhandled app exceptions, tool failures, and manual reports grouped by
fingerprint, plus versions that failed their boot health check (with the crash log tail,
merged via the read-only /versions syscall). Store logic lives in `errorlog` (app root).
"""

from __future__ import annotations

import time

import errorlog


def _schema(name: str, description: str, properties: dict, required=None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    }


def _ts(t) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(t)))
    except (TypeError, ValueError):
        return "?"


async def get_errors(args: dict, ctx) -> str:
    fingerprint = (args.get("fingerprint") or "").strip()
    version = (args.get("version") or "").strip() or None
    include_resolved = bool(args.get("include_resolved", False))

    if fingerprint and not fingerprint.startswith("boot-"):
        occurrences = errorlog.get_group(fingerprint, limit=5)
        if not occurrences:
            return f"no error group {fingerprint}"
        parts = [f"group {fingerprint} — newest first:"]
        for r in occurrences:
            parts.append(
                f"[{_ts(r.get('ts'))}] {r.get('exc_type')}: {r.get('message')}\n"
                f"  source={r.get('source')} route={r.get('route')} "
                f"version={(r.get('version') or '?')[:12]}\n"
                f"{r.get('traceback') or '(no traceback)'}")
        return "\n\n".join(parts)

    boot: list[dict] = []
    try:
        data = await ctx.syscall_get("/versions")
        boot = errorlog.boot_groups(
            (data or {}).get("versions") or [],
            include_resolved=include_resolved or bool(fingerprint),
        )
    except Exception:
        boot = []  # kernel unreachable — app-side records still shown

    if fingerprint:  # a boot-<sha> fingerprint: show that crash's captured log
        for g in boot:
            if g["fingerprint"] == fingerprint:
                return (f"BOOT FAILURE {g['versions'][0][:12] if g['versions'] else '?'} "
                        f"(v{g.get('seq', '?')}): {g['message']}\n"
                        f"--- captured app output (tail) ---\n"
                        f"{g['last_traceback'] or '(no log captured)'}")
        return f"no boot failure matching {fingerprint}"

    groups = errorlog.list_groups(version=version, include_resolved=include_resolved)
    if version:
        boot = [g for g in boot if any(v.startswith(version) for v in g["versions"])]
    if not boot and not groups:
        return "no errors recorded" + (f" for version {version}" if version else "")
    lines = []
    for g in boot:
        sha = g["versions"][0][:12] if g["versions"] else "?"
        lines.append(f"{g['fingerprint']}  BOOT FAILURE v{g.get('seq', '?')} ({sha}): "
                     f"{g['message']} (pass this fingerprint for the crash log)")
    for g in groups[:30]:
        vers = ",".join(v[:8] for v in g["versions"][-3:]) or "?"
        lines.append(
            f"{g['fingerprint']}  ×{g['count']}  {g['exc_type']}: {(g['message'] or '')[:160]}"
            f"  [source={g['source']} route={g['route']} versions={vers} "
            f"last={_ts(g['last_ts'])}{' RESOLVED' if g.get('resolved') else ''}]")
    return ("error groups (newest first; pass a fingerprint for full tracebacks):\n"
            + "\n".join(lines))


async def resolve_error(args: dict, ctx) -> str:
    fingerprint = (args.get("fingerprint") or "").strip()
    if not fingerprint:
        return "error: fingerprint required"
    if not errorlog.resolve(fingerprint, note=args.get("note") or ""):
        return f"no error group {fingerprint}"
    return f"marked {fingerprint} resolved"


TOOLS = {
    "get_errors": {
        "schema": _schema(
            "get_errors",
            "List the harness's recorded errors grouped by fingerprint (runtime exceptions, "
            "tool failures, manual reports, and versions that failed boot). Pass a fingerprint "
            "for full tracebacks / the boot crash log.",
            {"fingerprint": {"type": "string", "description": "Show one group in full."},
             "version": {"type": "string", "description": "Only errors seen in this version (sha prefix)."},
             "include_resolved": {"type": "boolean", "description": "Include resolved groups."}},
        ),
        "handler": get_errors,
    },
    "resolve_error": {
        "schema": _schema(
            "resolve_error",
            "Mark an error group as resolved (after the underlying problem is fixed).",
            {"fingerprint": {"type": "string"},
             "note": {"type": "string", "description": "Optional: what fixed it."}},
            required=["fingerprint"],
        ),
        "handler": resolve_error,
    },
}
