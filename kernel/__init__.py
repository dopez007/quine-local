"""Kernel layer (ring 0 / protected supervisor).

This package owns everything the agent must NOT touch: the version history, the A/B
slot pointers, the audit log, secrets, and the self-modification pipeline. It exposes
a narrow syscall surface to user space (the `app/` layer) via the gateway.

Per the microkernel principle, the kernel provides *mechanism* only. Application
features are built by the agent in `app/`.
"""
