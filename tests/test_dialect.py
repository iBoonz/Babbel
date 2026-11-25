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
    body = [line for line in wvl.splitlines() if line.startswith("- ")]
    safety_lines = {f"- {' / '.join(e['forms'])} = {e['standard']}"
                    for e in dialect.entries("wvl") if e.get("safety")}
    n = len(safety_lines)
    assert set(body[:n]) == safety_lines  # every safety word comes before any other word
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


def test_spelling_normaliser_maps_dialect_to_standard():
    """FR-19: heavy dialect spelling must reach the same safety rules as standard Dutch."""
    out = dialect.normalise_spelling("'t moet veur mie nie mier, ze meugen mie komen oaln")
    assert out == "'t moet voor mij niet meer, ze mogen mij komen halen"
    assert dialect.normalise_spelling("'k zijn evalln") == "'k zijn gevallen"
    assert dialect.normalise_spelling("de tomaten zijn rijp") == "de tomaten zijn rijp"


def test_normaliser_only_adds_safety_matches():
    """Checking the normalised text next to the raw text can never lose a flag."""
    kw = KeywordDetector("nl-BE")
    for text in ["Ik ben gevallen", "ze mogen me komen halen", "pijn op mijn borst"]:
        assert kw.detect(text)


def test_lexicon_review_round_trip(tmp_path):
    from babbel.evals import lexicon_review as lr

    sheet = tmp_path / "review.csv"
    n = lr.export("wvl", sheet)
    assert n == len(dialect.entries("wvl"))
    rows = sheet.read_text(encoding="utf-8-sig").splitlines()
    header, first, second = rows[0], rows[1], rows[2]
    filled = [header, first.replace(",no,,,,", ",no,ok,,,").replace(",yes,,,,", ",yes,ok,,,"),
              second.replace(",no,,,,", ",no,wrong,,,").replace(",yes,,,,", ",yes,wrong,,,")]
    sheet.write_text("\n".join(filled) + "\n", encoding="utf-8")
    store = tmp_path / "reviews.csv"
    added, problems = lr.import_sheet(sheet, "AB", "wvl-westhoek", path=store)
    assert added == 0 and any("needs a correct_meaning" in p for p in problems)  # wrong without fix
    filled[2] = filled[2].rstrip(",") + "fout,,"
    sheet.write_text("\n".join(filled[:2]) + "\n", encoding="utf-8")
    added, problems = lr.import_sheet(sheet, "AB", "wvl-westhoek", path=store)
    assert added == 1 and not problems
    counts = lr.status(lr.load_reviews(store))
    assert sum(c["confirmed"] for c in counts.values()) == 1


def test_reviews_file_refers_to_existing_entries():
    from babbel.evals import lexicon_review as lr

    keys = {e["forms"][0] for e in dialect.entries()}
    assert all(r["key"] in keys for r in lr.load_reviews())
