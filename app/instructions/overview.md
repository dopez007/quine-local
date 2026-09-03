---
title: Overview
category: Getting started
order: 10
---
# Welcome to Quine

Quine is a **self-modifying app**. You describe what you want in plain language and the
agent rewrites this app's own code to build it — then the app reboots into the new version.
Every change is versioned and watched, so a broken change rolls back automatically.

## The loop in one line

**Describe it → the agent edits a staging copy → it's validated (syntax + import + UI build)
→ you preview the diff (optional) → reboot into the new version, or auto-rollback if it
breaks.**

## Where to go

- **Run** — chat with the in-app agent. It can search the web, read your uploaded docs,
  keep artifacts, and inspect the harness.
- **Self-Modify** — the headline feature: ask the agent to change this app. One-click
  templates get you started.
- **Knowledge** — upload your documents so the Run agent can answer over them.
- **Development** — a sandbox where the agent builds standalone software you can download.
- **Versions / Usage / Audit / Settings** — review changes, watch token spend, see the
  full event log, and tune the agent.
- **Instructions** — this manual. It stays accurate because the agent updates it whenever
  it ships a new feature.

> New here? Open **Self-Modify**, click a template, and watch the app rewrite itself.
