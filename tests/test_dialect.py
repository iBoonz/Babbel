"""West- and East-Flemish lexicon (spec FR-19) and its safety phrases (FR-30 to FR-33)."""

from pathlib import Path

import yaml

from babbel.core import dialect
from babbel.core.safety import KeywordDetector

ROOT = Path(__file__).resolve().parents[1]
RULES = {"FR-30", "FR-31", "FR-32", "FR-33", "FR-35", "FR-36", "FR-37"}


def test_lexicon_is_well_formed():
    lex = dialect.load_lexicon()
    assert lex["sources"] and lex["regions"]["wvl"]["areas"] and lex["regions"]["ovl"]["areas"]
    seen = set()
    for e in lex["entries"]:
        assert e["forms"] and all(isinstance(f, str) and f for f in e["forms"]), e
        assert e["standard"] and e["example"], e
        assert e["region"] in dialect.REGIONS, e
        assert e["category"] in dialect.CATEGORIES, e
        assert e.get("safety") is None or e["safety"] in RULES, e
        key = e["forms"][0].lower()
        assert key not in seen, f"duplicate entry {key}"
        seen.add(key)


def test_every_dialect_safety_phrase_triggers_the_keyword_rules():
    """FR-30 to FR-33: a dialect safety phrase in the lexicon must not depend on the LLM alone,
    unless it is marked classifier_only (too broad, or usually about the past)."""
    kw = KeywordDetector("nl-BE")
    missed = []
    for e in dialect.load_lexicon()["entries"]:
        if e.get("safety") in {"FR-30", "FR-31", "FR-32", "FR-33"} and not e.get("classifier_only"):
            if e["safety"] not in {f.rule_id for f in kw.detect(e["example"])}:
                missed.append(f"{e['safety']}: {e['example']}")
    assert not missed, missed


def test_glossary_puts_health_first_and_filters_region():
    wvl = dialect.glossary("wvl")
    lines = wvl.splitlines()
    assert any("uitglijden" in line for line in lines[:8])
    assert "lochting" in wvl and "neuzekes" not in wvl  # West only, no Ghent sweets
    assert "neuzekes" in dialect.glossary("ovl")
    assert len(dialect.glossary("wvl", limit=5).splitlines()) <= 5 + 3  # header + 5 entries


def test_stt_hints_start_with_safety_words():
    hints = dialect.stt_hints("wvl", limit=10)
    assert len(hints) == 10 and len(set(hints)) == 10
    safety_forms = {e["forms"][0] for e in dialect.entries("wvl") if e.get("safety")}
    assert hints[0] in safety_forms


def test_dialect_cases_are_valid():
    cases = yaml.safe_load((ROOT / "evals/dialect_cases.yaml").read_text(encoding="utf-8"))["cases"]
    assert len({c["id"] for c in cases}) == len(cases)
    for c in cases:
        assert c["region"] in {"wvl", "ovl"} and c["text"] and c["meaning"], c["id"]
    gated = {c.get("expect", {}).get("args", {}).get("rule_id") for c in cases}
    assert {"FR-30", "FR-31", "FR-32"} <= gated
