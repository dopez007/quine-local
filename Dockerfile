# Quine local self-host image.
#
# The image is self-contained: the React UI ships prebuilt (app/frontend/dist is committed), and
# Node is included so self-modification validation can rebuild the frontend when a change touches
# frontend/. The pipeline fails closed if npm is missing, so Node is required for that capability.
FROM python:3.12-slim

# git: used by the versioning layer (A/B slots live in a git repo).
# nodejs/npm: used by the frontend build during self-mod validation.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# uv for Python dependency management (never pip).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install deps first (cached layer) from the lockfile, then copy the source.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY . .

# Run as a NON-ROOT user (P0.4): the self-modifying agent has a full shell in this container,
# so we shrink the blast radius of any escape. Pre-create the mount points owned by that user
# so freshly-created named volumes inherit writable ownership (Docker seeds a new volume's
# ownership from the image path). slots/ is a runtime-written dir (not a volume) — also owned.
RUN useradd --create-home --uid 10001 quine \
    && mkdir -p /app/state /app/data /app/slots \
    && chown -R quine:quine /app /home/quine

# state/ (secrets, version history, audit) and data/ (user data) are mounted at runtime
# so they persist across container restarts and never bake into the image.
VOLUME ["/app/state", "/app/data"]

USER quine
EXPOSE 8000
# Bind the gateway on all interfaces inside the container (default is loopback-only).
ENV KERNEL_BIND_HOST=0.0.0.0
# Set KERNEL_AUTH_TOKEN at runtime to require Bearer auth at the edge. Set
# KERNEL_REQUIRE_AUTH=1 to refuse startup when that token is missing.
#
# Hardened mode (opt-in, recommended for real deployments): set QUINE_KERNEL_HARDENED=1 and
# QUINE_SECRET_KEY at runtime to encrypt state/secrets.env at rest and enable the fail-closed
# secret-isolation self-check (kernel/state_store.py:enforce_secret_hardening). It complements the
# non-root user above. Left unset here so a container without a configured master key still boots
# with plaintext legacy behavior.

# Run the venv Python DIRECTLY (not via `uv run`) so bootstrap.boot is PID 1. In hardened mode the
# firmware marks itself non-dumpable to keep provider keys / QUINE_SECRET_KEY (which arrive in the
# container env) out of a same-UID agent's reach via /proc/1/environ — but only PID 1 can protect
# PID 1. `uv run` would keep uv as a dumpable PID 1 holding those secrets. uv stays on PATH for the
# agent's `uv pip install`; deps are already synced into /app/.venv at build time.
CMD ["/app/.venv/bin/python", "-m", "bootstrap.boot"]
