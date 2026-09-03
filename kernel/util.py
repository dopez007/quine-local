"""Small kernel-internal helpers."""

from __future__ import annotations

import socket
import subprocess

# Kernel children never use the console: their stdio is always redirected (slot logs,
# pipes, capture_output). CREATE_NO_WINDOW detaches them from the parent's console so
# dozens of short-lived children (app slots, workers, git, nested pytest) can't churn
# the ConPTY — concurrent attach/detach across parallel test workers corrupts Windows
# Terminal until restart. getattr: the constant only exists on Windows; 0 = no-op elsewhere.
CHILD_CREATIONFLAGS: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def free_port() -> int:
    """Ask the OS for an unused localhost TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
