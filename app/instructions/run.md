---
title: Run
category: Working with the agent
order: 20
---
# Run

The Run tab is a **streaming, tool-using chat** with the in-app assistant. Ask questions,
have it use tools, or just talk through what you want to build.

## What it can do

- **Answer with live Markdown** and a per-message token/cost badge.
- **Search & read the web** — `web_search` finds pages, `web_fetch` reads a URL's text.
  Use it for anything current or outside the model's training data.
- **Answer over your files** — `search_knowledge` queries documents you added in the
  **Knowledge** tab.
- **Keep artifacts** — save / list / read small markdown notes that persist on the server.
- **Inspect the harness** — read status, version history, a version's diff, and the audit log.
- **Build software** — `dev_*` tools operate the **Development** sandbox.

## Tips

- It only reaches for a tool when it helps — you don't have to ask explicitly.
- Conversations are saved; start a new one any time.
- To change the *app itself* (not just chat), use **Self-Modify** instead.
