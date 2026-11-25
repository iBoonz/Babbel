"""Safety and wellbeing detection (spec FR-30 to FR-37).

Every senior utterance passes through two detectors:
1. fast keyword rules from the locale pack (this module, deterministic, offline)
2. an LLM classifier (see babbel.core.llm.classify_utterance)

`combine` merges both. A false alarm is acceptable; a missed emergency is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum

from babbel.core.dialect import normalise_spelling
from babbel.core.locale import load_locale


class Severity(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    URGENT = 4
    CRITICAL = 5

    @classmethod
    def parse(cls, value: str) -> "Severity":
        return cls[value.upper()]


# Default severity per rule, used when the LLM classifier raises a rule.
RULE_SEVERITY: dict[str, Severity] = {
    "FR-30": Severity.URGENT,
    "FR-31": Severity.CRITICAL,
    "FR-32": Severity.CRITICAL,
    "FR-33": Severity.HIGH,
    "FR-34": Severity.MEDIUM,
    "FR-35": Severity.MEDIUM,
    "FR-36": Severity.LOW,
    "FR-37": Severity.LOW,  # raised to MEDIUM by the backend when it grows (see main.raise_escalation)
}


@dataclass(frozen=True)
class SafetyFlag:
    rule_id: str
    severity: Severity
    trigger_text: str
    source: str  # "keyword" | "llm" | "agent_tool" | "system"


def normalise(utterance: str) -> str:
    text = utterance.lower()
    text = text.replace("’", "'").replace("‘", "'").replace("`", "'")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class KeywordDetector:
    """Regex rules loaded from the locale pack (safety_patterns)."""

    def __init__(self, locale: str = "nl-BE") -> None:
        spec = load_locale(locale)["safety_patterns"]
        self._rules: list[tuple[str, Severity, list[re.Pattern[str]]]] = []
        for rule_id, rule in spec.items():
            severity = Severity.parse(rule["severity"])
            patterns = [re.compile(p) for p in rule["patterns"]]
            self._rules.append((rule_id, severity, patterns))

    def detect(self, utterance: str) -> list[SafetyFlag]:
        text = normalise(utterance)
        # FR-19: also check the sentence with dialect spellings mapped to standard forms, so
        # "ze meugen mie komen oaln" is caught like "ze mogen mij komen halen". Raw text first:
        # normalising can only add matches, never remove one.
        variants = {text, normalise_spelling(text)}
        flags: list[SafetyFlag] = []
        for rule_id, severity, patterns in self._rules:
            if any(p.search(v) for p in patterns for v in variants):
                flags.append(SafetyFlag(rule_id, severity, utterance.strip(), "keyword"))
        return flags


class IntentDetector:
    """Non-safety intents spoken by the senior: pause calls (FR-06), forget (FR-13), private."""

    def __init__(self, locale: str = "nl-BE") -> None:
        spec = load_locale(locale)["intent_patterns"]
        self._intents = {name: [re.compile(p) for p in pats] for name, pats in spec.items()}

    def detect(self, utterance: str) -> set[str]:
        text = normalise(utterance)
        return {name for name, pats in self._intents.items() if any(p.search(text) for p in pats)}


_STOPWORDS = {
    "de", "het", "een", "en", "ik", "'k", "k", "u", "je", "ge", "gij", "is", "es", "dat", "da",
    "die", "van", "in", "op", "te", "en", "nu", "nog", "ja", "nee", "neen", "wel", "maar", "zo",
    "al", "er", "eh", "euh", "hé", "he", "mijn", "men", "me", "mij", "was", "ben", "heb",
}


def _content_words(utterance: str) -> frozenset[str]:
    words = re.findall(r"[a-zà-ÿ']+", normalise(utterance))
    return frozenset(w for w in words if w not in _STOPWORDS and len(w) > 2)


class RepetitionTracker:
    """FR-37: notice when the person tells the same story or asks the same question again.

    Two utterances count as the same when their content words overlap strongly (Jaccard).
    Short replies ("ja", "dat is waar") are ignored. Fires once per call when one statement
    has come back `threshold` times.
    """

    def __init__(self, threshold: int = 3, similarity: float = 0.6, min_words: int = 4) -> None:
        self.threshold = threshold
        self.similarity = similarity
        self.min_words = min_words
        self._seen: list[tuple[frozenset[str], int, str]] = []
        self.fired = False

    def add(self, utterance: str) -> SafetyFlag | None:
        words = _content_words(utterance)
        if len(words) < self.min_words:
            return None
        for i, (prev, count, first) in enumerate(self._seen):
            overlap = len(words & prev) / len(words | prev)
            if overlap >= self.similarity:
                self._seen[i] = (prev | words, count + 1, first)
                if count + 1 >= self.threshold and not self.fired:
                    self.fired = True
                    return SafetyFlag("FR-37", Severity.LOW,
                                      f"herhaalde {count + 1} keer: {first}", "keyword")
                return None
        self._seen.append((words, 1, utterance.strip()))
        return None


def flags_from_classifier(result: dict, utterance: str) -> list[SafetyFlag]:
    """Turn classifier JSON ({"flags": [{"rule_id": ...}]}) into flags; ignore unknown rules."""
    out: list[SafetyFlag] = []
    for item in result.get("flags", []) or []:
        rule_id = str(item.get("rule_id", "")).strip()
        if rule_id in RULE_SEVERITY:
            out.append(SafetyFlag(rule_id, RULE_SEVERITY[rule_id], utterance.strip(), "llm"))
    return out


def combine(*flag_lists: list[SafetyFlag]) -> list[SafetyFlag]:
    """Union by rule, keeping the highest severity; ordered most severe first."""
    best: dict[str, SafetyFlag] = {}
    for flags in flag_lists:
        for flag in flags:
            current = best.get(flag.rule_id)
            if current is None or flag.severity > current.severity:
                best[flag.rule_id] = flag
    return sorted(best.values(), key=lambda f: (-f.severity, f.rule_id))


def needs_immediate_escalation(flag: SafetyFlag) -> bool:
    return flag.severity >= Severity.URGENT
