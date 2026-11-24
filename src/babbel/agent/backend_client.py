"""HTTP client from the agent worker to the backend's /internal endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx


class BackendClient:
    def __init__(self, base_url: str, service_token: str, timeout: float = 10.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-Service-Token": service_token},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def profile_by_phone(self, phone: str) -> dict[str, Any] | None:
        resp = await self._client.get(f"/internal/seniors/by-phone/{phone}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def register_inbound(self, senior_id: int, started_at: datetime) -> int:
        resp = await self._client.post(
            "/internal/calls", json={"senior_id": senior_id, "started_at": started_at.isoformat()}
        )
        resp.raise_for_status()
        return resp.json()["id"]

    async def complete_call(self, call_id: int, **payload: Any) -> dict:
        for key in ("started_at", "ended_at"):
            if isinstance(payload.get(key), datetime):
                payload[key] = payload[key].isoformat()
        resp = await self._client.post(f"/internal/calls/{call_id}/complete", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def raise_escalation(
        self, senior_id: int, rule_id: str, trigger_text: str, call_id: int | None
    ) -> dict:
        resp = await self._client.post(
            "/internal/escalations",
            json={"senior_id": senior_id, "rule_id": rule_id, "trigger_text": trigger_text,
                  "call_id": call_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def pause_calls(self, senior_id: int) -> None:
        (await self._client.post(f"/internal/seniors/{senior_id}/pause")).raise_for_status()

    async def forget(self, senior_id: int, text: str) -> dict:
        resp = await self._client.post(
            "/internal/memory/forget", json={"senior_id": senior_id, "text": text}
        )
        resp.raise_for_status()
        return resp.json()

    async def feedback(self, call_id: int, score: str) -> None:
        resp = await self._client.post(f"/internal/calls/{call_id}/feedback", json={"score": score})
        resp.raise_for_status()

    async def answer_question(self, question_id: int, answer: str, call_id: int | None) -> None:
        """FR-45: store an ask-list answer as said."""
        resp = await self._client.post(
            f"/internal/questions/{question_id}/answer", json={"answer": answer, "call_id": call_id}
        )
        resp.raise_for_status()

    async def request_help(
        self, senior_id: int, kind: str, details: str, call_id: int | None
    ) -> dict:
        """FR-16: the person accepted help from family or a partner."""
        resp = await self._client.post(
            "/internal/help-requests",
            json={"senior_id": senior_id, "kind": kind, "details": details, "call_id": call_id},
        )
        resp.raise_for_status()
        return resp.json()
