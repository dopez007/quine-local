---
title: Settings
category: Operations
order: 100
---
# Settings

Tune how the agent operates. These map to the kernel's bounded, allow-listed config, so you
can't set anything that would break the recovery core.

## Common knobs

- **Model / temperature / max steps** — which model the agent uses and how far it runs.
- **Preview before go-live** — require approval for self-mods (review them in **Versions**).
- **Backend config** — web-search provider key, pricing overrides, and the knowledge/embed
  settings live in the app's backend config (not the kernel).

Provider API keys are held by the kernel, never by this app — so changing settings here never
exposes a key.
