---
title: Versions
category: Evolving the app
order: 60
---
# Versions

Every self-modification is a **version**. This tab is the history and the review desk.

## What you can do

- **Browse versions** — each change the agent shipped, newest first.
- **Diff** — open a full, untruncated per-file diff in a modal to see exactly what changed.
- **Roll back** — return to any earlier version if you don't like a change.
- **Review pending changes** — when *Preview before go-live* is on (see **Self-Modify**),
  a change waits here for you to **Approve** or **Reject** before it boots.

## Why it's safe

Versions are immutable and the kernel keeps a recovery fallback. If a new version fails to
boot, it auto-rolls-back to the last good one — so you can experiment freely.
