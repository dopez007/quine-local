"""Example plugin demonstrating the SDK: one HTTP route + one Run-agent tool.

Copy this shape to build real plugins. Routers are namespaced under /api/plugins/<name>/;
tools are merged into the Run agent's toolset and listed at GET /api/plugins.
"""

from __future__ import annotations

from fastapi import APIRouter

PLUGIN = {
    "name": "hello",
    "version": "1.0.0",
    "description": "Sample plugin: a /ping route and an echo tool.",
}

router = APIRouter(prefix="/api/plugins/hello", tags=["plugin:hello"])


@router.get("/ping")
async def ping() -> dict:
    return {"plugin": "hello", "pong": True}


async def _echo(args: dict, ctx) -> str:
    return f"echo: {args.get('text', '')}"


TOOLS = {
    "hello_echo": {
        "schema": {
            "type": "function",
            "function": {
                "name": "hello_echo",
                "description": "Echo back the given text (sample plugin tool).",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string", "description": "text to echo"}},
                    "required": ["text"],
                },
            },
        },
        "handler": _echo,
    }
}
