"""Runtime configuration, read from environment variables.

Every provider is swappable by configuration (NFR-09). Defaults match the
"Open decisions" table in the spec (D2, D3, D9, D11).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


@dataclass(frozen=True)
class Settings:
    # General
    locale: str = field(default_factory=lambda: _env("BABBEL_LOCALE", "nl-BE"))
    timezone: str = field(default_factory=lambda: _env("BABBEL_TIMEZONE", "Europe/Brussels"))
    database_url: str = field(
        default_factory=lambda: _env("DATABASE_URL", "sqlite:///./babbel.db")
    )
    admin_token: str = field(default_factory=lambda: _env("BABBEL_ADMIN_TOKEN", "change-me-admin"))
    service_token: str = field(
        default_factory=lambda: _env("BABBEL_SERVICE_TOKEN", "change-me-service")
    )
    api_base_url: str = field(
        default_factory=lambda: _env("BABBEL_API_BASE_URL", "http://localhost:8000")
    )

    # Call behaviour (spec D2, FR-04, FR-05, FR-02, D9)
    default_quiet_start: str = field(default_factory=lambda: _env("BABBEL_QUIET_START", "08:00"))
    default_quiet_end: str = field(default_factory=lambda: _env("BABBEL_QUIET_END", "20:00"))
    default_call_time: str = field(default_factory=lambda: _env("BABBEL_CALL_TIME", "10:30"))  # D2: daily
    max_call_minutes: int = field(default_factory=lambda: _env_int("BABBEL_MAX_CALL_MINUTES", 12))
    retry_after_minutes: int = field(default_factory=lambda: _env_int("BABBEL_RETRY_MINUTES", 15))
    slot_window_minutes: int = field(default_factory=lambda: _env_int("BABBEL_SLOT_WINDOW", 10))
    escalation_wait_minutes: int = field(
        default_factory=lambda: _env_int("BABBEL_ESCALATION_WAIT", 3)
    )
    transcript_retention_days: int = field(
        default_factory=lambda: _env_int("BABBEL_TRANSCRIPT_RETENTION_DAYS", 30)
    )

    # LiveKit and telephony (D7)
    livekit_url: str = field(default_factory=lambda: _env("LIVEKIT_URL"))
    livekit_api_key: str = field(default_factory=lambda: _env("LIVEKIT_API_KEY"))
    livekit_api_secret: str = field(default_factory=lambda: _env("LIVEKIT_API_SECRET"))
    sip_outbound_trunk_id: str = field(default_factory=lambda: _env("SIP_OUTBOUND_TRUNK_ID"))
    agent_name: str = field(default_factory=lambda: _env("BABBEL_AGENT_NAME", "babbel"))

    # Models (D4, D5, D6): any OpenAI-compatible endpoint, ideally EU-hosted
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "gpt-4.1-mini"))
    llm_small_model: str = field(default_factory=lambda: _env("LLM_SMALL_MODEL", ""))
    stt_provider: str = field(default_factory=lambda: _env("STT_PROVIDER", "openai"))
    stt_model: str = field(default_factory=lambda: _env("STT_MODEL", "gpt-4o-transcribe"))
    tts_provider: str = field(default_factory=lambda: _env("TTS_PROVIDER", "openai"))
    tts_voice: str = field(default_factory=lambda: _env("TTS_VOICE", "alloy"))
    tts_speed: float = field(default_factory=lambda: _env_float("TTS_SPEED", 0.92))

    # Cost rates in EUR, for FR-51 / NFR-02 (set from your vendor contracts)
    rate_telephony_per_min: float = field(
        default_factory=lambda: _env_float("RATE_TELEPHONY_PER_MIN", 0.015)
    )
    rate_stt_per_min: float = field(default_factory=lambda: _env_float("RATE_STT_PER_MIN", 0.006))
    rate_tts_per_1k_chars: float = field(
        default_factory=lambda: _env_float("RATE_TTS_PER_1K_CHARS", 0.015)
    )
    rate_llm_in_per_1m: float = field(default_factory=lambda: _env_float("RATE_LLM_IN_PER_1M", 0.4))
    rate_llm_out_per_1m: float = field(
        default_factory=lambda: _env_float("RATE_LLM_OUT_PER_1M", 1.6)
    )
    cost_alert_per_min: float = field(
        default_factory=lambda: _env_float("COST_ALERT_PER_MIN", 0.08)
    )

    # Notifications (D10)
    notifier: str = field(default_factory=lambda: _env("BABBEL_NOTIFIER", "console"))
    sms_webhook_url: str = field(default_factory=lambda: _env("SMS_WEBHOOK_URL"))
    operator_phone: str = field(default_factory=lambda: _env("BABBEL_OPERATOR_PHONE"))

    # Help requests and proactive topics (FR-14, FR-16, D13, D14)
    partner_name: str = field(default_factory=lambda: _env("BABBEL_PARTNER_NAME", "Helpper"))
    partner_phone: str = field(default_factory=lambda: _env("BABBEL_PARTNER_PHONE"))
    topic_feeds_file: str = field(
        default_factory=lambda: _env("BABBEL_TOPIC_FEEDS", "config/topic_feeds.yaml")
    )
    wellbeing_medium_after: int = field(
        default_factory=lambda: _env_int("BABBEL_WELLBEING_MEDIUM_AFTER", 3)
    )  # FR-37: this many signals in 7 days -> Medium


def get_settings() -> Settings:
    return Settings()
