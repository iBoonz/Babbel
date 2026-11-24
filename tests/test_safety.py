import pytest

from babbel.core.safety import (
    IntentDetector,
    KeywordDetector,
    RepetitionTracker,
    SafetyFlag,
    Severity,
    combine,
    flags_from_classifier,
    needs_immediate_escalation,
)

KW = KeywordDetector()


@pytest.mark.parametrize("utterance,rule", [
    ("Ik ben gisteren gevallen in de keuken", "FR-30"),
    ("'k zijn gevalle en 'k geraak nie meer recht", "FR-30"),           # West-Vlaams
    ("Ik heb pijn op mijn borst", "FR-31"),
    ("ik kan nie goed asem krijgen, ik kan nie asem", "FR-31"),
    ("Ik wil niet meer leven", "FR-32"),
    ("t is gedaan met mij", "FR-32"),                                  # Oost-Vlaams
    ("De bank heeft gebeld voor mijn pincode", "FR-33"),
    ("Mijn kleinzoon heeft een nieuw nummer en vraagt geld overschrijven", "FR-33"),
    ("Waar ben ik eigenlijk?", "FR-35"),
    ("'k ben zo bang 's nachts", "FR-37"),
    ("Ik heb schrik van inbrekers", "FR-37"),
    ("ik durf nie meer alleen slapen", "FR-37"),
])
def test_keyword_rules_fire(utterance, rule):
    assert rule in {f.rule_id for f in KW.detect(utterance)}


@pytest.mark.parametrize("utterance", [
    "Het weer is schoon vandaag",
    "Mijn zoon Jan komt zondag",
    "De tomaten in de tuin doen het goed",
])
def test_ordinary_talk_is_quiet(utterance):
    assert KW.detect(utterance) == []


def test_fear_is_low_severity():
    flag = next(f for f in KW.detect("ik ben bang") if f.rule_id == "FR-37")
    assert flag.severity == Severity.LOW
    assert not needs_immediate_escalation(flag)


def test_repetition_fires_once_on_third_time():
    t = RepetitionTracker()
    story = "Mijn zoon Jan komt zondag op bezoek met de kleinkinderen"
    assert t.add(story) is None
    assert t.add("ja") is None  # short replies ignored
    assert t.add("Weet u, mijn zoon Jan komt zondag op bezoek met de kleinkinderen") is None
    flag = t.add("Jan komt zondag met de kleinkinderen op bezoek, mijn zoon")
    assert flag is not None and flag.rule_id == "FR-37" and "3 keer" in flag.trigger_text
    assert t.add(story) is None  # once per call


def test_repetition_ignores_different_stories():
    t = RepetitionTracker()
    for u in ["Mijn zoon Jan komt zondag op bezoek",
              "De tomaten in de tuin zijn dit jaar heel goed gelukt",
              "Club Brugge heeft gisteren gewonnen tegen Gent"]:
        assert t.add(u) is None


def test_intents():
    det = IntentDetector()
    assert "pause_calls" in det.detect("Bel me niet meer alstublieft")
    assert "forget" in det.detect("Vergeet dat maar")
    assert "private" in det.detect("Dit blijft onder ons")


def test_classifier_and_combine():
    flags = flags_from_classifier({"flags": [{"rule_id": "FR-37"}, {"rule_id": "FR-99"}]}, "x")
    assert [f.rule_id for f in flags] == ["FR-37"]
    merged = combine(
        [SafetyFlag("FR-30", Severity.URGENT, "a", "keyword")],
        [SafetyFlag("FR-31", Severity.CRITICAL, "b", "llm"), SafetyFlag("FR-30", Severity.LOW, "c", "llm")],
    )
    assert [(f.rule_id, f.severity) for f in merged] == [("FR-31", Severity.CRITICAL), ("FR-30", Severity.URGENT)]
