"""Gated Kernel Self-Update — the kernel-side half (ring 0 authors ring 0).

The app self-mod pipeline stages a clone of `versions.git`, which holds only `app/` — so
the agent has zero kernel-source access, and that is the immutable boundary. This module
opens a SEPARATE, heavily gated path for the kernel to rewrite itself:

  1. a dedicated kernel version store (`state/kernel.git`), seeded from the live `kernel/`;
  2. a KERNEL-RESIDENT agent driver (deliberately NOT the app-mutable `app/runtime/` — the
     thing authoring ring-0 code must itself be ring 0), contained to the kernel staging tree;
  3. validation stricter than an app change: syntax + import-smoke of the candidate kernel
     package + a curated kernel test subset run AGAINST the candidate;
  4. the candidate is only ever *committed + held* here — promotion (approval → the firmware
     verifies, swaps, health-gates, auto-rolls-back) lives in kernel/core.py + bootstrap/.

The repo tree keeps the `kernel/` prefix (we seed `kernel/` as a subdir) so a version's
digest is `bootstrap.integrity.compute_digest(tree_root)` uniformly across the live tree, a
staging clone, and a firmware-deployed candidate — the exact hash the firmware verifies.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any

from bootstrap import integrity
from kernel import llm, policy, state_store, versioning
from kernel.state_store import KERNEL_VERSIONS_GIT
from kernel.util import CHILD_CREATIONFLAGS

# The live/shipped kernel package — the seed for v1 and the firmware's recovery floor.
KERNEL_SEED = state_store.ROOT / "kernel"
_STAGE_SUBDIR = "kernel"  # the tree prefix inside kernel.git (so digests are layout-stable)

# The curated kernel test subset run against a candidate before it can be committed. Kept
# small + representative (core boot / reboot / auto-rollback). Overridable via env so the
# offline suite can point it at a fast stub instead of nesting full kernel boots.
CURATED_KERNEL_TESTS = ["tests/local/contract/test_kernel_candidate.py"]
_MAX_OUTPUT = 8000
_KERNEL_AGENT_MAX_STEPS = 30


def _truncate(text: str) -> str:
    return text if len(text) <= _MAX_OUTPUT else text[:_MAX_OUTPUT] + "\n…[truncated]"


# ── version store (parallels versioning.py, but for the kernel tree) ───────────────────
def ensure_kernel_repo() -> None:
    versioning._ensure_gitconfig()
    if not (KERNEL_VERSIONS_GIT / "HEAD").exists():
        KERNEL_VERSIONS_GIT.mkdir(parents=True, exist_ok=True)
        versioning._git(["init", "--bare", "-b", "main", str(KERNEL_VERSIONS_GIT)])


def has_kernel_history() -> bool:
    res = versioning._run(["--git-dir", str(KERNEL_VERSIONS_GIT),
                           "rev-parse", "--verify", "--quiet", "main"])
    return res.returncode == 0


def kernel_head() -> str:
    return versioning._git(["--git-dir", str(KERNEL_VERSIONS_GIT), "rev-parse", "main"]).strip()


def digest_of(tree_root: pathlib.Path) -> str:
    """The firmware-verifiable digest of a kernel tree (hashes `<root>/kernel/**/*.py`)."""
    return integrity.compute_digest(tree_root)


def deploy_kernel(sha: str, dest: pathlib.Path) -> None:
    """Materialize a kernel version's tree into `dest` via the SAME deterministic git archive
    the firmware uses (bootstrap.kernel_slots.deploy_version): pinned config +
    core.autocrlf=false/eol=lf, so the bytes — and therefore the digest — are identical on
    both sides regardless of the host's global git line-ending settings."""
    import shutil
    import zipfile

    import os
    import uuid

    if dest.exists():
        versioning._force_rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    # The scratch archive name must be UNIQUE per call, not just per sha: digest_of_version()
    # deploys into a mkdtemp, so `dest.parent` is the SHARED system temp dir — two processes
    # (parallel test workers; two harnesses on one host) deploying the same kernel version would
    # otherwise pick the identical path and rip the file out from under each other mid-extract
    # ("WinError 32: used by another process").
    archive = dest.parent / f".kernel-deploy-{sha[:12]}-{os.getpid()}-{uuid.uuid4().hex[:8]}.zip"
    try:
        versioning._git(["-c", "core.autocrlf=false", "-c", "core.eol=lf",
                         "--git-dir", str(KERNEL_VERSIONS_GIT), "archive",
                         "--format=zip", "-o", str(archive), sha])
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    finally:
        archive.unlink(missing_ok=True)
        shutil.rmtree(dest / "__pycache__", ignore_errors=True)


