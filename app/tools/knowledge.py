"""Knowledge / document Q&A — bring-your-own-data for the Run agent.

Uploaded documents are chunked and stored under the data partition (one JSONL per doc),
so they survive reboots and version swaps. The `search_knowledge` tool retrieves the most
relevant chunks for a query.

Retrieval is **keyword/TF-IDF by default** — pure Python, no extra deps, works offline,
on the scripted engine, and with any provider. When the app is configured for embeddings
(`knowledge.use_embeddings` + an embed-capable `knowledge.embed_model`) it upgrades to
semantic search via the kernel's `/llm_call` `kind:"embed"` syscall, caching vectors in the
JSONL; any failure falls back to keyword search.

This module also holds the store helpers (`ingest`/`list_docs`/`delete_doc`) that main.py's
upload endpoints call, so all knowledge logic lives in one place.
"""

from __future__ import annotations

import json
import math
import pathlib
import re
import time

_CHUNK_SIZE = 1500       # chars per chunk
_CHUNK_OVERLAP = 150     # chars of overlap between adjacent chunks
_DEFAULT_K = 5

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be", "been",
    "to", "of", "in", "on", "for", "with", "as", "by", "at", "from", "that", "this",
    "it", "its", "i", "you", "he", "she", "they", "we", "do", "does", "what", "how",
    "why", "when", "where", "which", "who", "can", "will", "would", "about",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (text or "").strip().lower()).strip("-")
    return s or "untitled"


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower())
            if len(t) > 1 and t not in _STOPWORDS]


# ── chunking + store ────────────────────────────────────────────────────────────────
def chunk_text(text: str, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = start + size
        chunk = text[start:end]
        # Prefer to break on a paragraph/sentence boundary near the end of the window.
        if end < len(text):
            for sep in ("\n\n", "\n", ". "):
                cut = chunk.rfind(sep)
                if cut > size // 2:
                    chunk = chunk[: cut + len(sep)]
                    break
        chunk = chunk.strip()
        if chunk:
            chunks.append(chunk)
        start += max(len(chunk) - overlap, 1)
    return chunks


def _doc_path(base_dir: pathlib.Path, title: str) -> pathlib.Path:
    return base_dir / (_slug(title) + ".jsonl")


def ingest(base_dir: pathlib.Path, title: str, text: str) -> dict:
    """Chunk `text` and persist it as one JSONL doc. Overwrites a same-named doc."""
    base_dir.mkdir(parents=True, exist_ok=True)
    chunks = chunk_text(text)
    path = _doc_path(base_dir, title)
    with path.open("w", encoding="utf-8") as f:
        for i, ch in enumerate(chunks):
            f.write(json.dumps({"i": i, "text": ch}, ensure_ascii=False) + "\n")
    return {"title": _slug(title), "chunks": len(chunks), "chars": len(text or "")}


def list_docs(base_dir: pathlib.Path) -> list[dict]:
    if not base_dir.is_dir():
        return []
    out = []
    for p in sorted(base_dir.glob("*.jsonl")):
        chunks = chars = 0
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    chunks += 1
                    try:
                        chars += len(json.loads(line).get("text", ""))
                    except json.JSONDecodeError:
                        pass
        except Exception:
            continue
        out.append({"title": p.stem, "chunks": chunks, "chars": chars})
    return out


def delete_doc(base_dir: pathlib.Path, title: str) -> bool:
    path = _doc_path(base_dir, title)
    if path.exists():
        path.unlink()
        return True
    return False


def _load_chunks(base_dir: pathlib.Path) -> list[dict]:
    """All chunks across docs: [{doc, i, text, emb?}]."""
    chunks: list[dict] = []
    if not base_dir.is_dir():
        return chunks
    for p in sorted(base_dir.glob("*.jsonl")):
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                chunks.append({"doc": p.stem, "i": rec.get("i", 0),
                               "text": rec.get("text", ""), "emb": rec.get("emb")})
        except Exception:
            continue
    return chunks


# ── keyword (TF-IDF) retrieval — the always-available default ────────────────────────
def _keyword_rank(query: str, chunks: list[dict], k: int) -> list[tuple[float, dict]]:
    q_terms = _tokens(query)
    if not q_terms or not chunks:
        return []
    n = len(chunks)
    tokenized = [_tokens(c["text"]) for c in chunks]
    df: dict[str, int] = {}
    for toks in tokenized:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    scored: list[tuple[float, dict]] = []
    for toks, chunk in zip(tokenized, chunks):
        if not toks:
            continue
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for qt in q_terms:
            if qt in tf:
                idf = math.log(1 + n / (df.get(qt, 0) or 1))
                score += (tf[qt] / len(toks)) * idf
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:k]


