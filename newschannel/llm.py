from __future__ import annotations

import json
from typing import Any

import requests

from .config import Config


class OpenAIClient:
    """Minimal OpenAI chat client (plain HTTPS, no extra dependency) that returns structured
    output through a forced function call - the same contract as the Anthropic path."""

    def __init__(self, key: str, base: str = "https://api.openai.com/v1"):
        self.key, self.base = key, base

    @staticmethod
    def convert(content: str | list) -> str | list:
        """Anthropic-style blocks (text / base64 image) -> OpenAI content parts."""
        if isinstance(content, str):
            return content
        parts = []
        for b in content:
            if b["type"] == "text":
                parts.append({"type": "text", "text": b["text"]})
            elif b["type"] == "image":
                src = b["source"]
                parts.append({"type": "image_url", "image_url": {
                    "url": f"data:{src['media_type']};base64,{src['data']}", "detail": "low"}})
        return parts

    def tool_call(self, model: str, system: str, content: str | list, tool_name: str, schema: dict,
                  max_tokens: int) -> dict:
        r = requests.post(f"{self.base}/chat/completions", timeout=180,
                          headers={"Authorization": f"Bearer {self.key}"},
                          json={"model": model, "max_completion_tokens": max_tokens,
                                "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": self.convert(content)}],
                                "tools": [{"type": "function", "function": {
                                    "name": tool_name, "description": "Return the structured result.",
                                    "parameters": schema}}],
                                "tool_choice": {"type": "function", "function": {"name": tool_name}}})
        if r.status_code != 200:
            raise RuntimeError(f"OpenAI error {r.status_code}: {r.text[:300]}")
        calls = r.json()["choices"][0]["message"].get("tool_calls") or []
        if not calls:
            raise RuntimeError("Model returned no structured result")
        return json.loads(calls[0]["function"]["arguments"])


def make_client(cfg: Config, required: bool = True):
    """Client for the provider chosen in config (auto = whichever API key you have)."""
    if cfg.llm_provider() == "openai":
        key = Config.env("OPENAI_API_KEY", required=required)
        return OpenAIClient(key) if key else None
    key = Config.env("ANTHROPIC_API_KEY", required=required)
    if not key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=key)


def call_tool(client: Any, model: str, system: str, content: str | list, tool_name: str,
              schema: dict, max_tokens: int = 4096) -> dict:
    """Force the model to answer through a single tool so the result is structured JSON."""
    if isinstance(client, OpenAIClient):
        return client.tool_call(model, system, content, tool_name, schema, max_tokens)
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