def digest_of_version(sha: str) -> str:
    """The firmware-verifiable digest of a COMMITTED kernel version — computed from a git
    archive of the sha (exactly what the firmware deploys) so it matches by construction,
    not from the staging working tree (whose line endings the host git could differ on)."""
    import shutil
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="quine-kdigest-"))
    try:
        deploy_kernel(sha, tmp)
        return digest_of(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def seed_kernel() -> str:
    """Import the live `kernel/` as kernel-version v1 if the store is empty. Returns the sha.
    The tree is committed WITH its `kernel/` prefix so digests are layout-stable."""
    import shutil
    import tempfile

    ensure_kernel_repo()
    if has_kernel_history():
        return kernel_head()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="quine-kernel-seed-"))
    try:
        shutil.copytree(KERNEL_SEED, tmp / _STAGE_SUBDIR, ignore=versioning._IGNORE)
        versioning._git(["init", "-b", "main"], cwd=tmp)
        versioning._git(["add", "-A"], cwd=tmp)
        versioning._git(["commit", "-m", "seed: initial kernel"], cwd=tmp)
        versioning._git(["remote", "add", "origin", str(KERNEL_VERSIONS_GIT)], cwd=tmp)
        versioning._git(["push", "origin", "main"], cwd=tmp)
        sha = versioning._git(["rev-parse", "HEAD"], cwd=tmp).strip()
        _record_version(sha, parent=None, message="seed: initial kernel", origin="seed",
                        digest=digest_of_version(sha), status="active")
        return sha
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def create_kernel_staging(task_id: str) -> pathlib.Path:
    """A fresh working clone of kernel.git for the agent to edit (holds `<staging>/kernel/`)."""
    seed_kernel()
    staging = state_store.TASKS_DIR / task_id / "kernel_staging"
    if staging.exists():
        versioning._force_rmtree(staging)
    staging.parent.mkdir(parents=True, exist_ok=True)
    versioning._git(["clone", "--quiet", str(KERNEL_VERSIONS_GIT), str(staging)])
    return staging


def commit_kernel_staging(staging: pathlib.Path, message: str, *, task_id: str,
                          prompt: str) -> tuple[str, str] | None:
    """Commit + push the edited kernel staging as a new kernel version. Returns
    (sha, digest) or None if the agent changed nothing."""
    versioning._git(["add", "-A"], cwd=staging)
    if not versioning._git(["status", "--porcelain"], cwd=staging).strip():
        return None
    versioning._git(["commit", "-m", message], cwd=staging)
    sha = versioning._git(["rev-parse", "HEAD"], cwd=staging).strip()
    parent_res = versioning._run(["rev-parse", "--verify", "--quiet", "HEAD^"], cwd=staging)
    parent = parent_res.stdout.strip() if parent_res.returncode == 0 else None
    versioning._git(["push", "origin", "main"], cwd=staging)
    versioning._git(["push", "origin", f"HEAD:refs/heads/kv_{sha[:8]}"], cwd=staging)
    digest = digest_of_version(sha)  # archive-based: matches what the firmware will deploy
    _record_version(sha, parent=parent, message=message, origin="kernel-self-mod",
                    task_id=task_id, prompt=prompt, digest=digest, status="committed")
    return sha, digest


