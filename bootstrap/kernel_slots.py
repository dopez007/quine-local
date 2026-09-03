"""Firmware-side kernel A/B slot management (the "BIOS updates the kernel" mechanism).

This is the firmware's half of Gated Kernel Self-Update. The kernel is normally immutable;
when the operator promotes a new kernel *version* (agent-authored, validated, and approved
in ring 0 — see `kernel/kernelmod.py`), the running kernel records the target in
protected `state/active_kernel.json` and restarts. The FIRMWARE — the one truly immutable
layer, PID 1 in the container — is the sole authority that then swaps `kernel/` on disk,
health-gates the new kernel, and AUTO-ROLLS-BACK to the previous (and ultimately the
shipped) kernel if it won't come up healthy. This recurses the app-slot safety model one
level down: the firmware gates the kernel exactly as the kernel gates the app.

Kept deliberately self-contained (stdlib + `bootstrap.integrity` only — NO kernel import),
so the firmware stays a small, auditable, dependency-light root of trust.

The source of truth is `state/active_kernel.json` = {version, digest, signature?} — the
kernel version that SHOULD run — reconciled against the on-disk `kernel/` at every boot:
  • absent, or digest == on-disk → in sync; launch as-is (the shipped kernel is the floor);
  • differs → deploy `version` from `state/kernel.git`, VERIFY it (digest match + ed25519
    signature in signed mode), atomically swap it into place, health-gate, else roll back.

The active kernel runs IN PLACE at `<root>/kernel` so the kernel's ROOT-anchored paths
(`state_store.ROOT`, `APP_SEED`, …) are unchanged; the firmware is the only writer of it.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Mapping

from bootstrap import integrity

# Intentional kernel exit code meaning "restart me now for a kernel update" — the firmware
# relaunches immediately (no crash backoff) and reconciles the pending active_kernel.json.
RESTART_CODE = 42

# Detached children (no console) — mirrors kernel.util.CHILD_CREATIONFLAGS, inlined because
# the firmware must stay import-free of the kernel package. The constant only exists on
# Windows; 0 = no-op elsewhere.
_CREATIONFLAGS: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# How many consecutive health failures of a NON-swapped active kernel before the firmware
# gives up on it and falls back to the shipped seed kernel (the ultimate recovery floor).
_MAX_ACTIVE_FAILURES = 3


def state_dir(root: pathlib.Path) -> pathlib.Path:
    """The protected state partition. Mirrors state_store's QUINE_STATE_HOME relocation
    (used by the test suite) WITHOUT importing the kernel."""
    home = os.environ.get("QUINE_STATE_HOME")
    base = pathlib.Path(home).resolve() if home else root
    return base / "state"


def _kernel_git(sd: pathlib.Path) -> pathlib.Path:
    return sd / "kernel.git"


def active_path(sd: pathlib.Path) -> pathlib.Path:
    return sd / "active_kernel.json"


def active_prev_path(sd: pathlib.Path) -> pathlib.Path:
    return sd / "active_kernel_prev.json"


def read_json(path: pathlib.Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: pathlib.Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def port_from_config(sd: pathlib.Path) -> int:
    """The gateway port, for health-polling — read from state/config.yaml with a tiny
    line parse (no yaml dependency in the firmware). Env override, else 8000."""
    env_port = os.environ.get("QUINE_KERNEL_HEALTH_PORT")
    if env_port and env_port.isdigit():
        return int(env_port)
    try:
        in_kernel = False
        for raw in (sd / "config.yaml").read_text(encoding="utf-8").splitlines():
            if raw[:1] not in (" ", "\t", "#", ""):  # a top-level key like "kernel:"
                in_kernel = raw.strip().startswith("kernel:")
            elif in_kernel and raw.strip().startswith("port:"):
                tok = raw.split(":", 1)[1].strip()
                if tok.isdigit():
                    return int(tok)
    except OSError:
        pass
    return 8000


def current_digest(root: pathlib.Path) -> str:
    return integrity.compute_digest(root)


# ── deploy + verify a candidate kernel version ─────────────────────────────────────────
def deploy_version(sd: pathlib.Path, version: str, dest: pathlib.Path,
                   env: Mapping[str, str]) -> None:
    """Materialize a kernel version's tree (a `kernel/` dir + siblings) into `dest` from
    state/kernel.git via `git archive` — no working checkout, no hooks.

    The git config is pinned deterministically (the kernel's own gitconfig, no system config,
    `core.autocrlf=false`/`core.eol=lf`) so the archived bytes are IDENTICAL to what the kernel
    hashed at commit time. Without this, a machine with `core.autocrlf=true` would re-encode
    line endings and the digest wouldn't match the approved one — the firmware would (correctly
    but unhelpfully) reject every promotion. The kernel commits its digest the same way."""
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest.parent / f".kernel-deploy-{version[:12]}.zip"
    git_env = dict(env)
    git_env["GIT_CONFIG_GLOBAL"] = str(sd / "gitconfig")  # the kernel-owned config (versioning._GITCONFIG)
    git_env["GIT_CONFIG_NOSYSTEM"] = "1"
    try:
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf",
             "--git-dir", str(_kernel_git(sd)), "archive", "--format=zip",
             "-o", str(archive), version],
            check=True, capture_output=True, env=git_env,
            creationflags=_CREATIONFLAGS,
        )
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    finally:
        archive.unlink(missing_ok=True)


def verify_candidate(cand_root: pathlib.Path, active: dict,
                     env: Mapping[str, str]) -> tuple[bool, str]:
    """Firmware gate before a swap: the deployed tree's digest must equal the approved
    `active.digest`, AND — in signed mode (KERNEL_INTEGRITY_PUBKEY set) — `active.signature`
    must be a valid ed25519 signature over that digest. Signed mode fails CLOSED if the
    signature is missing/invalid, so even a compromised running kernel can't promote a new
    kernel without the operator's offline private key."""
    expected = (active.get("digest") or "").strip().lower()
    got = current_digest(cand_root)
    if got != expected:
        return False, f"digest mismatch (approved {expected[:16]}…, deployed {got[:16]}…)"
    pubkey = (env.get("KERNEL_INTEGRITY_PUBKEY") or "").strip()
    if pubkey:
        sig = (active.get("signature") or "").strip()
        if not sig or not integrity._verify_signature(expected, pubkey, sig):
            return False, "signed mode: missing or invalid operator signature for kernel digest"
    return True, "verified"


