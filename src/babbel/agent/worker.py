"""Babbel voice agent (LiveKit Agents).

One job = one phone call. The scheduler dispatches this agent into a room with metadata
{"call_id", "senior_id", "phone", "direction": "outbound"}; inbound calls arrive through a
LiveKit SIP dispatch rule with no metadata.

Spec coverage: FR-01/03 (calls), FR-05 (max length), FR-06 (pause), FR-10 (memory thread),
FR-11 (summary), FR-13 (forget), FR-14/15 (proactive topics and activities, via prompt),
FR-16 (help requests), FR-17 (first-call introduction), FR-20/21/22 (disclosure and
boundaries), FR-30 to FR-37 (safety and wellbeing: keyword rules and a repetition tracker on
every final transcript, plus the raise_safety_flag tool), FR-42 (basic bridge), FR-45 (family
ask list), FR-51 (usage for cost), FR-52 (feedback).

Run:  python -m babbel.agent.worker dev
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from google.protobuf.duration_pb2 import Duration
from livekit import api
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    MetricsCollectedEvent,
    RunContext,
    cli,
    function_tool,
)
from livekit.agents.metrics import LLMMetrics, STTMetrics, TTSMetrics

from babbel.agent import providers
from babbel.agent.backend_client import BackendClient
from babbel.config import get_settings
from babbel.core import prompts
from babbel.core.llm import ChatClient, classify_utterance, summarise_call
from babbel.core.locale import text
from babbel.core.memory import Fact, Profile
from babbel.core.safety import IntentDetector, KeywordDetector, RepetitionTracker, combine
from babbel.core.scheduler import parse_hhmm, within_quiet_hours

log = logging.getLogger("babbel.agent")
SETTINGS = get_settings()
KEYWORDS = KeywordDetector(SETTINGS.locale)
INTENTS = IntentDetector(SETTINGS.locale)


@dataclass
class CallState:
    call_id: int | None
    senior: dict[str, Any]
    started_at: datetime
    transcript: list[str] = field(default_factory=list)
    raised: set[str] = field(default_factory=set)
    stt_seconds: float = 0.0
    tts_chars: int = 0
    llm_in: int = 0
    llm_out: int = 0
    legs: int = 1
    outcome: str = "answered"
    feedback_due: bool = False
    finalized: bool = False
    answered_questions: set[int] = field(default_factory=set)
    repetition: RepetitionTracker = field(default_factory=RepetitionTracker)


def profile_from(senior: dict[str, Any]) -> Profile:
    return Profile(
        preferred_name=senior["preferred_name"],
        address_form=senior.get("address_form", "u"),
        family=senior.get("family", []),
        interests=senior.get("interests", []),
        routines=senior.get("routines", []),
        avoid_topics=senior.get("avoid_topics", []),
    )


def facts_from(senior: dict[str, Any]) -> list[Fact]:
    out = []
    for f in senior.get("facts", []):
        created = datetime.fromisoformat(f["created_at"]) if f.get("created_at") else None
        out.append(Fact(text=f["text"], category=f.get("category", "other"),
                        private=f.get("private", False), created_at=created))
    return out


async def escalate(state: CallState, backend: BackendClient, rule_id: str, trigger: str) -> None:
    """Raise each rule at most once per call; escalation runs in the backend."""
    if rule_id in state.raised:
        return
    state.raised.add(rule_id)
    try:
        await backend.raise_escalation(state.senior["id"], rule_id, trigger, state.call_id)
        log.warning("escalation %s raised for senior %s", rule_id, state.senior["id"])
    except Exception:
        log.exception("could not raise escalation %s", rule_id)


class BabbelAgent(Agent):
    def __init__(self, ctx: JobContext, state: CallState, backend: BackendClient) -> None:
        self.ctx = ctx
        self.state = state
        self.backend = backend
        self.profile = profile_from(state.senior)
        now = datetime.now(ZoneInfo(state.senior.get("timezone", SETTINGS.timezone)))
        self.context = state.senior.get("context", {}) or {}
        super().__init__(instructions=prompts.system_prompt(
            self.profile, facts_from(state.senior), now, SETTINGS.locale, self.context))

    async def on_enter(self) -> None:
        # FR-20: disclose being an AI before anything else (FR-17: longer introduction first time)
        self.session.say(prompts.opening_line(self.profile, self.context, SETTINGS.locale))

    async def _hang_up_after_playout(self, handle) -> None:
        await handle.wait_for_playout()
        await asyncio.sleep(0.5)
        await self.ctx.delete_room()

    @function_tool()
    async def raise_safety_flag(self, context: RunContext, rule_id: str, reason: str) -> str:
        """Alert the family when safety is at risk. Use immediately, keep talking calmly.

        Args:
            rule_id: FR-30 fall or injury, FR-31 medical emergency, FR-32 hopelessness or not
                wanting to live, FR-33 scam or someone asking for money or bank codes,
                FR-35 unusual confusion, FR-37 repeating the same story or question, or fear.
            reason: the person's words, briefly.
        """
        await escalate(self.state, self.backend, rule_id, reason)
        return "De familie wordt verwittigd. Beloof niet dat er hulp komt; blijf rustig bij de persoon."

    @function_tool()
    async def pause_calls(self, context: RunContext) -> str:
        """The person asks not to be called anymore (FR-06)."""
        await self.backend.pause_calls(self.state.senior["id"])
        handle = self.session.say(
            text(SETTINGS.locale, "paused_confirmation", name=self.profile.preferred_name),
            allow_interruptions=False,
        )
        asyncio.create_task(self._hang_up_after_playout(handle))
        return "Gesprekken gepauzeerd."

    @function_tool()
    async def forget(self, context: RunContext, what: str) -> str:
        """The person asks to forget something they said (FR-13).

        Args:
            what: the thing to forget, in a few words.
        """
        await self.backend.forget(self.state.senior["id"], what)
        return text(SETTINGS.locale, "forget_confirmation")

    @function_tool()
    async def not_now(self, context: RunContext) -> None:
        """The person says this is not a good moment to talk."""
        self.state.outcome = "not_now"
        handle = self.session.say(
            text(SETTINGS.locale, f"not_now.{self.profile.address_form}", name=self.profile.preferred_name),
            allow_interruptions=False,
        )
        asyncio.create_task(self._hang_up_after_playout(handle))

    @function_tool()
    async def end_call(self, context: RunContext) -> None:
        """Close the conversation warmly when it has naturally come to an end."""
        nxt = self.state.senior.get("next_call")
        next_dt = datetime.fromisoformat(nxt).astimezone(
            ZoneInfo(self.state.senior.get("timezone", SETTINGS.timezone))
        ) if nxt else None
        handle = self.session.say(
            prompts.closing_line(self.profile, next_dt, SETTINGS.locale), allow_interruptions=False
        )
        asyncio.create_task(self._hang_up_after_playout(handle))

    @function_tool()
    async def record_feedback(self, context: RunContext, score: str) -> str:
        """Store the person's answer to the feedback question (FR-52).

        Args:
            score: good, okay or not_good.
        """
        if self.state.call_id and score in {"good", "okay", "not_good"}:
            await self.backend.feedback(self.state.call_id, score)
        return "Bedankt."

    @function_tool()
    async def answer_family_question(self, context: RunContext, question_id: int, answer: str) -> str:
        """Store the person's answer to a question from the family ask list (FR-45).

        Args:
            question_id: the id shown in the instructions, e.g. 3 for "[id 3]".
            answer: the answer as the person said it, briefly, without judgement.
        """
        valid = {a["id"] for a in self.context.get("family_asks", [])}
        if question_id not in valid or question_id in self.state.answered_questions:
            return "Die vraag staat niet (meer) open."
        self.state.answered_questions.add(question_id)
        await self.backend.answer_question(question_id, answer, self.state.call_id)
        return "Bewaard. Ga gewoon verder met het gesprek."

    @function_tool()
    async def request_help(self, context: RunContext, kind: str, details: str) -> str:
        """The person wants to do something and agreed to help from family or a volunteer (FR-16).

        Args:
            kind: shopping, walk, outing, transport or other.
            details: what, when and where, in a few words.
        """
        kind = kind if kind in {"shopping", "walk", "outing", "transport"} else "other"
        await self.backend.request_help(self.state.senior["id"], kind, details, self.state.call_id)
        return ("De familie krijgt een berichtje en regelt het. Beloof niets concreets; zeg dat de "
                "familie of een vrijwilliger contact opneemt.")

    @function_tool()
    async def call_contact(self, context: RunContext, name: str) -> str:
        """Call a family member or friend for the person and connect them (FR-42).

        Args:
            name: the contact's name as the person said it.
        """
        contacts = self.state.senior.get("contacts", [])
        match = next((c for c in contacts if c["name"].lower() in name.lower()
                      or name.lower() in c["name"].lower()), None)
        if not match:
            return "Die persoon staat niet in de lijst van geverifieerde contacten. Bel niemand anders."
        tz = ZoneInfo(self.state.senior.get("timezone", SETTINGS.timezone))
        local = datetime.now(tz)
        if not within_quiet_hours(local, parse_hhmm(match["quiet_start"]), parse_hhmm(match["quiet_end"])):
            return f"{match['name']} wil op dit uur niet gebeld worden (FR-44). Stel een berichtje voor."
        if not SETTINGS.sip_outbound_trunk_id:
            return "Bellen is niet ingesteld in deze omgeving."
        try:
            await self.ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=self.ctx.room.name,
                    sip_trunk_id=SETTINGS.sip_outbound_trunk_id,
                    sip_call_to=match["phone"],
                    participant_identity=f"contact-{match['id']}",
                    participant_name=match["name"],
                    wait_until_answered=True,
                    ringing_timeout=Duration(seconds=30),
                )
            )
        except api.TwirpError:
            return f"{match['name']} neemt niet op. Bied aan om een berichtje te sturen (FR-43)."
        self.state.legs = 2
        # Basic version: announce to both, then step aside. TODO: private announcement and
        # explicit consent from the contact, using LiveKit's agent-assisted warm transfer.
        handle = self.session.say(
            text(SETTINGS.locale, "bridge_announcement", contact=match["name"], name=self.profile.preferred_name)
            + " Ik laat jullie nu babbelen.",
            allow_interruptions=False,
        )
        asyncio.create_task(self._leave_after_playout(handle))
        return "Verbonden."

    async def _leave_after_playout(self, handle) -> None:
        await handle.wait_for_playout()
        await finalize(self.ctx, self.state, self.backend)
        self.ctx.shutdown(reason="bridged")  # the agent leaves; the people stay connected


async def finalize(ctx: JobContext, state: CallState, backend: BackendClient) -> None:
    """Post-call processing (FR-11, FR-51): summary and facts, usage for cost. No audio stored."""
    if state.finalized or state.call_id is None:
        return
    state.finalized = True
    payload: dict[str, Any] = {
        "outcome": state.outcome,
        "started_at": state.started_at,
        "ended_at": datetime.now(timezone.utc),
        "usage": {
            "stt_seconds": state.stt_seconds,
            "tts_characters": state.tts_chars,
            "llm_input_tokens": state.llm_in,
            "llm_output_tokens": state.llm_out,
            "telephony_legs": state.legs,
        },
    }
    if state.outcome == "answered" and state.transcript and SETTINGS.llm_base_url:
        try:
            client = ChatClient(SETTINGS.llm_base_url, SETTINGS.llm_api_key,
                                SETTINGS.llm_small_model or SETTINGS.llm_model)
            payload.update(await summarise_call(client, "\n".join(state.transcript), SETTINGS.locale))
        except Exception:
            log.exception("summary failed; call is logged without summary")
    await backend.complete_call(state.call_id, **payload)


def setup(proc: JobProcess) -> None:
    proc.userdata["vad"] = providers.build_vad()


server = AgentServer(setup_fnc=setup)


@server.rtc_session(agent_name=SETTINGS.agent_name)
async def entrypoint(ctx: JobContext) -> None:
    backend = BackendClient(SETTINGS.api_base_url, SETTINGS.service_token)
    meta = json.loads(ctx.job.metadata or "{}")
    await ctx.connect()

    if meta.get("direction") == "outbound":
        senior = await backend.profile_by_phone(meta["phone"])
        call_id = meta.get("call_id")
        if senior is None or senior.get("status") != "active":
            if call_id:
                await backend.complete_call(call_id, outcome="failed")
            ctx.shutdown(reason="senior not active")
            return
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=SETTINGS.sip_outbound_trunk_id,
                    sip_call_to=meta["phone"],
                    participant_identity="senior",
                    wait_until_answered=True,
                    ringing_timeout=Duration(seconds=40),
                    max_call_duration=Duration(seconds=60 * (senior["max_call_minutes"] + 1)),
                )
            )
        except api.TwirpError as err:
            status = str(err.metadata.get("sip_status_code", "")) if hasattr(err, "metadata") else ""
            outcome = "busy" if status == "486" else "no_answer"
            await backend.complete_call(call_id, outcome=outcome)  # FR-02 retry is the scheduler's job
            ctx.shutdown(reason=outcome)
            return
        participant = await ctx.wait_for_participant(identity="senior")
    else:
        participant = await ctx.wait_for_participant()
        phone = participant.attributes.get("sip.phoneNumber", "")
        senior = await backend.profile_by_phone(phone) if phone else None
        if senior is None:
            # FR-03: unknown caller, generic message, no personal data
            session = AgentSession(tts=providers.build_tts(SETTINGS))
            await session.start(agent=Agent(instructions="Zeg enkel de boodschap."), room=ctx.room)
            handle = session.say(text(SETTINGS.locale, "inbound_unknown"), allow_interruptions=False)
            await handle.wait_for_playout()
            await ctx.delete_room()
            return
        call_id = await backend.register_inbound(senior["id"], datetime.now(timezone.utc))

    state = CallState(call_id=call_id, senior=senior, started_at=datetime.now(timezone.utc))
    keywords = list(senior.get("family", [])) + [c["name"] for c in senior.get("contacts", [])]
    session = AgentSession(
        stt=providers.build_stt(SETTINGS, keywords),
        llm=providers.build_llm(SETTINGS),
        tts=providers.build_tts(SETTINGS),
        vad=ctx.proc.userdata["vad"],
        min_endpointing_delay=1.0,  # older speakers pause; wait before answering
        max_endpointing_delay=4.0,
    )

    classifier = (
        ChatClient(SETTINGS.llm_base_url, SETTINGS.llm_api_key, SETTINGS.llm_small_model)
        if SETTINGS.llm_base_url and SETTINGS.llm_small_model else None
    )

    @session.on("user_input_transcribed")
    def on_transcript(ev) -> None:
        if not ev.is_final or not ev.transcript.strip():
            return
        utterance = ev.transcript.strip()
        state.transcript.append(f"Persoon: {utterance}")
        # Safety net: keyword rules fire even if the LLM forgets to call the tool
        for flag in KEYWORDS.detect(utterance):
            asyncio.create_task(escalate(state, backend, flag.rule_id, utterance))
        # FR-37: the same story or question again and again within this call
        repeated = state.repetition.add(utterance)
        if repeated is not None:
            asyncio.create_task(escalate(state, backend, repeated.rule_id, repeated.trigger_text))
        if classifier is not None:
            async def run_classifier() -> None:
                for flag in combine(await classify_utterance(classifier, utterance, SETTINGS.locale)):
                    await escalate(state, backend, flag.rule_id, utterance)
            asyncio.create_task(run_classifier())
        if "private" in INTENTS.detect(utterance):
            state.transcript.append("[De persoon wil het vorige onderwerp prive houden.]")

    @session.on("conversation_item_added")
    def on_item(ev) -> None:
        item = ev.item
        if getattr(item, "role", None) == "assistant" and getattr(item, "text_content", None):
            state.transcript.append(f"Babbel: {item.text_content}")

    @session.on("metrics_collected")
    def on_metrics(ev: MetricsCollectedEvent) -> None:
        m = ev.metrics
        if isinstance(m, LLMMetrics):
            state.llm_in += m.prompt_tokens
            state.llm_out += m.completion_tokens
        elif isinstance(m, STTMetrics):
            state.stt_seconds += m.audio_duration
        elif isinstance(m, TTSMetrics):
            state.tts_chars += m.characters_count

    async def on_shutdown() -> None:
        try:
            await finalize(ctx, state, backend)
        finally:
            await backend.aclose()

    ctx.add_shutdown_callback(on_shutdown)

    agent = BabbelAgent(ctx, state, backend)
    await session.start(agent=agent, room=ctx.room)

    # FR-05: warn one minute before the limit, then close warmly
    async def enforce_max_length() -> None:
        limit = senior["max_call_minutes"] * 60
        await asyncio.sleep(max(limit - 60, 30))
        session.say(text(SETTINGS.locale, "max_length_warning"))
        await asyncio.sleep(50)
        nxt = senior.get("next_call")
        next_dt = datetime.fromisoformat(nxt) if nxt else None
        handle = session.say(prompts.closing_line(agent.profile, next_dt, SETTINGS.locale),
                             allow_interruptions=False)
        await handle.wait_for_playout()
        await ctx.delete_room()

    asyncio.create_task(enforce_max_length())


if __name__ == "__main__":
    cli.run_app(server)