# ── metadata index (state/kernel_versions.json) ────────────────────────────────────────
def _record_version(sha: str, *, parent: str | None, message: str, origin: str,
                    digest: str, status: str, task_id: str | None = None,
                    prompt: str | None = None) -> None:
    reg = state_store.read_kernel_versions()
    if sha in reg["versions"]:
        return
    seq = int(reg.get("next_seq", 1))
    reg["versions"][sha] = {
        "sha": sha, "short": sha[:8], "seq": seq, "parent": parent, "origin": origin,
        "message": message, "digest": digest, "task": task_id,
        "prompt": (prompt or "")[:300] or None, "created_at": round(time.time(), 3),
        "status": status, "history": [{"status": status, "t": round(time.time(), 3)}],
    }
    reg["next_seq"] = seq + 1
    state_store.write_kernel_versions(reg)


def set_kernel_status(sha: str, status: str, **fields: Any) -> None:
    reg = state_store.read_kernel_versions()
    entry = reg["versions"].get(sha)
    if entry is None:
        return
    entry["history"].append({"status": status, "t": round(time.time(), 3), **fields})
    entry["status"] = status
    state_store.write_kernel_versions(reg)


def get_kernel_version(sha: str) -> dict[str, Any] | None:
    return state_store.read_kernel_versions()["versions"].get(sha)


def seed_version() -> dict[str, Any] | None:
    """The kv1 SEED entry — the shipped kernel (origin='seed', lowest seq). This is the
    rollback-to-shipped target; NOT `seed_kernel()`/`kernel_head()`, which returns the tip
    of the version line (the just-promoted version) once history exists."""
    versions = list(state_store.read_kernel_versions()["versions"].values())
    if not versions:
        return None
    seeds = [v for v in versions if v.get("origin") == "seed"]
    pool = seeds or versions
    return min(pool, key=lambda v: v.get("seq") or 0)


def list_kernel_versions() -> list[dict[str, Any]]:
    reg = state_store.read_kernel_versions()
    active = (state_store.read_active_kernel() or {}).get("version")
    rows = sorted(reg["versions"].values(), key=lambda v: -(v.get("seq") or 0))
    for r in rows:
        r["is_active"] = r["sha"] == active
    return rows


# ── validation (stricter than an app change) ───────────────────────────────────────────
def _pyfiles(staging: pathlib.Path) -> list[str]:
    return [str(p) for p in (staging / _STAGE_SUBDIR).rglob("*.py")
            if "__pycache__" not in p.parts]


def _smoke_env(staging: pathlib.Path) -> dict[str, str]:
    """Env for validating the CANDIDATE kernel: candidate on PYTHONPATH first (so `import
    kernel` resolves to it), then ROOT (so `bootstrap`/`tests`/`app` resolve); secrets
    stripped; an isolated state home; and QUINE_APP_SEED so the candidate can seed the app."""
    env = state_store.stripped_env()
    root = str(state_store.ROOT)
    env["PYTHONPATH"] = os.pathsep.join([str(staging), root])
    env["QUINE_APP_SEED"] = str(state_store.ROOT / "app")
    return env


