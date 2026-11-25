"""West- and East-Flemish lexicon (spec FR-19, NFR-11).

Loads src/babbel/locales/dialects/vlaams.yaml and renders it as:
  - a glossary block for the language model prompt (glossary);
  - hint words for speech-to-text (stt_hints).

Whether the glossary goes into the live prompt is decided by the bake-off dialect suite
(babbel-bakeoff --suites dialect), which runs every case with and without it.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

import yaml

from babbel.core.locale import LOCALES_DIR, text

REGIONS = {"vl", "wvl", "ovl"}
CATEGORIES = {"grammar", "daily", "family", "feeling", "health", "home", "money", "time", "social",
              "false_friend"}
# Categories that matter most for understanding a senior correctly; listed first in the glossary.
PRIORITY = ("health", "feeling", "false_friend", "money", "grammar", "time", "daily", "home", "family",
            "social")


@lru_cache(maxsize=4)
def load_lexicon(name: str = "vlaams") -> dict[str, Any]:
    path = LOCALES_DIR / "dialects" / f"{name}.yaml"
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def entries(region: str | None = None, name: str = "vlaams") -> list[dict]:
    """Entries for one region ('wvl' or 'ovl'), always including general Flemish ('vl')."""
    items = load_lexicon(name)["entries"]
    if region is None:
        return list(items)
    return [e for e in items if e["region"] in ("vl", region)]


def glossary(region: str | None = None, locale: str = "nl-BE", limit: int = 300,
             name: str = "vlaams") -> str:
    """Compact prompt block: 'form / form = meaning' per line; safety words first, then by category."""
    ranked = sorted(entries(region, name),
                    key=lambda e: (e.get("safety") is None, PRIORITY.index(e["category"])))
    lines = [f"- {' / '.join(e['forms'])} = {e['standard']}" for e in ranked[:limit]]
    return text(locale, "dialect_glossary_header").rstrip() + "\n" + "\n".join(lines)


def stt_hints(region: str | None = None, limit: int = 40, name: str = "vlaams") -> list[str]:
    """Dialect forms to bias speech recognition towards; safety and health words first."""
    ranked = sorted(entries(region, name),
                    key=lambda e: (e.get("safety") is None, PRIORITY.index(e["category"])))
    seen, out = set(), []
    for e in ranked:
        form = e["forms"][0]
        if form not in seen:
            seen.add(form)
            out.append(form)
    return out[:limit]


@lru_cache(maxsize=4)
def _spelling_rules(name: str = "vlaams") -> tuple[tuple[re.Pattern[str], str], ...]:
    pairs = [(form.lower(), rule["standard"]) for rule in load_lexicon(name).get("spelling", [])
             for form in rule["dialect"]]
    pairs.sort(key=lambda p: -len(p[0]))  # longest first: "nie mier" before "mier"
    return tuple((re.compile(r"(?<![\w'])" + re.escape(form) + r"(?![\w'])"), std)
                 for form, std in pairs)


def normalise_spelling(text: str, name: str = "vlaams") -> str:
    """Map dialect spellings to standard forms ("mie" -> "mij", "evalln" -> "gevallen"), so the
    safety keyword rules also catch a transcript written the way the dialect sounds (FR-19,
    FR-30 to FR-37). Expects lowercased text with normalised apostrophes.

    Used only for safety matching, next to the raw text: a wrong mapping (e.g. "mier", which is
    also the standard word for ant) can at most add a match, never remove one."""
    for pattern, standard in _spelling_rules(name):
        text = pattern.sub(standard, text)
    return text
