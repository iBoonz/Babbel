"""Outbound notifications to family and care partners (SMS first, spec D10).

`ConsoleNotifier` prints messages, for local development and tests.
`WebhookSmsNotifier` POSTs {"to", "body"} to a URL you control (for example a small
function that calls your SMS or WhatsApp provider), so no vendor SDK is hard-wired.
Phone calls to family (escalation channel "call") are placed by the agent worker
through LiveKit SIP; here they are recorded as a request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

log = logging.getLogger("babbel.notify")


class Notifier(Protocol):
    def sms(self, to: str, body: str) -> None: ...
    def request_call(self, to: str, body: str) -> None: ...


@dataclass
class ConsoleNotifier:
    sent: list[tuple[str, str, str]] = field(default_factory=list)

    def sms(self, to: str, body: str) -> None:
        self.sent.append(("sms", to, body))
        log.warning("SMS to %s: %s", to, body)

    def request_call(self, to: str, body: str) -> None:
        self.sent.append(("call", to, body))
        log.warning("CALL REQUEST to %s: %s", to, body)


@dataclass
class WebhookSmsNotifier:
    url: str
    timeout: float = 10.0

    def sms(self, to: str, body: str) -> None:
        httpx.post(self.url, json={"to": to, "body": body}, timeout=self.timeout).raise_for_status()

    def request_call(self, to: str, body: str) -> None:
        payload = {"to": to, "body": body, "kind": "call_request"}
        httpx.post(self.url, json=payload, timeout=self.timeout).raise_for_status()


def make_notifier(kind: str, sms_webhook_url: str = "") -> Notifier:
    if kind == "webhook" and sms_webhook_url:
        return WebhookSmsNotifier(sms_webhook_url)
    return ConsoleNotifier()
