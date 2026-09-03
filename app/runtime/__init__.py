"""The self-modification agent's runtime — the agent's own brain.

This entire package is EDITABLE by the agent (it ships in the versioned system image).
Change how the agent operates here: add tools (tools.py), change the loop (agent.py),
adjust the prompt (prompt.py), add engines (engines.py). The kernel runs this as a
keyless worker and keeps a minimal recovery fallback, so a bad edit is recoverable.
"""
