"""Kernel-image integrity (P2.6): verify ring-0 hasn't been tampered with.

The kernel tree is immutable by design — only `app/` is agent-mutable — so before launching the
kernel the firmware hashes `kernel/` and checks it against an operator expectation delivered via
the environment. Putting the expectation in the environment (not a file) matters: an attacker who
could rewrite `kernel/` on disk could also rewrite an on-disk expected hash, but not one supplied
at launch.

Two enforcement modes, both fail-CLOSED when configured (a mismatch aborts boot):
  • Pinned hash  — `KERNEL_EXPECTED_HASH=<sha256 hex>`. The deployment supplies the kernel hash
    together with any configured `KERNEL_AUTH_TOKEN`.
  • Signed hash  — `KERNEL_INTEGRITY_PUBKEY=<base64 ed25519 public key>` plus
    `KERNEL_INTEGRITY_SIG=<base64 signature over the lowercase hex digest>`. The digest need not
    be trusted in transit — the signature proves it came from the operator's key. The private key
    never touches the container.

With neither configured, integrity is observability-only (the digest is still logged at boot).

CLI: `python -m bootstrap.integrity {hash|keygen|sign <privkey_b64>}`.
"""

from __future__ import annotations

import base64
import hashlib
import os
import pathlib
from collections.abc import Mapping

ROOT = pathlib.Path(__file__).resolve().parents[1]


def compute_digest(root: pathlib.Path | None = None) -> str:
    """Deterministic sha256 over the kernel source tree: for every `kernel/**/*.py` (sorted by
    POSIX relative path) hash the path then its bytes. `__pycache__`/`.pyc` are excluded by the
    `*.py` glob, so the digest is stable across runs."""
    root = root or ROOT
    digest = hashlib.sha256()
    for path in sorted((root / "kernel").rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _verify_signature(digest_hex: str, pubkey_b64: str, sig_b64: str) -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64))
        pub.verify(base64.b64decode(sig_b64), digest_hex.encode())
        return True
    except Exception:
        # A bad signature OR malformed key/sig/base64 → failed verification (fail-closed).
        return False


def verify(root: pathlib.Path | None = None,
           env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """Check the live kernel digest against the configured expectation.

    Returns `(ok, message)`. Signed mode takes precedence over a pinned hash; when neither is
    configured the result is `(True, "not enforced")`.
    """
    src: Mapping[str, str] = os.environ if env is None else env
    digest = compute_digest(root)
    pubkey = (src.get("KERNEL_INTEGRITY_PUBKEY") or "").strip()
    sig = (src.get("KERNEL_INTEGRITY_SIG") or "").strip()
    expected = (src.get("KERNEL_EXPECTED_HASH") or "").strip().lower()
    if pubkey and sig:
        ok = _verify_signature(digest, pubkey, sig)
        return ok, ("signature verified" if ok else "signature INVALID for kernel digest")
    if expected:
        ok = digest == expected
        return ok, ("hash matches pin" if ok
                    else f"hash MISMATCH (expected {expected[:16]}…, got {digest[:16]}…)")
    return True, "not enforced (set KERNEL_EXPECTED_HASH or KERNEL_INTEGRITY_PUBKEY/SIG)"


def generate_keypair() -> tuple[str, str]:
    """`(private_b64, public_b64)` ed25519 keypair — for operators enabling signed mode."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    raw_priv = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                  serialization.NoEncryption())
    raw_pub = priv.public_key().public_bytes(serialization.Encoding.Raw,
                                             serialization.PublicFormat.Raw)
    return base64.b64encode(raw_priv).decode(), base64.b64encode(raw_pub).decode()


def sign(digest_hex: str, private_key_b64: str) -> str:
    """Sign a hex digest with an ed25519 private key (base64) → base64 signature."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    return base64.b64encode(priv.sign(digest_hex.encode())).decode()


def _cli(argv: list[str]) -> int:
    cmd = argv[0] if argv else "hash"
    if cmd == "hash":
        print(compute_digest())
        return 0
    if cmd == "keygen":
        priv, pub = generate_keypair()
        print("# Keep the private key OFFLINE; sign with it, never ship it to a container.")
        print("KERNEL_INTEGRITY_PRIVATE=" + priv)
        print("KERNEL_INTEGRITY_PUBKEY=" + pub)
        return 0
    if cmd == "sign" and len(argv) >= 2:
        print(sign(compute_digest(), argv[1]))
        return 0
    print("usage: python -m bootstrap.integrity {hash|keygen|sign <privkey_b64>}")
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_cli(sys.argv[1:]))
