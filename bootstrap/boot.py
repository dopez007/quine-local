"""Firmware entry point.

Run with:  uv run python -m bootstrap.boot

Responsibilities (kept deliberately minimal):
  1. Verify kernel-image integrity (P2.6) before each launch — when the operator pins an
     expected hash or a signature (via the environment), a tampered kernel fails CLOSED and the
     firmware refuses to launch it. With nothing configured this is observability-only (the
     digest is still logged). See bootstrap/integrity.py.
  2. Launch the kernel as a child process and supervise it — if the kernel exits unexpectedly,
     reboot it (with backoff). This is the "power button + reset".

The kernel itself (ring 0) does the real work: serving the gateway, managing app
slots, and driving the self-modification pipeline.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from bootstrap import integrity, kernel_slots

ROOT = integrity.ROOT


def _load_boot_secrets() -> None:
    """Load deployment secrets from a read-only boot file into the environment before launch.

    QUINE_BOOT_SECRETS_FILE avoids putting provider keys, an edge token, or QUINE_SECRET_KEY in
    container configuration that host inspection can read. Values are line-oriented (KEY=VALUE,
    split on the first '=' so a value may contain '=') and enter the kernel only through its spawn
    environment. The firmware stays self-contained, with no kernel import. No-op when absent.
    """
    path = os.environ.get("QUINE_BOOT_SECRETS_FILE")
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.rstrip("\n")
                if not line or line.lstrip().startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ[key.strip()] = val
    except OSError as exc:
        print(f"[bootstrap] warning: could not read boot secrets file: {exc}", flush=True)


def _set_undumpable_if_hardened() -> None:
    """In hardened mode, mark THIS firmware process (pid 1 in a container) non-dumpable on Linux.

    Provider keys and QUINE_SECRET_KEY arrive in the container's env, so they live in this process's
    /proc/<pid>/environ (an initial-env snapshot that os.environ.pop can't scrub). The kernel makes
    ITSELF non-dumpable, but its firmware parent would otherwise still expose the same secrets via
    /proc/1/environ to a same-UID agent. Close that too. Best-effort; no-op off Linux or unhardened.
    Deliberately self-contained (no kernel import) to keep the firmware layer dependency-free."""
    if sys.platform != "linux":
        return
    if os.environ.get("QUINE_KERNEL_HARDENED", "").strip().lower() not in ("1", "true", "yes", "on"):
        return
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 4
    except Exception as exc:
        print(f"[bootstrap] warning: could not set firmware non-dumpable: {exc}", flush=True)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--print-hash" in argv:  # operator helper: print the digest to pin (KERNEL_EXPECTED_HASH)
        print(integrity.compute_digest())
        return 0

    _load_boot_secrets()           # secrets arrive via a RO mount, not container env (inspect-safe)
    _set_undumpable_if_hardened()  # before we do anything else that could spawn a readable child
    print("[bootstrap] Quine firmware starting", flush=True)
    print(f"[bootstrap] kernel digest: {integrity.compute_digest()[:16]}…", flush=True)

    supervise = os.environ.get("QUINE_SUPERVISE", "1") != "0"
    backoff = 1.0
    consecutive_kernel_failures = 0  # crashes of a promoted (active) kernel → fall back to seed

    sd = kernel_slots.state_dir(ROOT)

    while True:
        # Gated Kernel Self-Update: if the operator has promoted a kernel version, reconcile
        # the on-disk kernel to it (deploy from state/kernel.git, verify digest + signature,
        # swap in place) BEFORE the integrity check + launch. When no active kernel is pinned
        # this is a no-op, so a system not using the feature behaves exactly as before.
        prep = {"swapped": False, "to_version": None}
        if kernel_slots.active_path(sd).exists():
            prep = kernel_slots.prepare_kernel(ROOT, os.environ)

        # Re-verify before every (re)launch so a kernel tampered with at runtime can't be loaded
        # by triggering a reboot. Fail CLOSED when an expectation is configured.
        ok, detail = integrity.verify()
        print(f"[bootstrap] kernel integrity: {'ok' if ok else 'FAILED'} — {detail}", flush=True)
        if not ok:
            print("[bootstrap] refusing to launch — kernel integrity verification failed",
                  flush=True)
            return 3

        print("[bootstrap] launching kernel (python -m kernel)", flush=True)
        proc = subprocess.Popen([sys.executable, "-m", "kernel"], cwd=str(ROOT))

        # Right after applying a kernel UPDATE, health-gate the new kernel: if it doesn't come
        # up healthy, roll back to the previous kernel and relaunch — unattended. A normal
        # launch (no swap) is not gated, preserving the original supervise behavior.
        if prep["swapped"]:
            active = kernel_slots.read_json(kernel_slots.active_path(sd)) or {}
            timeout = float(active.get("boot_health_seconds")
                            or os.environ.get("QUINE_KERNEL_HEALTH_SECONDS") or 60)
            healthy, why = kernel_slots.poll_health(
                kernel_slots.port_from_config(sd), timeout, lambda: proc.poll() is None)
            if not healthy:
                print(f"[bootstrap] new kernel {str(prep['to_version'])[:12]} failed health "
                      f"gate ({why}); rolling back", flush=True)
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                # The breadcrumb is written ONLY on failure — the OLD kernel we're about to
                # relaunch consumes it at boot to mark the bad version failed + audit the
                # rollback. A healthy promote needs no breadcrumb: the new kernel self-detects
                # it (its running digest matches active_kernel.json — see core._reconcile…).
                kernel_slots.record_boot_result(ROOT, prep["to_version"], False, why)
                kernel_slots.rollback_after_health_failure(ROOT, prep)
                backoff = 1.0
                continue  # relaunch the restored (previous good) kernel immediately
            print(f"[bootstrap] new kernel {str(prep['to_version'])[:12]} is healthy — promoted",
                  flush=True)
            consecutive_kernel_failures = 0

        try:
            code = proc.wait()
        except KeyboardInterrupt:
            print("[bootstrap] shutdown requested; terminating kernel", flush=True)
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            return 0

        print(f"[bootstrap] kernel exited with code {code}", flush=True)
        # Intentional restart for a kernel update: relaunch immediately (no backoff) so the
        # reconcile above applies the active_kernel.json the kernel just wrote.
        if code == kernel_slots.RESTART_CODE:
            print("[bootstrap] kernel requested restart for update — reconciling", flush=True)
            backoff = 1.0
            continue
        if not supervise or code == 0:
            return code
        # A promoted kernel that crashes on boot repeatedly (rather than failing the health
        # gate) is the other way it can be broken — after a few crashes, fall back to the
        # shipped kernel (the ultimate recovery floor) so the system can never wedge itself.
        if kernel_slots.active_path(sd).exists():
            consecutive_kernel_failures += 1
            if consecutive_kernel_failures >= kernel_slots._MAX_ACTIVE_FAILURES:
                kernel_slots.fall_back_to_seed(ROOT)
                consecutive_kernel_failures = 0
        print(f"[bootstrap] rebooting kernel in {backoff:.0f}s", flush=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


if __name__ == "__main__":
    raise SystemExit(main())
