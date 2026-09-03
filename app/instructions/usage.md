---
title: Usage
category: Operations
order: 80
---
# Usage

See what the agent is spending. This tab rolls up **token usage and estimated cost** across
chat and self-modification.

## What's shown

- **Tokens** — prompt, completion, and cached tokens per model.
- **Estimated cost** — derived from per-model pricing (override it in the backend config's
  `pricing` map; embeddings use `embed_pricing`).
- **Embedding tokens** — metered separately when the Knowledge tab uses embeddings.

Costs are **estimates** for visibility. If you run BYOK (bring-your-own-key), you pay your
provider directly — these numbers help you keep an eye on consumption.
