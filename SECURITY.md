# Security policy

Quine can generate, validate, and execute application code. Treat that capability as security-sensitive.

## Supported release

Security fixes target the latest public local-core release. Older snapshots may not receive fixes.

## Report a vulnerability

Do not open a public issue containing exploit details, credentials, private data, or a working destructive payload.

When this repository is public, use GitHub **Security → Report a vulnerability** to create a private vulnerability report. That control must be enabled and verified as part of the approved publication process.

Include:

- affected revision/version
- operating system and deployment form
- minimal reproduction
- impact and required preconditions
- whether secrets, user data, or host access are involved

No response-time or bounty guarantee is offered.

## Security boundary

- The mutable app does not receive provider keys through its environment.
- Protected path policy, candidate validation, health checks, approval, audit, and rollback reduce risk; they do not make generated code inherently safe.
- Processes using the same operating-system account may access files that Unix/Windows permissions allow. Environment stripping is not a disk sandbox.
- Use a dedicated non-root container or stronger isolation for untrusted generated code.
- Do not expose a local instance publicly without authentication, network controls, TLS termination, and a reviewed deployment model.
- Keep `state/` and `data/` private. They can contain credentials, conversations, uploaded documents, generated software, and detailed history.
- Never commit `state/secrets.env`, runtime databases, logs containing provider content, or exported user data.

## Out of scope

Hosted multi-user operation is outside this release boundary.
