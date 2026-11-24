"""Minimal client for any OpenAI-compatible chat endpoint (spec D4, NFR-09).

Used outside the live call: post-call summaries (FR-11), the safety classifier, and
the eval harness. The live conversation itself runs through LiveKit's LLM plugin.
Point LLM_BASE_URL at an EU-hosted endpoint.
"""

from __future__ import annotations

import json
import re

import httpx

from babbel.core import prompts
from babbel.core.memory import parse_extraction
from babbel.core.safety import SafetyFlag, flags_from_classifier


class ChatClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30.0) -> None:
        if not base_url:
            raise ValueError("LLM_BASE_URL is not set")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def complete(
        self, messages: list[dict], *, temperature: float = 0.3, max_tokens: int = 800
    ) -> str:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]


async def summarise_call(client: ChatClient, transcript: str, locale: str = "nl-BE") -> dict:
    raw = await client.complete(
        [{"role": "user", "content": prompts.summary_prompt(transcript, locale)}], temperature=0.1
    )
    return parse_extraction(raw)


async def classify_utterance(
    client: ChatClient, utterance: str, locale: str = "nl-BE"
) -> list[SafetyFlag]:
    raw = await client.complete(
        [{"role": "user", "content": prompts.classifier_prompt(utterance, locale)}],
        temperature=0.0,
        max_tokens=200,
    )
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return []
    try:
        return flags_from_classifier(json.loads(match.group(0)), utterance)
    except json.JSONDecodeError:
        return []
