"""Cost per call (spec FR-51, NFR-02). All amounts in EUR."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rates:
    telephony_per_min: float
    stt_per_min: float
    tts_per_1k_chars: float
    llm_in_per_1m: float
    llm_out_per_1m: float


@dataclass(frozen=True)
class Usage:
    call_seconds: float
    stt_seconds: float = 0.0
    tts_characters: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    telephony_legs: int = 1  # bridged calls (FR-42) bill two legs


@dataclass(frozen=True)
class CostBreakdown:
    telephony: float
    stt: float
    tts: float
    llm: float

    @property
    def total(self) -> float:
        return round(self.telephony + self.stt + self.tts + self.llm, 5)

    def per_minute(self, call_seconds: float) -> float:
        minutes = call_seconds / 60
        return round(self.total / minutes, 5) if minutes > 0 else 0.0


def compute(usage: Usage, rates: Rates) -> CostBreakdown:
    minutes = usage.call_seconds / 60
    return CostBreakdown(
        telephony=round(minutes * rates.telephony_per_min * usage.telephony_legs, 5),
        stt=round(usage.stt_seconds / 60 * rates.stt_per_min, 5),
        tts=round(usage.tts_characters / 1000 * rates.tts_per_1k_chars, 5),
        llm=round(
            usage.llm_input_tokens / 1_000_000 * rates.llm_in_per_1m
            + usage.llm_output_tokens / 1_000_000 * rates.llm_out_per_1m,
            5,
        ),
    )


def weekly_average_per_minute(rows: list[tuple[float, float]]) -> float:
    """rows: (total_cost, call_seconds). Weighted average EUR per minute."""
    total_cost = sum(r[0] for r in rows)
    total_min = sum(r[1] for r in rows) / 60
    return round(total_cost / total_min, 5) if total_min > 0 else 0.0
