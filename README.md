# Quine

Quine is a local, single-user, self-modifying LLM harness. An agent can change the application layer from a plain-language request; every candidate is versioned, validated, health-checked, and either promoted or rolled back automatically.

This repository contains the local self-hosted core only.

> **Status:** experimental source-available software. Review the safety and secret boundaries before using a real provider key. Keep backups of `state/` and `data/`.

## See it in action

![Animated synthetic Quine product illustration](assets/quine-teaser-preview.gif)

This 19-second animated preview is a synthetic product illustration of Quine's versioned self-modification flow, not a recording of a live instance or customer data.

## What is included

- Immutable firmware and kernel mechanisms under `bootstrap/` and `kernel/`
- Mutable seed application and self-modifying runtime under `app/`
- Git-backed versions, A/B promotion, rollback, previews, and recovery runtime
- Optional acceptance checks and human approval gates
- Local React interface, knowledge search, development workspace, plugins, usage, audit, and version review
- Offline scripted-engine test coverage

## Download and quick start

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), and Git. Node.js 24+ is needed only when a self-modification changes the frontend.

Clone the source:

```sh
git clone https://github.com/dopez007/quine-local.git
cd quine-local
uv sync --frozen
uv run python -m bootstrap.boot
```

GitHub also offers **Code → Download ZIP**. Extract it, open a terminal in the extracted `quine-local` folder, then run the last two commands above.

Open <http://127.0.0.1:8000>. A successful boot loads the Quine UI; <http://127.0.0.1:8000/health> returns HTTP 200.

The server can start without an API key. The default agent configuration uses LiteLLM with `deepseek/deepseek-v4-flash`, so real model-backed chat and self-modification need a configured provider key. The serial test lanes use the deterministic scripted engine and need no provider key:

```sh
uv sync --frozen --group test
uv run --group test python scripts/test_low_memory.py quick
uv run --group test python scripts/test_low_memory.py contract
```

Both lanes are serial and enforce process-tree memory, child-process, timeout, and orphan budgets. Heavy integration/system scenarios are explicit and never part of the default Windows run.

For the default provider, copy the example and add `DEEPSEEK_API_KEY`:

```sh
# macOS/Linux
cp state/secrets.env.example state/secrets.env

# Windows PowerShell
Copy-Item state/secrets.env.example state/secrets.env
```

Edit `state/secrets.env`, then change `agent.model` in the generated `state/config.yaml` when using another LiteLLM provider or model. Do not commit `state/`.

## Data, upgrades, and Docker

Runtime data is deliberately outside the versioned application:

- `state/` — configuration, version history, audit records, and provider secrets
- `data/` — conversations, knowledge, plugins, settings, and development output
- `slots/` — generated boot cache; do not migrate it

For an upgrade, use a freshly cloned or downloaded source snapshot in a new folder and copy only `state/` and `data/` before first boot. You can instead set `QUINE_STATE_HOME` so runtime data lives outside the installation directory.

Docker requires Docker Desktop or Docker Engine. For provider-backed use, create and configure `state/secrets.env` as above, then run:

```sh
docker compose up --build
```

Open <http://127.0.0.1:8000>. The compose sample publishes port 8000 and is for trusted local use only; do not expose it publicly without the authentication, network controls, TLS, and reviewed deployment model described in [SECURITY.md](SECURITY.md). The image runs as a non-root user and persists `state/` and `data/` through mounted volumes. Local operation does not make generated application code a strong security sandbox; use stronger isolation for untrusted code.

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

Do not post exploit details, credentials, private conversations, uploaded documents, generated code, or destructive payloads in public issues. For private reports on a public release, follow [SECURITY.md](SECURITY.md).
