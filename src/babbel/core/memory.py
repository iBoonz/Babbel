"""Per-person memory (spec FR-10 to FR-13, memory rules in "Conversation design").

Rules enforced here:
- only facts the senior said are stored (the extractor is told so; we also filter)
- no bank details, codes or passwords are ever stored
- deleted facts never reach a prompt
- private facts never reach a family update
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

FORBIDDEN = re.compile(
    r"\b(pincode|pin|bankcode|iban|be\d{2}\s?\d{4}|rekeningnummer|wachtwoord|paswoord|"
    r"kaartnummer|digipass|itsme|code)\b",
    re.IGNORECASE,
)
CATEGORIES = {"family", "interest", "routine", "health_remark", "other"}


@dataclass
class Fact:
    text: str
    category: str = "other"
    private: bool = False
    source_call_id: int | None = None
    created_at: datetime | None = None
    deleted: bool = False


@dataclass
class Profile:
    preferred_name: str
    address_form: str = "u"  # FR-10 / D3
    family: list[str] = field(default_factory=list)
    interests: list[str] = field(default_factory=list)
    routines: list[str] = field(default_factory=list)
    avoid_topics: list[str] = field(default_factory=list)


def is_storable(fact_text: str) -> bool:
    return bool(fact_text.strip()) and not FORBIDDEN.search(fact_text)


def memory_context(profile: Profile, facts: list[Fact], max_items: int = 12) -> str:
    """Bullet list for the system prompt: profile basics plus the most recent live facts."""
    lines: list[str] = []
    if profile.family:
        lines.append("Familie: " + ", ".join(profile.family))
    if profile.interests:
        lines.append("Interesses: " + ", ".join(profile.interests))
    if profile.routines:
        lines.append("Gewoontes: " + ", ".join(profile.routines))
    live = [f for f in facts if not f.deleted]
    live.sort(key=lambda f: f.created_at or datetime.min, reverse=True)
    for fact in live[:max_items]:
        lines.append(fact.text)
    if not lines:
        return "  - (nog niets: dit is het eerste gesprek)"
    return "\n".join(f"  - {line}" for line in lines)


def shareable_facts(facts: list[Fact]) -> list[Fact]:
    return [f for f in facts if not f.deleted and not f.private]


def parse_extraction(raw: str) -> dict:
    """Parse the summariser's JSON robustly (models sometimes wrap JSON in prose or fences)."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {"summary": "", "shareable_summary": "", "facts": [], "private_topics": [],
                "wellbeing": ""}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"summary": "", "shareable_summary": "", "facts": [], "private_topics": [],
                "wellbeing": ""}
    facts = []
    for item in data.get("facts", []) or []:
        txt = str(item.get("text", "")).strip()
        if not is_storable(txt):
            continue
        cat = item.get("category", "other")
        facts.append(
            {
                "text": txt,
                "category": cat if cat in CATEGORIES else "other",
                "private": bool(item.get("private", False)),
            }
        )
    summary = " ".join(str(data.get("summary", "")).split()[:120])
    return {
        "summary": summary,
        "shareable_summary": str(data.get("shareable_summary", "")).strip(),
        "facts": facts,
        "private_topics": [str(t) for t in data.get("private_topics", []) or []],
        "wellbeing": str(data.get("wellbeing", "") or "").strip()[:200],  # FR-37
    }


def redact_private(shareable_summary: str, private_topics: list[str]) -> str:
    """Last line of defence: drop any sentence that mentions a private topic (FR-41)."""
    if not private_topics:
        return shareable_summary
    sentences = re.split(r"(?<=[.!?])\s+", shareable_summary)
    keep = [
        s for s in sentences if not any(t.lower() in s.lower() for t in private_topics if t.strip())
    ]
    return " ".join(keep).strip()
