"""Instructions store + tools — the living manual for this app.

Every tab/feature has a markdown doc explaining what it does and how it works. Two
layers, merged at read time (runtime wins):

  • SHIPPED seeds  — markdown files in ``app/instructions/*.md`` (this source tree).
    They version-control and travel with the code, so a fresh clone is documented on
    first boot. The SELF-MODIFY agent edits these as ordinary source files: whenever it
    adds or changes a tab/feature it writes the matching ``app/instructions/<slug>.md``.
  • RUNTIME overrides — markdown files under ``DATA_DIR/instructions/*.md``. The RUN
    agent (``write_instruction`` tool) and the Instructions tab's editor write here, so
    user edits persist across reboots/version switches without touching source.

Each doc is a markdown file with a tiny YAML-ish frontmatter block:

    ---
    title: Run
    category: Working with the agent
    order: 20
    ---
    # Run
    …markdown body…

Deleting a runtime override reverts to the shipped seed (if any); deleting a doc with no
seed removes it entirely.
"""

from __future__ import annotations

import json
import pathlib
import re

# Shipped seed docs live next to the app package (app/instructions/). Resolved from this
# file's location so it points at the RUNNING version's seeds (each slot has its own).
SEED_DIR = pathlib.Path(__file__).resolve().parent.parent / "instructions"


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (text or "").strip().lower()).strip("-")
    return s or "untitled"


def _overrides_dir(data_dir) -> pathlib.Path | None:
    if not data_dir:
        return None
    return pathlib.Path(data_dir) / "instructions"


# ── frontmatter (de)serialization ───────────────────────────────────────────────────
def _parse(text: str) -> tuple[dict, str]:
    """Split a doc into (metadata, body). Tolerates a missing/malformed frontmatter."""
    meta: dict = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            head = text[3:end].strip("\n")
            body = text[end + 4:].lstrip("\n")
            for line in head.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
    return meta, body


def _render(title: str, category: str, order, body: str) -> str:
    body = (body or "").rstrip() + "\n"
    return (f"---\ntitle: {title}\ncategory: {category}\norder: {int(order)}\n---\n{body}")


def _entry_from_text(slug: str, text: str, source: str) -> dict:
    meta, body = _parse(text)
    try:
        order = int(meta.get("order", 100))
    except (TypeError, ValueError):
        order = 100
    # A readable title even if frontmatter is absent: first heading, else the slug.
    title = meta.get("title") or _first_heading(body) or slug.replace("-", " ").title()
    return {
        "slug": slug,
        "title": title,
        "category": meta.get("category") or "General",
        "order": order,
        "content": body,
        "source": source,
        "chars": len(body),
    }


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()
    return ""


def _read(path: pathlib.Path, slug: str, source: str) -> dict | None:
    try:
        return _entry_from_text(slug, path.read_text(encoding="utf-8"), source)
    except Exception:
        return None


# ── merge seeds + overrides ──────────────────────────────────────────────────────────
def _collect(data_dir) -> dict[str, dict]:
    """slug -> entry, seeds overlaid by runtime overrides (override content wins; an
    overridden seed is tagged ``edited`` so the UI can show a Reset action)."""
    out: dict[str, dict] = {}
    if SEED_DIR.is_dir():
        for p in sorted(SEED_DIR.glob("*.md")):
            e = _read(p, p.stem, "shipped")
            if e:
                out[p.stem] = e
    odir = _overrides_dir(data_dir)
    if odir and odir.is_dir():
        for p in sorted(odir.glob("*.md")):
            e = _read(p, p.stem, "custom")
            if e:
                if p.stem in out:
                    e["source"] = "edited"  # a shipped doc the user has customized
                out[p.stem] = e
    return out


def _summary(body: str) -> str:
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return s[:120]
    return ""


def list_all(data_dir) -> list[dict]:
    """Doc metadata (no full body), sorted by (order, title) for stable navigation."""
    items = [
        {k: e[k] for k in ("slug", "title", "category", "order", "source", "chars")}
        | {"summary": _summary(e["content"])}
        for e in _collect(data_dir).values()
    ]
    items.sort(key=lambda e: (e["order"], e["title"].lower()))
    return items


def get_one(data_dir, slug: str) -> dict | None:
    return _collect(data_dir).get(_slug(slug))


