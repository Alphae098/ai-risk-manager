"""LLM transport.

Thin wrapper over the OpenRouter chat-completions endpoint, which speaks the
OpenAI-compatible schema including tool calls. The interface is deliberately
narrow so the provider can be swapped without touching the analyst logic.

When no API key is configured, or ARM_LLM_MOCK=1, a deterministic mock takes
over. The mock is not a stub that returns a fixed string: it runs the same tool
loop against the same evidence and applies an explicit heuristic, so the whole
demo and the full test suite work offline with no network calls.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import LLM, PRICE_PER_1M_INPUT_USD, PRICE_PER_1M_OUTPUT_USD


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    source: str = "openrouter"

    @property
    def cost_usd(self) -> float:
        return (self.prompt_tokens / 1e6) * PRICE_PER_1M_INPUT_USD + \
               (self.completion_tokens / 1e6) * PRICE_PER_1M_OUTPUT_USD


class LLMError(RuntimeError):
    pass


class OpenRouterClient:
    def __init__(self, config=LLM):
        self.config = config

    @property
    def is_mock(self) -> bool:
        return self.config.use_mock

    def chat(self, messages: list[dict[str, Any]],
             tools: list[dict[str, Any]] | None = None) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": "Bearer " + self.config.api_key,
            "Content-Type": "application/json",
            # OpenRouter uses these for attribution on its dashboard.
            "HTTP-Referer": "https://github.com/ai-risk-manager",
            "X-Title": "AI Risk Manager",
        }
        try:
            with httpx.Client(timeout=self.config.timeout_s) as client:
                response = client.post(self.config.base_url + "/chat/completions",
                                       headers=headers, json=payload)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            raise LLMError("OpenRouter request failed: " + str(exc)) from exc

        choice = body["choices"][0]["message"]
        usage = body.get("usage", {})
        return LLMResponse(
            content=choice.get("content"),
            tool_calls=choice.get("tool_calls") or [],
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            source=self.config.model,
        )


def parse_json_object(text: str | None) -> dict[str, Any] | None:
    """Extract a JSON object from a model reply that may wrap it in prose."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
