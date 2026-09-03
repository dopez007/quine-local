# Quine local-core architecture

Quine separates protected mechanism from agent-mutable application code.

## Layers

| Layer | Path | Agent-mutable | Purpose |
|---|---|---:|---|
| Firmware | `bootstrap/` | No | Verify and launch the kernel; restart safely |
| Kernel | `kernel/` | No by default | Versioning, policy, model calls, validation, promotion, rollback, audit |
| Application | `app/` | Yes | User-facing features and HTTP application |
| Self-mod runtime | `app/runtime/` | Yes | Agent loop, prompt, engines, and editing tools |
| Protected runtime state | `state/` | No app handle | Configuration, secrets, audit, version repositories |
| User data | `data/` | App-managed | Conversations, knowledge, plugins, settings, development output |
| Boot slots | `slots/` | Generated | Active/candidate checkouts used for A/B promotion |

The application calls narrow kernel syscalls. Provider keys stay kernel-side and are stripped from application subprocess environments. This environment boundary is not a strong on-disk sandbox when all processes share one operating-system account; containers or equivalent isolation remain the hard deployment boundary.

## Self-modification pipeline

```text
request
  → isolated staging checkout
  → agent edits application code
  → path-policy and syntax/import validation
  → candidate tests and optional acceptance checks
  → commit into protected version history
  → boot candidate in inactive slot
  → health gate
  → promote or rollback
```

A broken candidate never advances the active line. A recovery runtime remains protected so a self-edit cannot permanently strand the editor.

## Version operations

- **Rollback** returns to the previous promoted version.
- **Rollback to** selects a specific earlier version.
- **Revert** removes one version's effect while keeping later work.
- **Re-apply** brings an abandoned version onto the current line.
- **Preview** boots a candidate without promoting it.
- **Named lines** provide isolated experiment/staging branches with their own previews.

## Approval and verification

`agent.require_approval` holds a successful candidate for human review. Approval still reuses health and verification gates.

The optional verification gate derives bounded acceptance checks, executes them against the booted candidate, and freezes successful checks into the regression set. Check failures block promotion.

Autonomous triggers hold for approval unless both explicit auto-promotion and verification are enabled. A scheduled task never promotes unverified code.

## Persistence

The repository is a clean seed. Runtime state is ignored by Git and can live under `QUINE_STATE_HOME`.

- Application versions live in protected Git repositories under `state/`.
- User data is not rolled back with application code.
- `slots/` is disposable cache and should be regenerated during upgrades.

## Threat boundary

Quine reduces risk through allowlisted syscalls, protected paths, separate process environments, non-root containers, versioned candidates, bounded validation, approval, audit, and rollback. It does not claim that running generated code is risk-free. Read [SECURITY.md](../SECURITY.md) before exposing Quine beyond a trusted local environment.
