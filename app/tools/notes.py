"""Note tools — persist small markdown notes under the data partition (survives reboots).

Each tool is `async def handler(args: dict, ctx) -> str`; `ctx.notes_dir` is the storage
directory. Register tools in TOOLS at the bottom.
"""

from __future__ import annotations

import json
import re


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (text or "").strip().lower()).strip("-")
    return s or "untitled"


def _schema(name: str, description: str, properties: dict, required=None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    }


async def save_note(args: dict, ctx) -> str:
    title = (args.get("title") or "").strip()
    body = args.get("body") or ""
    if not title:
        return "error: a title is required"
    ctx.notes_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.notes_dir / (_slug(title) + ".md")
    existed = path.exists()
    path.write_text(body, encoding="utf-8")
    return f"{'updated' if existed else 'saved'} note '{title}' ({len(body)} chars)"


async def list_notes(args: dict, ctx) -> str:
    ctx.notes_dir.mkdir(parents=True, exist_ok=True)
    items = []
    for p in sorted(ctx.notes_dir.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        snippet = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        items.append({"title": p.stem, "chars": len(text), "snippet": snippet[:80]})
    return json.dumps(items) if items else "no notes saved yet"


async def read_note(args: dict, ctx) -> str:
    p = ctx.notes_dir / (_slug(args.get("title", "")) + ".md")
    if not p.exists():
        return "note not found"
    return p.read_text(encoding="utf-8")[:8000]


TOOLS = {
    "save_note": {
        "schema": _schema(
            "save_note",
            "Save (or overwrite) a markdown note for the user.",
            {"title": {"type": "string"}, "body": {"type": "string"}},
            ["title", "body"],
        ),
        "handler": save_note,
    },
    "list_notes": {
        "schema": _schema(
            "list_notes",
            "List saved notes with size and a short snippet of each.",
            {},
        ),
        "handler": list_notes,
    },
    "read_note": {
        "schema": _schema(
            "read_note",
            "Read a saved note by title.",
            {"title": {"type": "string"}},
            ["title"],
        ),
        "handler": read_note,
    },
}
