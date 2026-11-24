from datetime import datetime

from babbel.core.memory import Profile
from babbel.core.prompts import (
    closing_line,
    disclosure_line,
    first_call_line,
    opening_line,
    system_prompt,
)

MARIA = Profile("Maria")


def test_disclosure_says_computer_friend():
    line = disclosure_line(MARIA)
    assert line.startswith("Dag Maria, met Babbel, uw computervriend")
    assert "je computervriend die je" in disclosure_line(Profile("Mon", address_form="je"))


def test_first_call_introduces_with_family():
    line = first_call_line(MARIA, {"name": "Jan", "relation": "zoon"}, daily=True)
    assert "Samen met uw zoon Jan" in line and "elke dag" in line and "computervriend" in line
    assert "uw familie" in first_call_line(MARIA, None, daily=False)
    assert "geregeld" in first_call_line(MARIA, None, daily=False)


def test_opening_line_switches_on_first_call():
    assert opening_line(MARIA, {"first_call": True, "introduced_by": {"name": "Jan"}}).startswith("Dag Maria, ik ben")
    assert opening_line(MARIA, {"first_call": False}) == disclosure_line(MARIA)


def test_system_prompt_includes_context():
    prompt = system_prompt(MARIA, [], datetime(2026, 11, 24, 10, 30), context={
        "family_asks": [{"id": 7, "text": "Heb je je pillen genomen?", "kind": "check_in", "added_by": "dochter Lotte"},
                        {"id": 8, "text": "a"}, {"id": 9, "text": "b"}],
        "topics": [{"title": "Club Brugge wint", "summary": "2-0 tegen Gent"}],
        "activities": [{"title": "Kaartnamiddag", "starts_at": "2026-11-27T14:00:00+01:00", "place": "LDC"}],
    })
    assert "[id 7] dochter Lotte (check): Heb je je pillen genomen?" in prompt
    assert "[id 9]" not in prompt  # at most two asks per call
    assert "Club Brugge wint: 2-0 tegen Gent" in prompt
    assert "Kaartnamiddag, vrijdag om 14.00 uur, LDC" in prompt
    assert "FR-37" in prompt and "1813" in prompt and "dinsdag 24/11/2026" in prompt


def test_system_prompt_without_context():
    prompt = system_prompt(MARIA, [], datetime(2026, 11, 24))
    assert "(geen)" in prompt and "{" not in prompt.split("Gesprek")[0]


def test_closing_line():
    assert "dinsdag om 10.30 uur" in closing_line(MARIA, datetime(2026, 11, 24, 10, 30))


def test_feedback_question_only_when_due():
    now = datetime(2026, 11, 24)
    assert "record_feedback" in system_prompt(MARIA, [], now, context={"ask_feedback": True})
    assert "record_feedback" not in system_prompt(MARIA, [], now, context={})
