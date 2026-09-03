---
title: Development
category: Evolving the app
order: 50
---
# Development

A **build sandbox** for arbitrary software — separate from the app's own code. The Run and
Self-Modify agents work here with the `dev_*` tools (read, write, edit, list files, run
shell / compile / test).

## What it's for

- Have the agent build a standalone project (a script, a small service, a prototype).
- It lives on the **data partition**, so it survives reboots, self-mods and rollbacks —
  changing the app never touches what you built here.
- It's **isolated** from the app: nothing here can break the running app or its runtime.

## Background commands

Long builds, downloads, and development servers run as background tasks so the conversation stays
responsive. The agent can wait for a task to finish without repeatedly polling its log, and you can
press **Stop** at any time to interrupt that wait. If a task outlives one wait interval, the agent can
wait again without being mistaken for a tool loop.

## Take it with you

Use **Download zip** to export the entire `development/` tree as a `.zip` — your code
leaves the box whenever you want it.
