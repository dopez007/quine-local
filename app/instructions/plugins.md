---
title: Plugins
category: Evolving the app
order: 70
---
# Plugins

Install, enable/disable, and remove **plugins** that extend the app without a full
self-modification.

## How it works

- A plugin registers extra capability (e.g. new agent tools or routes) through the app's
  plugin SDK.
- **Enable / disable** toggles a plugin live; disabled plugins are gated out of requests.
- **Uninstall** removes it entirely.

Plugins are a lighter-weight path than Self-Modify when you just want to add a packaged
capability rather than have the agent rewrite the app.
