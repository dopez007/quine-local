"""Kernel entry point: start the gateway server.

Launched by the firmware as `python -m kernel`. Serves the gateway (reverse proxy +
syscall boundary) on the configured host/port; the gateway's startup hook boots the
rest of the kernel (state, versioning, first app slot).
"""

from __future__ import annotations

import os

import uvicorn

from kernel import state_store


def main() -> None:
    # Harden the kernel process FIRST (before anything reads secrets): on Linux this marks it
    # non-dumpable so a same-UID agent child can't read the kernel's /proc environ. No-op elsewhere.
    state_store.harden_process()
    # First-boot self-provisioning from env (writes secrets.env + seeds engine/model) MUST
    # run before load_config() seeds config.yaml. No-op when the provisioning env is unset.
    state_store.provision_from_env()
    # Hardened mode (QUINE_KERNEL_HARDENED=1): encrypt secrets.env at rest and PROVE fail-closed
    # that the agent can't reach the keys (disk + /proc). Refuses to boot if the boundary doesn't
    # hold. Opportunistic no-op when unhardened, so local dev / tests are unaffected.
    state_store.enforce_secret_hardening()
    cfg = state_store.load_config()
    # KERNEL_BIND_HOST lets a container bind 0.0.0.0 without editing config; the default
    # stays 127.0.0.1 so local runs remain loopback-only. (App slots are always reached
    # over loopback inside the host/container, so only the gateway's edge bind changes.)
    host = os.environ.get("KERNEL_BIND_HOST") or cfg["kernel"]["host"]
    port = int(cfg["kernel"]["port"])
    # Fail closed when KERNEL_REQUIRE_AUTH=1: a misconfigured deployment with a missing token
    # refuses to serve rather than exposing an open kernel. Local development leaves it unset.
    if os.environ.get("KERNEL_REQUIRE_AUTH", "").strip().lower() in ("1", "true", "yes", "on") \
            and not os.environ.get("KERNEL_AUTH_TOKEN"):
        print("[kernel] KERNEL_REQUIRE_AUTH is set but KERNEL_AUTH_TOKEN is missing — "
              "refusing to start (would serve an unauthenticated kernel)", flush=True)
        raise SystemExit(1)
    print(f"[kernel] serving gateway on http://{host}:{port}", flush=True)
    # Construct the uvicorn Server explicitly (not uvicorn.run) so a kernel-update approval can
    # stop it cleanly (gateway sets server.should_exit) and we can exit with a distinct code the
    # firmware recognizes as "restart for a kernel update" (bootstrap.kernel_slots.RESTART_CODE).
    from bootstrap.kernel_slots import RESTART_CODE
    from kernel import gateway

    server = uvicorn.Server(uvicorn.Config(gateway.app, host=host, port=port, log_level="info"))
    gateway.app.state.server = server
    server.run()

    kernel = getattr(gateway.app.state, "kernel", None)
    if kernel is not None and getattr(kernel, "restart_requested", False):
        print("[kernel] exiting for kernel update — firmware will apply + health-gate", flush=True)
        raise SystemExit(RESTART_CODE)


if __name__ == "__main__":
    main()
