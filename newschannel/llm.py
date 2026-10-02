from __future__ import annotations

from typing import Any


def make_client(api_key: str):
    import anthropic
    return anthropic.Anthropic(api_key=api_key)


def call_tool(client: Any, model: str, system: str, content: str | list, tool_name: str,
              schema: dict, max_tokens: int = 4096) -> dict:
    """Force Claude to answer through a single tool so the result is structured JSON."""
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        tools=[{"name": tool_name, "description": "Return the structured result.",
                "input_schema": schema}],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": content}],
    )
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use":
            return dict(block.input)
    raise RuntimeError("Model returned no structured result")
