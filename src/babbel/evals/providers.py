"""Model clients for the model bake-off (spec "Evals", D4, NFR-01, NFR-09).

Two kinds of endpoint cover every candidate:
  openai     any OpenAI-compatible /chat/completions endpoint: OpenAI, Azure OpenAI (v1 API),
             Mistral, Gemini (OpenAI-compatible endpoint), a self-hosted vLLM/Ollama/LM Studio
  anthropic  Claude through the Anthropic SDK, on the Claude API or on Amazon Bedrock
             (set bedrock_region, e.g. eu-central-1, for EU processing)

Every call streams, so the time to the first token is measured on each request.
Messages use the OpenAI shape ({"role": "system"|"user"|"assistant", "content": str}).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class Reply:
    text: str = ""
    tool_calls: list[tuple[str, dict]] = field(default_factory=list)
    ttft: float | None = None  # seconds to the first streamed token (text or tool call)
    total: float = 0.0  # seconds for the whole response
    input_tokens: int = 0
    output_tokens: int = 0
    refused: bool = False


@dataclass
class Candidate:
    """One entry of evals/models.yaml."""

    id: str
    provider: str  # openai | anthropic
    model: str
    base_url: str = ""
    api_key_env: str = ""
    auth_header: str = "bearer"  # bearer | api-key (Azure key auth)
    bedrock_region: str = ""
    temperature: float | None = None  # openai only; Claude 5.x models take no sampling parameters
    extra: dict = field(default_factory=dict)  # merged into every request body
    price_in_per_1m: float | None = None  # EUR, for the cost estimate
    price_out_per_1m: float | None = None
    eu: str = ""  # where it is processed, for the report

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""


def openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": t} for t in tools]


def anthropic_tools(tools: list[dict]) -> list[dict]:
    return [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
            for t in tools]


CALL_START = "[call connected]"


def anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Split the system prompt off and make the history start with a user turn."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    rest = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
    if not rest or rest[0]["role"] != "user":
        rest.insert(0, {"role": "user", "content": CALL_START})
    return system, rest


class OpenAICompatible:
    def __init__(self, c: Candidate, timeout: float = 60.0) -> None:
        if not c.base_url:
            raise ValueError(f"{c.id}: base_url is required for provider openai")
        self.c = c
        self.timeout = timeout

    async def complete(self, messages: list[dict], *, temperature: float = 0.3,
                       max_tokens: int = 800) -> str:
        """Same shape as core.llm.ChatClient.complete, so core helpers accept this client.
        temperature is ignored: the candidate's own config decides sampling."""
        return (await self.chat(messages, max_tokens=max_tokens)).text

    def _headers(self) -> dict:
        if not self.c.api_key:  # local servers (LM Studio, Ollama) need no key
            return {}
        if self.c.auth_header == "api-key":
            return {"api-key": self.c.api_key}
        return {"Authorization": f"Bearer {self.c.api_key}"}

    async def chat(self, messages: list[dict], *, tools: list[dict] | None = None,
                   max_tokens: int = 800) -> Reply:
        body: dict[str, Any] = {"model": self.c.model, "messages": messages, "stream": True,
                                "max_tokens": max_tokens,
                                "stream_options": {"include_usage": True}}
        if self.c.temperature is not None:
            body["temperature"] = self.c.temperature
        if tools:
            body["tools"] = openai_tools(tools)
        body.update(self.c.extra)
        reply, calls = Reply(), {}
        start = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", f"{self.c.base_url.rstrip('/')}/chat/completions",
                                     headers=self._headers(), json=body) as resp:
                if resp.status_code >= 400:
                    raise RuntimeError(f"{self.c.id}: HTTP {resp.status_code} "
                                       f"{(await resp.aread()).decode()[:300]}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if chunk.get("usage"):
                        reply.input_tokens = chunk["usage"].get("prompt_tokens", 0) or 0
                        reply.output_tokens = chunk["usage"].get("completion_tokens", 0) or 0
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta", {})
                        if (delta.get("content") or delta.get("tool_calls")) and reply.ttft is None:
                            reply.ttft = time.perf_counter() - start
                        reply.text += delta.get("content") or ""
                        for tc in delta.get("tool_calls") or []:
                            slot = calls.setdefault(tc.get("index", 0), {"name": "", "args": ""})
                            fn = tc.get("function", {})
                            slot["name"] += fn.get("name") or ""
                            slot["args"] += fn.get("arguments") or ""
        reply.total = time.perf_counter() - start
        reply.tool_calls = [(s["name"], _loads(s["args"])) for _, s in sorted(calls.items())]
        return reply


# Models that accept the server-side refusal fallback on the Claude API (not on Bedrock).
FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5")


class AnthropicClaude:
    def __init__(self, c: Candidate, timeout: float = 60.0) -> None:
        import anthropic  # optional dependency: pip install -e ".[bakeoff]"

        self.c = c
        if c.bedrock_region:
            self.client = anthropic.AsyncAnthropicBedrockMantle(aws_region=c.bedrock_region,
                                                               timeout=timeout)
        else:
            self.client = anthropic.AsyncAnthropic(api_key=c.api_key or None, timeout=timeout)
        self.use_fallback = not c.bedrock_region and c.model.startswith(FALLBACK_MODELS)

    async def complete(self, messages: list[dict], *, temperature: float = 0.3,
                       max_tokens: int = 800) -> str:
        """Same shape as core.llm.ChatClient.complete, so core helpers accept this client.
        temperature is ignored: the candidate's own config decides sampling."""
        return (await self.chat(messages, max_tokens=max_tokens)).text

    async def chat(self, messages: list[dict], *, tools: list[dict] | None = None,
                   max_tokens: int = 800) -> Reply:
        system, msgs = anthropic_messages(messages)
        # Adaptive thinking spends output tokens before the answer: leave room for it.
        params: dict[str, Any] = {"model": self.c.model, "max_tokens": max(max_tokens, 4000),
                                  "messages": msgs}
        if system:
            params["system"] = system
        if tools:
            params["tools"] = anthropic_tools(tools)
        params.update(self.c.extra)  # e.g. output_config: {effort: low}
        if self.use_fallback:
            params.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            manager = self.client.beta.messages.stream(**params)
        else:
            manager = self.client.messages.stream(**params)
        reply = Reply()
        start = time.perf_counter()
        async with manager as stream:
            async for event in stream:
                if reply.ttft is None and event.type == "content_block_start" and \
                        event.content_block.type in ("text", "tool_use"):
                    reply.ttft = time.perf_counter() - start
            message = await stream.get_final_message()
        reply.total = time.perf_counter() - start
        reply.refused = message.stop_reason == "refusal"
        for block in message.content:
            if block.type == "text":
                reply.text += block.text
            elif block.type == "tool_use":
                reply.tool_calls.append((block.name, dict(block.input or {})))
        reply.input_tokens = message.usage.input_tokens or 0
        reply.output_tokens = message.usage.output_tokens or 0
        return reply


def make_client(c: Candidate):
    if c.provider == "openai":
        return OpenAICompatible(c)
    if c.provider == "anthropic":
        return AnthropicClaude(c)
    raise ValueError(f"{c.id}: unknown provider {c.provider!r} (use openai or anthropic)")


def _loads(raw: str) -> dict:
    try:
        value = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"_raw": raw}
