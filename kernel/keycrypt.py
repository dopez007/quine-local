"""At-rest encryption for the kernel's provider secrets (`state/secrets.env`).

Why this exists: env-stripping keeps provider keys out of the agent's *process environment*,
but the agent shares the kernel's OS user and — absent this — could read the plaintext
`state/secrets.env` straight off disk (see `state_store.stripped_env`). When a `QUINE_SECRET_KEY`
is configured (hardened deployments), the kernel encrypts that file at rest so a same-container
read yields ciphertext, not keys. The decryption key lives only in the kernel's environment
(and is stripped from every child), so ring-3 code never holds it.

The kernel derives a Fernet key from a SHA-256 digest of its configured secret. This implementation
stays local to the protected kernel so the mutable application never receives the decryption key.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

SECRET_KEY_ENV = "QUINE_SECRET_KEY"

# Fallback used only when no key is configured: a random per-process key. Ciphertext written
# with it cannot be read back after a restart — which is exactly why `configured()` gates
# whether we encrypt at all (we never persist something we can't decrypt on the next boot).
_EPHEMERAL = base64.urlsafe_b64encode(os.urandom(32)).decode()


def _fernet() -> Fernet:
    secret = os.environ.get(SECRET_KEY_ENV) or _EPHEMERAL
    # Derive a valid 32-byte urlsafe-base64 Fernet key deterministically from the secret.
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def configured() -> bool:
    """True when a persistent `QUINE_SECRET_KEY` is set, so encrypted blobs survive a restart.
    When False the kernel keeps secrets.env in plaintext (legacy / local-dev behavior)."""
    return bool(os.environ.get(SECRET_KEY_ENV))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str | None:
    """Return the plaintext, or None if the token can't be decrypted (wrong/missing key)."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return None
