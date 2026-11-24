from datetime import datetime

from babbel.core.memory import (
    Fact,
    Profile,
    is_storable,
    memory_context,
    parse_extraction,
    redact_private,
)


def test_forbidden_facts_not_storable():
    assert not is_storable("Haar pincode is 1234")
    assert not is_storable("rekeningnummer BE12 3456")
    assert is_storable("Ze kweekt tomaten")


def test_memory_context_skips_deleted_and_orders_recent_first():
    p = Profile("Maria", family=["zoon Jan"])
    facts = [Fact("oud feit", created_at=datetime(2026, 1, 1)),
             Fact("nieuw feit", created_at=datetime(2026, 11, 1)),
             Fact("weg", deleted=True)]
    ctx = memory_context(p, facts)
    assert "weg" not in ctx and ctx.index("nieuw feit") < ctx.index("oud feit")


def test_parse_extraction_handles_fences_and_wellbeing():
    raw = 'Hier is het:\n```json\n{"summary": "Babbel over de tuin", "shareable_summary": "Goed gesprek.",' \
          ' "facts": [{"text": "kweekt tomaten", "category": "interest"},' \
          ' {"text": "pincode 1234", "category": "other"}], "private_topics": [],' \
          ' "wellbeing": "vertelde drie keer hetzelfde verhaal"}\n```'
    out = parse_extraction(raw)
    assert [f["text"] for f in out["facts"]] == ["kweekt tomaten"]
    assert out["wellbeing"] == "vertelde drie keer hetzelfde verhaal"
    assert parse_extraction("geen json")["wellbeing"] == ""
    assert parse_extraction("{kapot")["facts"] == []


def test_redact_private():
    out = redact_private("Ze praatte over de tuin. Ze vertelde over haar ruzie met de buurvrouw.", ["buurvrouw"])
    assert out == "Ze praatte over de tuin."