def validate_kernel_staging(staging: pathlib.Path) -> tuple[bool, str]:
    """Pre-commit gate for a kernel candidate: syntax-compile, import-smoke the candidate
    kernel package, then run the curated kernel test subset against it. Any failure blocks."""
    env = _smoke_env(staging)

    py = _pyfiles(staging)
    if py:
        res = subprocess.run([sys.executable, "-m", "py_compile", *py],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", env=env, creationflags=CHILD_CREATIONFLAGS)
        if res.returncode != 0:
            return False, "Kernel syntax errors:\n" + _truncate(res.stderr)

    smoke = ("import kernel.core, kernel.gateway, kernel.bootloader, kernel.watchdog, "
             "kernel.versioning, kernel.state_store, kernel.agent_runtime, kernel.kernelmod")
    res = subprocess.run([sys.executable, "-c", smoke], cwd=str(staging),
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", env=env, creationflags=CHILD_CREATIONFLAGS)
    if res.returncode != 0:
        return False, "Candidate kernel failed import-smoke:\n" + _truncate(res.stderr)

    ok, report = _run_curated_tests(staging, env)
    if not ok:
        return False, report
    return True, "kernel validation passed"


def _run_curated_tests(staging: pathlib.Path, env: dict[str, str]) -> tuple[bool, str]:
    """Run the curated kernel test subset with the candidate kernel on PYTHONPATH and its
    own isolated state home (conftest respects QUINE_KERNEL_VALIDATION). Skips cleanly if
    pytest can't be launched (infra gap, not a bad candidate)."""
    tests = (os.environ.get("QUINE_KERNEL_VALIDATION_TESTS", "").split()
             or CURATED_KERNEL_TESTS)
    tests = [t for t in tests if t and (state_store.ROOT / t).exists()]
    if not tests:
        return True, "no curated tests"
    import tempfile

    home = tempfile.mkdtemp(prefix="quine-kernel-val-")
    run_env = {**env, "QUINE_STATE_HOME": home, "QUINE_KERNEL_VALIDATION": "1"}
    try:
        res = subprocess.run(
            [sys.executable, "-m", "pytest", *tests, "-x", "-q", "-o", "addopts=",
             "-p", "no:cacheprovider"],
            cwd=str(state_store.ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=run_env, timeout=600,
            creationflags=CHILD_CREATIONFLAGS,
        )
    except subprocess.TimeoutExpired:
        return False, "curated kernel tests timed out"
    except Exception as exc:
        return True, f"curated kernel tests skipped ({exc})"
    finally:
        import shutil
        shutil.rmtree(home, ignore_errors=True)
    if res.returncode != 0:
        return False, "Candidate kernel failed the curated test subset:\n" + _truncate(
            res.stdout + "\n" + res.stderr)
    return True, "curated tests passed"


# ── the kernel-resident agent driver (ring 0 authors ring 0) ───────────────────────────
_KERNEL_MARKER = re.compile(r"__KERNEL_EDIT__\s+([^\s:]+)::(.+?)(?=$|__KERNEL_)", re.DOTALL)
_KERNEL_SYSTEM = """\
You are editing the Quine KERNEL (ring 0) in a staging tree. Files live under `kernel/`.
Make the smallest change that satisfies the request. The kernel must keep importing and
keep booting the app + serving GET /health. Use read_file/write_file, then propose_commit.
Return ONLY tool calls. Never touch anything outside `kernel/`."""


async def run_kernel_agent(task_id: str, prompt: str, staging: pathlib.Path,
                           config: dict, emit) -> dict[str, Any]:
    """Drive an edit of the kernel staging. Offline (scripted engine) it applies
    `__KERNEL_EDIT__ <relpath>::<text>` markers (append text to the file) or emits invalid
    Python for `__KERNEL_BREAK__`; with a real model it runs a minimal read/write/propose
    tool loop over the kernel-held llm.chat primitive. Returns {proposed, message}."""
    engine = (config.get("agent", {}) or {}).get("engine", "scripted")
    if engine == "scripted":
        return _scripted_kernel_edit(prompt, staging, emit)
    return await _litellm_kernel_edit(task_id, prompt, staging, config, emit)


def _resolve(staging: pathlib.Path, rel: str) -> pathlib.Path | None:
    # Contain every write to <staging>/kernel/** — the agent can never escape the tree, and
    # can only touch the kernel subdir (not e.g. a planted .git/hooks).
    full = policy.resolve_within(staging, rel)
    if full is None:
        return None
    try:
        full.relative_to(staging / _STAGE_SUBDIR)
    except ValueError:
        return None
    return full


def _scripted_kernel_edit(prompt: str, staging: pathlib.Path, emit) -> dict[str, Any]:
    if "__KERNEL_BREAK__" in prompt:
        target = staging / _STAGE_SUBDIR / "core.py"
        target.write_text(target.read_text(encoding="utf-8")
                          + "\n\ndef __kernel_broken__( this is not valid python\n",
                          encoding="utf-8")
        emit({"kind": "tool_call", "name": "write_file", "summary": "kernel/core.py (broken)"})
        return {"proposed": True, "message": "scripted: kernel break"}
    applied = False
    for m in _KERNEL_MARKER.finditer(prompt):
        rel, text = m.group(1).strip(), m.group(2)
        full = _resolve(staging, rel)
        if full is None or not full.exists():
            continue
        full.write_text(full.read_text(encoding="utf-8") + "\n" + text.strip() + "\n",
                        encoding="utf-8")
        emit({"kind": "tool_call", "name": "write_file", "summary": rel})
        applied = True
    if not applied:  # default benign edit: append a comment to a standalone changelog MODULE
        # (a .py, so it actually changes the digest the firmware swaps on — a non-.py edit
        # would be a no-op at the firmware layer). Comment-only ⇒ valid Python, imported by
        # nothing, so it can never affect kernel behavior.
        note = staging / _STAGE_SUBDIR / "_kernel_changelog.py"
        prior = note.read_text(encoding="utf-8") if note.exists() else "# Kernel change log\n"
        note.write_text(prior + f"# {prompt[:80]}\n", encoding="utf-8")
        emit({"kind": "tool_call", "name": "write_file", "summary": "kernel/_kernel_changelog.py"})
    return {"proposed": True, "message": "scripted kernel: " + prompt[:60]}


_KERNEL_TOOLS = [
    {"type": "function", "function": {"name": "read_file", "description": "Read a kernel file",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Overwrite a kernel file",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"},
                    "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "propose_commit",
     "description": "Finish and propose the change",
     "parameters": {"type": "object", "properties": {"message": {"type": "string"}},
                    "required": ["message"]}}},
]


async def _litellm_kernel_edit(task_id: str, prompt: str, staging: pathlib.Path,
                               config: dict, emit) -> dict[str, Any]:
    model = (config.get("agent", {}) or {}).get("model", "")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _KERNEL_SYSTEM},
        {"role": "user", "content": f"Change request for the kernel:\n{prompt}\n\n"
         f"Files present: {sorted(p.name for p in (staging / _STAGE_SUBDIR).glob('*.py'))}"},
    ]
    for _ in range(_KERNEL_AGENT_MAX_STEPS):
        resp = await llm.chat(model, messages, tools=_KERNEL_TOOLS, temperature=0.0)
        msg = resp["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            messages.append({"role": "user", "content": "Use a tool, or propose_commit."})
            continue
        messages.append(msg)
        for tc in calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result, done, message = _apply_kernel_tool(name, args, staging, emit)
            messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                             "name": name, "content": result})
            if done:
                return {"proposed": True, "message": message}
    return {"proposed": False, "message": "kernel agent did not propose a commit"}


def _apply_kernel_tool(name: str, args: dict, staging: pathlib.Path, emit
                       ) -> tuple[str, bool, str]:
    if name == "propose_commit":
        emit({"kind": "propose", "summary": args.get("message", "kernel change")})
        return "ok", True, args.get("message") or "kernel change"
    rel = str(args.get("path") or "")
    full = _resolve(staging, rel)
    if full is None:
        return f"error: path {rel!r} is outside kernel/", False, ""
    if name == "read_file":
        emit({"kind": "tool_call", "name": "read_file", "summary": rel})
        try:
            return _truncate(full.read_text(encoding="utf-8")), False, ""
        except OSError as exc:
            return f"error: {exc}", False, ""
    if name == "write_file":
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(str(args.get("content") or ""), encoding="utf-8")
        emit({"kind": "tool_call", "name": "write_file", "summary": rel})
        return "written", False, ""
    return f"error: unknown tool {name}", False, ""
