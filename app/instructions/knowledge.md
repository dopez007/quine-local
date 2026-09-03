---
title: Knowledge
category: Working with the agent
order: 30
---
# Knowledge

Bring your own data. Upload documents here and the **Run** agent can answer questions
about them using its `search_knowledge` tool.

## How it works

1. **Upload a file** (text, markdown, CSV, code, or PDF) or **paste text** with a title.
2. The document is **chunked** and stored under the data partition, so it survives reboots
   and version switches.
3. In **Run**, ask a question — the agent retrieves the most relevant chunks and answers
   from them.

## Retrieval

- The default is **keyword / TF-IDF** scoring: no model or API key needed, works offline.
- If an embedding-capable model is configured, retrieval can upgrade to **embeddings**
  automatically (vectors are cached alongside the chunks). Embedding calls are metered and
  show up under **Usage**.

Delete a document any time from the table — its chunks are removed with it.
