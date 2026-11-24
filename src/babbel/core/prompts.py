"""Prompt and script builders (spec FR-14, FR-15, FR-17, FR-20 to FR-22, FR-45, "Conversation design")."""

from __future__ import annotations

from datetime import datetime

from babbel.core.locale import load_locale, text
from babbel.core.memory import Fact, Profile, memory_context


def disclosure_line(profile: Profile, locale: str = "nl-BE") -> str:
    """FR-20: the opening line, spoken before anything else."""
    form = "je" if profile.address_form == "je" else "u"
    return text(locale, f"disclosure.{form}", name=profile.preferred_name)


def first_call_line(
    profile: Profile,
    introduced_by: dict | None = None,
    daily: bool = True,
    locale: str = "nl-BE",
) -> str:
    """FR-17: first call only. Introduces Babbel together with the family member who set it up.

    introduced_by: {"name": "Jan", "relation": "zoon"} (the family admin), or None.
    """
    form = "je" if profile.address_form == "je" else "u"
    if introduced_by and introduced_by.get("name"):
        possessive = "je" if form == "je" else "uw"
        relation = (introduced_by.get("relation") or "").strip()
        family = f"{possessive} {relation} {introduced_by['name']}" if relation else introduced_by["name"]
    else:
        family = text(locale, f"first_call.family_fallback.{form}")
    frequency = text(locale, "first_call.frequency_daily" if daily else "first_call.frequency_some")
    return text(locale, f"first_call.{form}", name=profile.preferred_name, family=family,
                frequency=frequency)


def opening_line(profile: Profile, context: dict | None = None, locale: str = "nl-BE") -> str:
    """The first words of a call: the FR-17 introduction on the first call, else the FR-20 line."""
    context = context or {}
    if context.get("first_call"):
        return first_call_line(profile, context.get("introduced_by"), context.get("daily", True), locale)
    return disclosure_line(profile, locale)


def _bullets(items: list[str], empty: str = "geen") -> str:
    return "\n".join(f"  - {item}" for item in items) if items else f"  - ({empty})"


def family_asks_block(asks: list[dict]) -> str:
    lines = []
    for ask in asks[:2]:
        who = ask.get("added_by") or "de familie"
        kind = "check" if ask.get("kind") == "check_in" else "vraag"
        lines.append(f"[id {ask['id']}] {who} ({kind}): {ask['text']}")
    return _bullets(lines)


def topics_block(topics: list[dict]) -> str:
    return _bullets([f"{t['title']}: {t.get('summary', '')}".rstrip(": ") for t in topics[:3]])


def activities_block(activities: list[dict]) -> str:
    out = []
    for a in activities[:2]:
        when = a.get("starts_at", "")
        try:
            when = human_when(datetime.fromisoformat(when))
        except (TypeError, ValueError):
            pass
        out.append(f"{a['title']}, {when}, {a.get('place', '')}".rstrip(", "))
    return _bullets(out)


def closing_line(profile: Profile, next_call: datetime | None, locale: str = "nl-BE") -> str:
    form = "je" if profile.address_form == "je" else "u"
    return text(
        locale,
        f"closing.{form}",
        name=profile.preferred_name,
        next_call=human_when(next_call, locale) if next_call else "binnenkort",
    )


def human_when(when: datetime, locale: str = "nl-BE") -> str:
    days = load_locale(locale)["weekdays"]
    return f"{days[when.weekday()]} om {when.strftime('%H.%M')} uur"


def system_prompt(
    profile: Profile,
    facts: list[Fact],
    now: datetime,
    locale: str = "nl-BE",
    context: dict | None = None,
) -> str:
    """context: optional {"family_asks": [...], "topics": [...], "activities": [...]} (FR-14/15/45)."""
    context = context or {}
    days = load_locale(locale)["weekdays"]
    today = f"{days[now.weekday()]} {now.strftime('%d/%m/%Y')}"
    return text(
        locale,
        "system_prompt",
        preferred_name=profile.preferred_name,
        address_form=profile.address_form,
        memory=memory_context(profile, facts),
        avoid_topics=", ".join(profile.avoid_topics) or "geen",
        today=today,
        family_asks=family_asks_block(context.get("family_asks", [])),
        topics=topics_block(context.get("topics", [])),
        activities=activities_block(context.get("activities", [])),
    ) + (feedback_note(locale) if context.get("ask_feedback") else "")


def feedback_note(locale: str = "nl-BE") -> str:
    """FR-52: every 3rd call, one feedback question before the close."""
    question = text(locale, "feedback_question")
    return (f"\n\nStel vlak voor het afsluiten deze ene vraag: \"{question}\" "
            "en gebruik record_feedback met good, okay of not_good.")


def summary_prompt(transcript: str, locale: str = "nl-BE") -> str:
    return text(locale, "summary_prompt", transcript=transcript)


def classifier_prompt(utterance: str, locale: str = "nl-BE") -> str:
    return text(locale, "classifier_prompt", utterance=utterance.replace('"', "'"))