# ── embeddings retrieval — optional upgrade via the kernel /llm_call kind:embed ───────
def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def _embed(ctx, model: str, inputs: list[str]) -> list[list[float]] | None:
    """Embed `inputs` via the kernel. Returns None on any failure (caller falls back)."""
    if getattr(ctx, "syscall_post", None) is None:
        return None
    try:
        resp = await ctx.syscall_post("/llm_call", {"model": model, "kind": "embed",
                                                    "input": inputs})
        if not resp.get("ok"):
            return None
        data = (resp.get("response") or {}).get("data") or []
        vecs = [row["embedding"] for row in data if isinstance(row, dict) and "embedding" in row]
        if len(vecs) != len(inputs):
            return None
        _record_embed_usage(ctx, model, (resp.get("response") or {}).get("usage") or {})
        return vecs
    except Exception:
        return None


def _record_embed_usage(ctx, model: str, usage: dict) -> None:
    """Append embedding token usage to DATA_DIR/embedding_usage.json for the usage rollup.

    Best-effort; never raises.
    """
    data_dir = getattr(ctx, "data_dir", None)
    if not data_dir or not usage:
        return
    try:
        tokens = int(usage.get("total_tokens") or usage.get("prompt_tokens") or 0)
        if tokens <= 0:
            return
        path = data_dir / "embedding_usage.json"
        recs = []
        if path.exists():
            try:
                recs = json.loads(path.read_text(encoding="utf-8")) or []
            except Exception:
                recs = []
        recs.append({"timestamp": time.time(), "model": model, "tokens": tokens})
        path.write_text(json.dumps(recs, indent=2), encoding="utf-8")
    except Exception:
        pass


async def _embed_rank(query: str, chunks: list[dict], k: int, ctx, base_dir, model):
    """Semantic rank; caches chunk vectors back into the JSONL. None ⇒ caller falls back."""
    missing = [c for c in chunks if not c.get("emb")]
    if missing:
        vecs = await _embed(ctx, model, [c["text"] for c in missing])
        if vecs is None:
            return None
        for c, v in zip(missing, vecs):
            c["emb"] = v
        _persist_embeddings(base_dir, chunks)
    q = await _embed(ctx, model, [query])
    if not q:
        return None
    qv = q[0]
    scored = [(_cosine(qv, c["emb"]), c) for c in chunks if c.get("emb")]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:k]


def _persist_embeddings(base_dir: pathlib.Path, chunks: list[dict]) -> None:
    """Rewrite each doc's JSONL with embeddings cached inline (best-effort)."""
    by_doc: dict[str, list[dict]] = {}
    for c in chunks:
        by_doc.setdefault(c["doc"], []).append(c)
    for doc, recs in by_doc.items():
        recs.sort(key=lambda r: r.get("i", 0))
        try:
            with (base_dir / f"{doc}.jsonl").open("w", encoding="utf-8") as f:
                for r in recs:
                    out = {"i": r.get("i", 0), "text": r.get("text", "")}
                    if r.get("emb"):
                        out["emb"] = r["emb"]
                    f.write(json.dumps(out, ensure_ascii=False) + "\n")
        except Exception:
            pass


def _format(results: list[tuple[float, dict]]) -> str:
    if not results:
        return "(no relevant passages found in the knowledge base)"
    out = []
    for score, c in results:
        out.append(f"[{c['doc']} #{c['i']}] (score {score:.3f})\n{c['text'].strip()}")
    return "\n\n".join(out)


# ── the tool ──────────────────────────────────────────────────────────────────────
async def search_knowledge(args: dict, ctx) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "error: a query is required"
    k = max(1, min(10, int(args.get("k", _DEFAULT_K) or _DEFAULT_K)))
    base_dir = (ctx.data_dir / "knowledge") if getattr(ctx, "data_dir", None) else None
    if base_dir is None:
        return "error: knowledge store unavailable"
    chunks = _load_chunks(base_dir)
    if not chunks:
        return "the knowledge base is empty — upload documents in the Knowledge tab first"

    # Optional semantic upgrade when configured; otherwise keyword.
    cfg = {}
    if getattr(ctx, "config_get", None) is not None:
        try:
            cfg = (ctx.config_get() or {}).get("knowledge", {}) or {}
        except Exception:
            cfg = {}
    if cfg.get("use_embeddings") and cfg.get("embed_model"):
        ranked = await _embed_rank(query, chunks, k, ctx, base_dir, cfg["embed_model"])
        if ranked is not None:
            return _format(ranked)
    return _format(_keyword_rank(query, chunks, k))


TOOLS = {
    "search_knowledge": {
        "schema": {
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": "Search the user's uploaded documents (knowledge base) and "
                               "return the most relevant passages. Use this to answer "
                               "questions about files the user has provided.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "what to look for"},
                        "k": {"type": "integer", "description": "passages to return (1–10)"},
                    },
                    "required": ["query"],
                },
            },
        },
        "handler": search_knowledge,
    },
}
