---
title: Errors
category: Observability
order: 75
---

# Errors

The Errors tab is the harness's built-in error tracker. It records problems
automatically and keeps them until you resolve or clear them — so nothing that went
wrong disappears with a page reload or a reboot.

## What gets recorded

- **Backend errors** — any unhandled exception in the app while serving a request.
- **Chat/tool failures** — errors that happen while the Run-tab assistant is working.
- **Boot failures** — a new version that crashed before it could pass its health check,
  including the captured crash log (so you can see exactly why it was rejected).
- **Page errors** — JavaScript errors from this UI.

Repeated occurrences of the same problem are grouped into a single entry with a count,
so one recurring bug never floods the list.

## Reading an entry

Each entry shows where the error came from (its source badge), how often it happened,
which app versions it was seen in, and when it last occurred. **Details** expands the
full technical traceback for each occurrence.

## Fixing and resolving

- **Fix with agent** — the fastest path: it pre-fills a Self-Modify request with the
  error's details. Review the prompt, adjust it if you like, and submit — the agent
  will investigate and ship a fix.
- **Resolve** — mark an entry as dealt with; it disappears from the default view
  (use *Show resolved* to see it again, or *Unresolve* to bring it back).
- **Clear all** — wipe the list entirely.

The agents can also read this tracker themselves: when you ask for a fix, they check
recent errors — including ones from previous versions — to find the cause.
