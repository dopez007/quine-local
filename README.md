# Quine

Quine is a local, single-user, self-modifying LLM harness. An agent can change the application layer from a plain-language request; every candidate is versioned, validated, health-checked, and either promoted or rolled back automatically.

This repository contains the local self-hosted core only.

> **Status:** experimental source-available software. Review the safety and secret boundaries before using a real provider key. Keep backups of `state/` and `data/`.

## What is included

- Immutable firmware and kernel mechanisms under `bootstrap/` and `kernel/`
- Mutable seed application and self-modifying runtime under `app/`
- Git-backed versions, A/B promotion, rollback, previews, and recovery runtime
- Optional acceptance checks and human approval gates
- Local React interface, knowledge search, development workspace, plugins, usage, audit, and version review
- Offline scripted-engine test coverage

## Quick start

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Git, and Node.js 24+ for frontend-changing self-modifications.

```sh
uv sync --frozen
uv run python -m bootstrap.boot
```

Open <http://127.0.0.1:8000>.

Quine boots with a deterministic scripted engine and does not need an API key for the initial run or tests.

```sh
uv sync --frozen --group test
uv run --group test python scripts/test_low_memory.py quick
uv run --group test python scripts/test_low_memory.py contract
```

Both lanes are serial and enforce process-tree memory, child-process, timeout, and orphan budgets. Heavy integration/system scenarios are explicit and never part of the default Windows run.

For a real provider, copy `state/secrets.env.example` to `state/secrets.env`, add a provider key, and configure a LiteLLM model identifier in `state/config.yaml`. Do not commit `state/`.

## Data, upgrades, and containers

Runtime data is deliberately outside the versioned application:

- `state/` — configuration, version history, audit records, and provider secrets
- `data/` — conversations, knowledge, plugins, settings, and development output
- `slots/` — generated boot cache; do not migrate it

To upgrade, unpack a new release into a new folder and copy only `state/` and `data/` before first boot. You can instead set `QUINE_STATE_HOME` so runtime data lives outside the installation directory.

```sh
docker compose up --build
```

The image runs as a non-root user and persists `state/` and `data/` through mounted volumes. Local operation does not make generated application code a strong security sandbox; use stronger isolation for untrusted code.

## License

Quine is **source-available**, not OSI open source. It is licensed under the [PolyForm Shield License 1.0.0](LICENSE); the license text controls if this summary differs.

You may use, modify, and distribute Quine for permitted noncompeting purposes, including personal, research, and company-internal use. You may not use it to provide a competing or substantially substitutable Quine product or hosted/managed service without separate permission. Renaming the software or changing its interface does not avoid that restriction.

Future releases may use different or additional terms. Versions already received retain the license granted for those versions.

Read the required [NOTICE](NOTICE), [trademark policy](docs/TRADEMARKS.md), and [architecture](docs/ARCHITECTURE.md).

## Suggestions, bugs, and support

Issues and proposals are welcome for:

- reproducible bug reports
- feature and improvement ideas
- design feedback
- documentation corrections

Please include the Quine revision, platform, command, exact error, redacted logs, and minimal reproduction where relevant. Quine is maintained on a best-effort basis: no SLA, consulting promise, immediate response, hosted-operation support, or fixed release cadence is offered.

Code pull requests are not accepted yet. This keeps the project owner-controlled while a future contributor and relicensing policy is designed. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Do not post exploit details, credentials, private conversations, uploaded documents, generated code, or destructive payloads in public issues. After an explicitly approved public release, use GitHub **Security → Report a vulnerability** for private reports. See [SECURITY.md](SECURITY.md) for boundaries and reporting expectations.
