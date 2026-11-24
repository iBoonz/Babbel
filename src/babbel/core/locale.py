"""Locale packs (NFR-10). All user-facing words live in src/babbel/locales/<locale>.yaml."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"


@lru_cache(maxsize=8)
def load_locale(locale: str = "nl-BE") -> dict[str, Any]:
    path = LOCALES_DIR / f"{locale}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No locale pack for {locale!r} at {path}")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def text(locale: str, key: str, **values: Any) -> str:
    """Return a formatted message. Nested keys use dots, e.g. 'disclosure.u'."""
    node: Any = load_locale(locale)
    for part in key.split("."):
        node = node[part]
    values.setdefault("product", load_locale(locale)["product_name"])
    return str(node).format(**values)