# ── the atomic in-place swap (both dirs under `root`, same filesystem) ──────────────────
def apply_swap(root: pathlib.Path, cand_root: pathlib.Path) -> pathlib.Path:
    """Swap the deployed candidate's `kernel/` into place, backing up the current one.
    Returns the backup path (the previous good kernel, kept for rollback)."""
    live = root / "kernel"
    backup = root / ".kernel_old"
    incoming = cand_root / "kernel"
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    # Move the incoming tree onto the SAME filesystem as `live` first, so the final swap is
    # two local renames (atomic), never a cross-device copy leaving a half-written kernel/.
    staged = root / ".kernel_incoming"
    if staged.exists():
        shutil.rmtree(staged, ignore_errors=True)
    shutil.copytree(incoming, staged)
    os.replace(live, backup)      # kernel/ -> .kernel_old
    os.replace(staged, live)      # .kernel_incoming -> kernel/
    return backup


def restore_swap(root: pathlib.Path, backup: pathlib.Path) -> None:
    """Undo a swap: move the bad kernel aside and restore the backed-up previous kernel."""
    live = root / "kernel"
    bad = root / ".kernel_bad"
    if bad.exists():
        shutil.rmtree(bad, ignore_errors=True)
    if live.exists():
        os.replace(live, bad)
    os.replace(backup, live)


