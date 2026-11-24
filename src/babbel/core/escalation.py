"""Escalation chain (spec section "Safety and escalation rules").

Chain order: family admin -> other contacts by priority -> care partner -> operator.
Urgent and critical flags call and text the first person immediately and move down
the chain every `wait_minutes` until someone acknowledges. High and medium flags send
one SMS to the family admin. Low flags (FR-36, FR-37) are added to the next family update.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from babbel.core.locale import text
from babbel.core.safety import SafetyFlag, Severity

ROLE_ORDER = {"admin": 0, "contact": 1, "care_partner": 2, "operator": 3}


@dataclass(frozen=True)
class ChainMember:
    name: str
    phone: str
    role: str  # admin | contact | care_partner | operator
    priority: int = 100
    opted_out: bool = False


@dataclass(frozen=True)
class EscalationStep:
    offset_minutes: int
    member: ChainMember
    channels: tuple[str, ...]  # ("call", "sms") or ("sms",)


def build_chain(members: list[ChainMember], operator_phone: str | None = None) -> list[ChainMember]:
    chain = [m for m in members if not m.opted_out and m.phone]
    if operator_phone:
        chain.append(ChainMember("Operator", operator_phone, "operator", 999))
    return sorted(chain, key=lambda m: (ROLE_ORDER.get(m.role, 9), m.priority))


def plan(flag: SafetyFlag, chain: list[ChainMember], wait_minutes: int = 3) -> list[EscalationStep]:
    if not chain:
        return []
    if flag.severity >= Severity.URGENT:
        return [
            EscalationStep(i * wait_minutes, member, ("call", "sms"))
            for i, member in enumerate(chain)
        ]
    if flag.severity >= Severity.MEDIUM:
        return [EscalationStep(0, chain[0], ("sms",))]
    return []  # LOW: reported in the next family update (FR-36, FR-37)


def message(
    flag: SafetyFlag,
    *,
    senior_name: str,
    at: datetime,
    locale: str = "nl-BE",
    count: int | None = None,
) -> str:
    trigger = flag.trigger_text if len(flag.trigger_text) <= 80 else flag.trigger_text[:77] + "..."
    return text(
        locale,
        f"escalation_messages.{flag.rule_id}",
        name=senior_name,
        time=at.strftime("%H:%M"),
        trigger=trigger,
        count=count if count is not None else "",
    )