def upsert(data_dir, slug: str, content: str, title: str = "",
           category: str = "", order=None) -> dict:
    """Write/overwrite a runtime doc under DATA_DIR/instructions/. Missing title/category/
    order inherit from any existing doc (seed or override) so partial edits don't wipe them."""
    odir = _overrides_dir(data_dir)
    if odir is None:
        raise ValueError("no data dir configured")
    slug = _slug(slug)
    existing = get_one(data_dir, slug) or {}
    title = (title or existing.get("title") or slug.replace("-", " ").title()).strip()
    category = (category or existing.get("category") or "General").strip()
    if order is None:
        order = existing.get("order", 100)
    odir.mkdir(parents=True, exist_ok=True)
    (odir / f"{slug}.md").write_text(_render(title, category, order, content), encoding="utf-8")
    e = get_one(data_dir, slug) or {}
    return {k: e.get(k) for k in ("slug", "title", "category", "order", "source", "chars")}


def remove(data_dir, slug: str) -> dict:
    """Delete the runtime override. If a shipped seed exists the doc reverts to it;
    otherwise it is gone. Returns {ok, reverted}."""
    odir = _overrides_dir(data_dir)
    slug = _slug(slug)
    path = odir / f"{slug}.md" if odir else None
    if not path or not path.exists():
        return {"ok": False, "reverted": False}
    path.unlink()
    return {"ok": True, "reverted": (SEED_DIR / f"{slug}.md").exists()}


# ── Run-agent tools ──────────────────────────────────────────────────────────────────
def _schema(name: str, description: str, properties: dict, required=None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    }


async def list_instructions(args: dict, ctx) -> str:
    items = list_all(getattr(ctx, "data_dir", None))
    return json.dumps(items) if items else "no instruction docs yet"


async def read_instruction(args: dict, ctx) -> str:
    e = get_one(getattr(ctx, "data_dir", None), args.get("slug", ""))
    if not e:
        return "instruction doc not found"
    return f"# {e['title']}  (category: {e['category']}, source: {e['source']})\n\n{e['content']}"


async def write_instruction(args: dict, ctx) -> str:
    slug = (args.get("slug") or args.get("title") or "").strip()
    content = args.get("content") or ""
    if not slug:
        return "error: a slug or title is required"
    if not content.strip():
        return "error: content is required"
    try:
        info = upsert(getattr(ctx, "data_dir", None), slug, content,
                      title=args.get("title", ""), category=args.get("category", ""),
                      order=args.get("order"))
    except ValueError as exc:
        return f"error: {exc}"
    return f"saved instruction '{info['title']}' ({info['slug']}, {info['chars']} chars)"


async def delete_instruction(args: dict, ctx) -> str:
    r = remove(getattr(ctx, "data_dir", None), args.get("slug", ""))
    if not r["ok"]:
        return "no custom instruction to delete (nothing changed)"
    return "reverted to the shipped default" if r["reverted"] else "deleted the instruction doc"


TOOLS = {
    "list_instructions": {
        "schema": _schema(
            "list_instructions",
            "List the in-app instruction docs (the Instructions tab): slug, title, category.",
            {},
        ),
        "handler": list_instructions,
    },
    "read_instruction": {
        "schema": _schema(
            "read_instruction",
            "Read one instruction doc's markdown by slug (e.g. 'run', 'knowledge').",
            {"slug": {"type": "string"}},
            ["slug"],
        ),
        "handler": read_instruction,
    },
    "write_instruction": {
        "schema": _schema(
            "write_instruction",
            "Create or update an instruction doc shown in the Instructions tab. Use this to "
            "document a new tab/feature or correct an existing doc. Body is markdown.",
            {
                "slug": {"type": "string", "description": "stable id, e.g. 'kanban'"},
                "title": {"type": "string"},
                "category": {"type": "string", "description": "grouping header in the tab"},
                "content": {"type": "string", "description": "markdown body"},
                "order": {"type": "integer", "description": "sort position (lower = higher)"},
            },
            ["slug", "content"],
        ),
        "handler": write_instruction,
    },
    "delete_instruction": {
        "schema": _schema(
            "delete_instruction",
            "Delete a custom instruction doc. If it overrode a shipped default, it reverts "
            "to that default instead of disappearing.",
            {"slug": {"type": "string"}},
            ["slug"],
        ),
        "handler": delete_instruction,
    },
}