# ── health gating ──────────────────────────────────────────────────────────────────────
def poll_health(port: int, timeout: float, is_alive, interval: float = 0.5) -> tuple[bool, str]:
    """Poll the gateway /health until 200, the process dies, or we time out. `is_alive()`
    lets the firmware notice a kernel that crashed outright without waiting the full window."""
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_alive():
            return False, "kernel process exited before becoming healthy"
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 (loopback only)
                if resp.status == 200:
                    return True, "healthy"
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(interval)
    return (False, "kernel process exited before becoming healthy") if not is_alive() \
        else (False, f"health check timed out after {timeout:.0f}s")


# ── the reconcile the firmware runs before each launch ─────────────────────────────────
def prepare_kernel(root: pathlib.Path, env: Mapping[str, str], log=print) -> dict:
    """Make `<root>/kernel` match state/active_kernel.json. Returns a record of what
    happened: {ok, swapped, backup, to_version, from_digest, reason}. On a verify failure
    the on-disk kernel is left untouched (falls back to whatever is there — the shipped
    floor); the caller still launches it. Only a SUCCESSFUL swap sets swapped=True (so the
    caller knows a health failure should roll it back)."""
    sd = state_dir(root)
    active = read_json(active_path(sd))
    on_disk = current_digest(root)
    if not active or (active.get("digest") or "").lower() == on_disk:
        return {"ok": True, "swapped": False, "backup": None,
                "to_version": None, "from_digest": on_disk, "reason": "in sync"}

    version = active.get("version") or ""
    cand = root / ".kernel_next"
    try:
        deploy_version(sd, version, cand, env)
    except Exception as exc:  # missing repo/version → do NOT touch the live kernel
        log(f"[bootstrap] kernel deploy failed for {version[:12]}: {exc}; keeping current kernel")
        return {"ok": False, "swapped": False, "backup": None, "to_version": version,
                "from_digest": on_disk, "reason": f"deploy failed: {exc}"}

    ok, reason = verify_candidate(cand, active, env)
    if not ok:
        shutil.rmtree(cand, ignore_errors=True)
        log(f"[bootstrap] kernel candidate {version[:12]} REJECTED: {reason}; keeping current kernel")
        return {"ok": False, "swapped": False, "backup": None, "to_version": version,
                "from_digest": on_disk, "reason": reason}

    backup = apply_swap(root, cand)
    shutil.rmtree(cand, ignore_errors=True)
    log(f"[bootstrap] swapped in kernel {version[:12]} (was {on_disk[:12]}…); health-gating")
    return {"ok": True, "swapped": True, "backup": str(backup), "to_version": version,
            "from_digest": on_disk, "reason": "swapped"}


def rollback_after_health_failure(root: pathlib.Path, prep: dict, log=print) -> None:
    """A freshly-swapped kernel failed its health gate: restore the previous kernel and
    revert active_kernel.json to the previous good entry so the next boot is stable."""
    backup = prep.get("backup")
    if backup:
        restore_swap(root, pathlib.Path(backup))
    sd = state_dir(root)
    prev = read_json(active_prev_path(sd))
    if prev:
        _write_json(active_path(sd), prev)
    else:
        active_path(sd).unlink(missing_ok=True)  # nothing before it → shipped kernel is active
    log("[bootstrap] rolled back kernel to previous good version after health failure")


def fall_back_to_seed(root: pathlib.Path, log=print) -> None:
    """Last resort: the active kernel itself keeps failing health with no swap to undo.
    Clear the pointer so the firmware launches the shipped image kernel (the floor)."""
    active_path(state_dir(root)).unlink(missing_ok=True)
    log("[bootstrap] cleared active kernel pointer — falling back to the shipped kernel")


def record_boot_result(root: pathlib.Path, version: str | None, ok: bool, reason: str) -> None:
    """Drop a breadcrumb the KERNEL reconciles at its next boot into the hash-chained audit
    log + kernel_versions.json status. The firmware can't touch those (no kernel import, and
    the audit chain is kernel-owned), so it records the outcome of a swap here and the kernel
    turns it into `kernel_promoted` / `kernel_health_failed` + audit. Best-effort."""
    if not version:
        return
    try:
        _write_json(state_dir(root) / "kernel_boot_result.json",
                    {"version": version, "ok": ok, "reason": reason, "ts": time.time()})
    except OSError:
        pass
