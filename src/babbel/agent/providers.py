"""Build speech-to-text, LLM and text-to-speech from configuration (spec D4-D6, NFR-09).

Default: any OpenAI-compatible endpoint via LLM_BASE_URL (point it at an EU-hosted service).
Add other LiveKit plugins (deepgram, elevenlabs, azure, ...) by installing them and adding
a branch below; the rest of the agent does not change.
"""

from __future__ import annotations

from babbel.config import Settings


def _given(value: str):
    from livekit.agents import NOT_GIVEN

    return value if value else NOT_GIVEN


def build_llm(s: Settings):
    from livekit.plugins import openai

    return openai.LLM(
        model=s.llm_model, base_url=_given(s.llm_base_url), api_key=_given(s.llm_api_key),
        temperature=0.6,
    )


def build_stt(s: Settings, keywords: list[str] | None = None):
    """keywords: names and places from the senior's profile, as vocabulary hints."""
    if s.stt_provider == "openai":
        from livekit.plugins import openai

        prompt = "Vlaams gesprek met een oudere persoon. Namen: " + ", ".join(keywords or [])
        return openai.STT(
            model=s.stt_model, language="nl", prompt=prompt,
            base_url=_given(s.llm_base_url), api_key=_given(s.llm_api_key),
        )
    raise ValueError(f"Unsupported STT_PROVIDER {s.stt_provider!r}; add it in providers.py")


def build_tts(s: Settings):
    if s.tts_provider == "openai":
        from livekit.plugins import openai

        return openai.TTS(
            voice=s.tts_voice, speed=s.tts_speed,
            instructions="Spreek warm, rustig en duidelijk, in zacht Vlaams, met korte pauzes.",
            base_url=_given(s.llm_base_url), api_key=_given(s.llm_api_key),
        )
    raise ValueError(f"Unsupported TTS_PROVIDER {s.tts_provider!r}; add it in providers.py")


def build_vad():
    from livekit.plugins import silero

    # Older speakers pause longer; do not treat a short pause as end of turn.
    return silero.VAD.load(min_silence_duration=0.8)
