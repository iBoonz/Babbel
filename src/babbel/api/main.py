"""Babbel backend (spec "Data model and API").

Admin endpoints: header X-Admin-Token (prototype; operator login with MFA is NFR-06 work).
Internal endpoints for the agent worker: header X-Service-Token.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from babbel.api.db import (
    AuditEvent,
    Call,
    CallSummary,
    Consent,
    Contact,
    Escalation,
    EscalationStepRow,
    FamilyQuestion,
    Feedback,
    HelpRequest,
    LocalActivity,
    MemoryFact,
    ScheduleSlot,
    Senior,
    TopicItem,
    get_session,
    utcnow,
)
from babbel.config import get_settings
from babbel.core import cost as costmod
from babbel.core import escalation as esc
from babbel.core.locale import text
from babbel.core.memory import is_storable, redact_private
from babbel.core.notify import Notifier, make_notifier
from babbel.core.safety import RULE_SEVERITY, SafetyFlag, Severity
from babbel.core.scheduler import (
    CallRecord,
    SeniorSchedule,
    Slot,
    consecutive_missed_slots,
    missed_call_action,
    next_slot_after,
    parse_hhmm,
)

settings = get_settings()
app = FastAPI(title="Babbel API", version="0.1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.state.notifier = make_notifier(settings.notifier, settings.sms_webhook_url)


# ---------- auth ----------


def require_admin(
    x_admin_token: Optional[str] = Header(default=None), token: Optional[str] = Query(default=None)
) -> str:
    if (x_admin_token or token) != get_settings().admin_token:
        raise HTTPException(401, "admin token required")
    return "operator"


def require_service(x_service_token: Optional[str] = Header(default=None)) -> str:
    if x_service_token != get_settings().service_token:
        raise HTTPException(401, "service token required")
    return "agent"


def get_notifier(request: Request) -> Notifier:
    return request.app.state.notifier


def audit(session: Session, actor: str, action: str, target: str) -> None:
    session.add(AuditEvent(actor=actor, action=action, target=target))


# ---------- schemas ----------


class SeniorIn(BaseModel):
    display_name: str
    preferred_name: str
    phone: str
    address_form: str = "u"
    timezone: str = "Europe/Brussels"
    quiet_start: str = Field(default_factory=lambda: get_settings().default_quiet_start)
    quiet_end: str = Field(default_factory=lambda: get_settings().default_quiet_end)
    max_call_minutes: int = 12
    municipality: str = ""
    # D2: daily at 10:30 by default; the family can choose fewer days
    call_time: str = Field(default_factory=lambda: get_settings().default_call_time)
    call_days: list[int] = Field(default_factory=lambda: list(range(7)))
    family: list[str] = []
    interests: list[str] = []
    routines: list[str] = []
    avoid_topics: list[str] = []


class SeniorPatch(BaseModel):
    preferred_name: Optional[str] = None
    address_form: Optional[str] = None
    quiet_start: Optional[str] = None
    quiet_end: Optional[str] = None
    max_call_minutes: Optional[int] = None
    municipality: Optional[str] = None
    status: Optional[str] = None
    family: Optional[list[str]] = None
    interests: Optional[list[str]] = None
    routines: Optional[list[str]] = None
    avoid_topics: Optional[list[str]] = None


class ContactIn(BaseModel):
    name: str
    phone: str
    relation: str = ""
    role: str = "contact"
    priority: int = 100
    whatsapp_opt_in: bool = False


class VerifyIn(BaseModel):
    code: str


class SlotIn(BaseModel):
    weekday: int = Field(ge=0, le=6)
    time: str


class ScheduleIn(BaseModel):
    slots: list[SlotIn]


class FactIn(BaseModel):
    text: str
    category: str = "other"
    private: bool = False


class UsageIn(BaseModel):
    stt_seconds: float = 0.0
    tts_characters: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    telephony_legs: int = 1


class CallCompleteIn(BaseModel):
    outcome: str
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    summary: str = ""
    shareable_summary: str = ""
    facts: list[FactIn] = []
    private_topics: list[str] = []
    wellbeing: str = ""  # FR-37 observation from the summariser
    usage: UsageIn = UsageIn()


class InboundCallIn(BaseModel):
    senior_id: int
    started_at: Optional[datetime] = None


class EscalationIn(BaseModel):
    senior_id: int
    rule_id: str
    trigger_text: str = ""
    call_id: Optional[int] = None


class ForgetIn(BaseModel):
    senior_id: int
    text: str


class FeedbackIn(BaseModel):
    score: str = Field(pattern="^(good|okay|not_good)$")


class QuestionIn(BaseModel):
    text: str = Field(min_length=3, max_length=300)
    kind: str = Field(default="question", pattern="^(question|check_in)$")
    recurring: bool = False
    added_by_contact_id: Optional[int] = None


class AnswerIn(BaseModel):
    answer: str = Field(max_length=500)
    call_id: Optional[int] = None


class HelpRequestIn(BaseModel):
    senior_id: int
    kind: str = Field(pattern="^(shopping|walk|outing|transport|other)$")
    details: str = ""
    call_id: Optional[int] = None


class HelpStatusIn(BaseModel):
    status: str = Field(pattern="^(requested|confirmed|done|cancelled)$")


class ActivityIn(BaseModel):
    municipality: str
    title: str
    starts_at: datetime
    place: str = ""
    tags: list[str] = []
    source: str = "operator"


class TopicIn(BaseModel):
    interest: str
    title: str
    summary: str = ""
    source: str = ""
    url: str = ""
    published_at: Optional[datetime] = None


# ---------- helpers ----------


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _senior_or_404(session: Session, senior_id: int) -> Senior:
    senior = session.get(Senior, senior_id)
    if not senior or senior.status == "deleted":
        raise HTTPException(404, "senior not found")
    return senior


def schedule_for(session: Session, senior: Senior) -> SeniorSchedule:
    slots = session.exec(select(ScheduleSlot).where(ScheduleSlot.senior_id == senior.id)).all()
    return SeniorSchedule(
        senior_id=senior.id,
        timezone=senior.timezone,
        slots=[Slot(s.weekday, parse_hhmm(s.time)) for s in slots],
        quiet_start=parse_hhmm(senior.quiet_start),
        quiet_end=parse_hhmm(senior.quiet_end),
        active=senior.status == "active",
    )


def chain_for(session: Session, senior: Senior) -> list[esc.ChainMember]:
    contacts = session.exec(
        select(Contact).where(Contact.senior_id == senior.id, Contact.verified_at.is_not(None))
    ).all()
    members = [esc.ChainMember(c.name, c.phone, c.role, c.priority, c.opted_out) for c in contacts]
    return esc.build_chain(members, get_settings().operator_phone or None)


def admin_contact(session: Session, senior: Senior) -> Optional[Contact]:
    return session.exec(
        select(Contact).where(
            Contact.senior_id == senior.id,
            Contact.role == "admin",
            Contact.verified_at.is_not(None),
            Contact.opted_out == False,  # noqa: E712
        )
    ).first()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def open_family_asks(session: Session, senior: Senior, now: datetime, limit: int = 2) -> list[FamilyQuestion]:
    """FR-45: oldest open asks first; a recurring check-in is skipped once answered today."""
    rows = session.exec(
        select(FamilyQuestion)
        .where(FamilyQuestion.senior_id == senior.id, FamilyQuestion.status == "open")
        .order_by(FamilyQuestion.created_at)
    ).all()
    today = now.astimezone(ZoneInfo(senior.timezone)).date()
    out = []
    for q in rows:
        if q.recurring and q.answered_at and _aware(q.answered_at).astimezone(ZoneInfo(senior.timezone)).date() == today:
            continue
        out.append(q)
    return out[:limit]


def call_context(session: Session, senior: Senior, now: datetime, mark_offered: bool = False) -> dict:
    """What the next call needs beyond the profile (FR-14, FR-15, FR-17, FR-45)."""
    answered_calls = len(session.exec(
        select(Call.id).where(Call.senior_id == senior.id, Call.outcome == "answered")
    ).all())
    admin = admin_contact(session, senior)
    days = {s.weekday for s in session.exec(
        select(ScheduleSlot).where(ScheduleSlot.senior_id == senior.id)).all()}
    interests = [i.lower() for i in _split(senior.interests)]
    topics = session.exec(
        select(TopicItem)
        .where(TopicItem.senior_id == senior.id, TopicItem.offered_at.is_(None))
        .order_by(TopicItem.published_at.desc(), TopicItem.created_at.desc())
        .limit(3)
    ).all()
    activities = []
    if senior.municipality:
        horizon = now + timedelta(days=14)
        for a in session.exec(
            select(LocalActivity)
            .where(LocalActivity.municipality == senior.municipality)
            .order_by(LocalActivity.starts_at)
        ).all():
            start = _aware(a.starts_at)
            if not (now <= start <= horizon):
                continue
            tags = [t.lower() for t in _split(a.tags)]
            if tags and interests and not set(tags) & set(interests) and "iedereen" not in tags:
                continue
            activities.append(a)
            if len(activities) == 2:
                break
    if mark_offered:
        for t in topics:
            t.offered_at = now
        session.commit()
    return {
        "first_call": answered_calls == 0,
        "ask_feedback": answered_calls > 0 and (answered_calls + 1) % 3 == 0,  # FR-52
        "introduced_by": {"name": admin.name, "relation": admin.relation} if admin else None,
        "daily": len(days) >= 7,
        "family_asks": [
            {"id": q.id, "text": q.text, "kind": q.kind, "added_by": q.added_by_name}
            for q in open_family_asks(session, senior, now)
        ],
        "topics": [{"id": t.id, "title": t.title, "summary": t.summary, "interest": t.interest}
                   for t in topics],
        "activities": [
            {"id": a.id, "title": a.title, "starts_at": _aware(a.starts_at).astimezone(
                ZoneInfo(senior.timezone)).isoformat(), "place": a.place}
            for a in activities
        ],
    }


def open_escalation(
    session: Session,
    notifier: Notifier,
    senior: Senior,
    flag: SafetyFlag,
    call_id: Optional[int] = None,
    count: Optional[int] = None,
) -> Escalation:
    row = Escalation(
        senior_id=senior.id,
        call_id=call_id,
        rule_id=flag.rule_id,
        severity=flag.severity.name.lower(),
        trigger_text=flag.trigger_text[:500],
    )
    session.add(row)
    session.flush()
    now = utcnow()
    local = now.astimezone(ZoneInfo(senior.timezone))
    body = esc.message(flag, senior_name=senior.preferred_name, at=local, count=count)
    for step in esc.plan(flag, chain_for(session, senior), get_settings().escalation_wait_minutes):
        session.add(
            EscalationStepRow(
                escalation_id=row.id,
                due_at=now + timedelta(minutes=step.offset_minutes),
                to_name=step.member.name,
                to_phone=step.member.phone,
                channels=",".join(step.channels),
                message=body,
            )
        )
    session.flush()
    process_due_steps(session, notifier, now)
    return row


def process_due_steps(session: Session, notifier: Notifier, now: datetime) -> int:
    """Send steps that are due, unless the escalation was acknowledged. Returns steps sent."""
    sent = 0
    steps = session.exec(
        select(EscalationStepRow).where(
            EscalationStepRow.sent_at.is_(None), EscalationStepRow.skipped == False  # noqa: E712
        )
    ).all()
    for step in steps:
        due = step.due_at if step.due_at.tzinfo else step.due_at.replace(tzinfo=timezone.utc)
        if due > now:
            continue
        parent = session.get(Escalation, step.escalation_id)
        if parent and parent.status != "open":
            step.skipped = True
            continue
        if "sms" in step.channels:
            notifier.sms(step.to_phone, step.message)
        if "call" in step.channels:
            notifier.request_call(step.to_phone, step.message)
        step.sent_at = now
        sent += 1
    session.commit()
    return sent


# ---------- routes: health ----------


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": "0.1.0"}


# ---------- routes: seniors, contacts, schedule (FR-10, FR-40) ----------


@app.post("/seniors", status_code=201)
def create_senior(
    body: SeniorIn, session: Session = Depends(get_session), actor: str = Depends(require_admin)
) -> Senior:
    senior = Senior(
        display_name=body.display_name,
        preferred_name=body.preferred_name,
        phone=body.phone,
        address_form=body.address_form,
        timezone=body.timezone,
        quiet_start=body.quiet_start,
        quiet_end=body.quiet_end,
        max_call_minutes=body.max_call_minutes,
        municipality=body.municipality,
        family=", ".join(body.family),
        interests=", ".join(body.interests),
        routines=", ".join(body.routines),
        avoid_topics=", ".join(body.avoid_topics),
    )
    session.add(senior)
    session.flush()
    parse_hhmm(body.call_time)
    for day in sorted(set(body.call_days)):
        if 0 <= day <= 6:
            session.add(ScheduleSlot(senior_id=senior.id, weekday=day, time=body.call_time))
    audit(session, actor, "create_senior", f"senior:{senior.id}")
    session.commit()
    session.refresh(senior)
    return senior


@app.get("/seniors/{senior_id}")
def get_senior(
    senior_id: int, session: Session = Depends(get_session), _: str = Depends(require_admin)
) -> Senior:
    return _senior_or_404(session, senior_id)


@app.patch("/seniors/{senior_id}")
def patch_senior(
    senior_id: int,
    body: SeniorPatch,
    session: Session = Depends(get_session),
    actor: str = Depends(require_admin),
) -> Senior:
    senior = _senior_or_404(session, senior_id)
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(senior, key, ", ".join(value) if isinstance(value, list) else value)
    audit(session, actor, "patch_senior", f"senior:{senior_id}")
    session.commit()
    session.refresh(senior)
    return senior


@app.post("/seniors/{senior_id}/contacts", status_code=201)
def add_contact(
    senior_id: int,
    body: ContactIn,
    session: Session = Depends(get_session),
    notifier: Notifier = Depends(get_notifier),
    actor: str = Depends(require_admin),
) -> dict:
    _senior_or_404(session, senior_id)
    code = f"{random.SystemRandom().randint(0, 999999):06d}"
    contact = Contact(senior_id=senior_id, verification_code=code, **body.model_dump())
    session.add(contact)
    session.flush()
    notifier.sms(contact.phone, f"Babbel: uw verificatiecode is {code}.")
    audit(session, actor, "add_contact", f"contact:{contact.id}")
    session.commit()
    return {"id": contact.id, "verified": False}


@app.post("/contacts/{contact_id}/verify")
def verify_contact(
    contact_id: int,
    body: VerifyIn,
    session: Session = Depends(get_session),
    _: str = Depends(require_admin),
) -> dict:
    contact = session.get(Contact, contact_id)
    if not contact or contact.verification_code != body.code:
        raise HTTPException(400, "invalid code")
    contact.verified_at = utcnow()
    contact.verification_code = None
    session.commit()
    return {"id": contact.id, "verified": True}


@app.put("/seniors/{senior_id}/schedule")
def set_schedule(
    senior_id: int,
    body: ScheduleIn,
    session: Session = Depends(get_session),
    actor: str = Depends(require_admin),
) -> dict:
    _senior_or_404(session, senior_id)
    for old in session.exec(select(ScheduleSlot).where(ScheduleSlot.senior_id == senior_id)).all():
        session.delete(old)
    for slot in body.slots:
        parse_hhmm(slot.time)
        session.add(ScheduleSlot(senior_id=senior_id, weekday=slot.weekday, time=slot.time))
    audit(session, actor, "set_schedule", f"senior:{senior_id}")
    session.commit()
    return {"slots": [s.model_dump() for s in body.slots]}


@app.post("/seniors/{senior_id}/consents", status_code=201)
def add_consent(
    senior_id: int,
    kind: str,
    given_by: str,
    evidence: str = "",
    session: Session = Depends(get_session),
    actor: str = Depends(require_admin),
) -> Consent:
    _senior_or_404(session, senior_id)
    row = Consent(senior_id=senior_id, kind=kind, given_by=given_by, evidence=evidence)
    session.add(row)
    audit(session, actor, "add_consent", f"senior:{senior_id}:{kind}")
    session.commit()
    session.refresh(row)
    return row


# ---------- routes: memory (FR-11, FR-13) ----------


@app.get("/seniors/{senior_id}/memory")
def list_memory(
    senior_id: int, session: Session = Depends(get_session), _: str = Depends(require_admin)
) -> list[MemoryFact]:
    return session.exec(
        select(MemoryFact).where(MemoryFact.senior_id == senior_id, MemoryFact.deleted_at.is_(None))
    ).all()


@app.delete("/memory/{fact_id}")
def delete_memory(
    fact_id: int, session: Session = Depends(get_session), actor: str = Depends(require_admin)
) -> dict:
    fact = session.get(MemoryFact, fact_id)
    if not fact:
        raise HTTPException(404, "fact not found")
    fact.deleted_at = utcnow()
    audit(session, actor, "forget_fact", f"fact:{fact_id}")
    session.commit()
    return {"deleted": fact_id}


# ---------- routes: calls ----------


@app.get("/calls")
def list_calls(
    senior_id: Optional[int] = None,
    session: Session = Depends(get_session),
    _: str = Depends(require_admin),
) -> list[dict]:
    query = select(Call).order_by(Call.created_at.desc())
    if senior_id is not None:
        query = query.where(Call.senior_id == senior_id)
    rows = session.exec(query.limit(200)).all()
    return [
        {**r.model_dump(), "total_cost": round(r.total_cost, 4), "seconds": r.seconds} for r in rows
    ]


# ---------- routes: internal (agent worker) ----------


@app.get("/internal/seniors/by-phone/{phone}")
def senior_by_phone(
    phone: str, session: Session = Depends(get_session), _: str = Depends(require_service)
) -> dict:
    senior = session.exec(select(Senior).where(Senior.phone == phone)).first()
    if not senior or senior.status == "deleted":
        raise HTTPException(404, "unknown number")  # FR-03: caller gets the generic message
    facts = session.exec(
        select(MemoryFact)
        .where(MemoryFact.senior_id == senior.id, MemoryFact.deleted_at.is_(None))
        .order_by(MemoryFact.created_at.desc())
        .limit(12)
    ).all()
    contacts = session.exec(
        select(Contact).where(
            Contact.senior_id == senior.id,
            Contact.verified_at.is_not(None),
            Contact.opted_out == False,  # noqa: E712
        )
    ).all()
    nxt = next_slot_after(schedule_for(session, senior), utcnow())
    return {
        "id": senior.id,
        "preferred_name": senior.preferred_name,
        "address_form": senior.address_form,
        "status": senior.status,
        "timezone": senior.timezone,
        "max_call_minutes": senior.max_call_minutes,
        "family": _split(senior.family),
        "interests": _split(senior.interests),
        "routines": _split(senior.routines),
        "avoid_topics": _split(senior.avoid_topics),
        "facts": [
            {"id": f.id, "text": f.text, "category": f.category, "private": f.private,
             "created_at": f.created_at.isoformat()}
            for f in facts
        ],
        # FR-42: only verified contacts can be dialled
        "contacts": [
            {"id": c.id, "name": c.name, "relation": c.relation, "phone": c.phone,
             "quiet_start": c.quiet_start, "quiet_end": c.quiet_end}
            for c in contacts
        ],
        "next_call": nxt.isoformat() if nxt else None,
        "context": call_context(session, senior, utcnow(), mark_offered=True),
    }


@app.get("/internal/seniors/{senior_id}/topics")
def topics_for_call(
    senior_id: int, session: Session = Depends(get_session), _: str = Depends(require_service)
) -> dict:
    """FR-14, FR-15, FR-45: proactive topics, local activities and open asks for the next call."""
    return call_context(session, _senior_or_404(session, senior_id), utcnow())


@app.post("/internal/calls", status_code=201)
def register_inbound_call(
    body: InboundCallIn, session: Session = Depends(get_session), _: str = Depends(require_service)
) -> dict:
    _senior_or_404(session, body.senior_id)
    call = Call(senior_id=body.senior_id, direction="inbound", started_at=body.started_at or utcnow())
    session.add(call)
    session.commit()
    return {"id": call.id}


@app.post("/internal/calls/{call_id}/complete")
def complete_call(
    call_id: int,
    body: CallCompleteIn,
    session: Session = Depends(get_session),
    notifier: Notifier = Depends(get_notifier),
    _: str = Depends(require_service),
) -> dict:
    call = session.get(Call, call_id)
    if not call:
        raise HTTPException(404, "call not found")
    senior = _senior_or_404(session, call.senior_id)
    call.outcome = body.outcome
    call.started_at = body.started_at or call.started_at
    call.ended_at = body.ended_at or utcnow()

    # FR-51: cost per call by component
    s = get_settings()
    rates = costmod.Rates(
        s.rate_telephony_per_min, s.rate_stt_per_min, s.rate_tts_per_1k_chars,
        s.rate_llm_in_per_1m, s.rate_llm_out_per_1m,
    )
    breakdown = costmod.compute(costmod.Usage(call_seconds=call.seconds, **body.usage.model_dump()), rates)
    call.cost_telephony, call.cost_stt = breakdown.telephony, breakdown.stt
    call.cost_llm, call.cost_tts = breakdown.llm, breakdown.tts

    actions: list[str] = []
    if body.outcome == "answered":
        # FR-11: summary and facts, no audio
        shareable = redact_private(body.shareable_summary, body.private_topics)
        session.merge(CallSummary(call_id=call.id, text=body.summary, shareable_text=shareable))
        for fact in body.facts:
            if is_storable(fact.text):
                session.add(
                    MemoryFact(senior_id=senior.id, text=fact.text, category=fact.category,
                               private=fact.private, source_call_id=call.id)
                )
        # FR-37: an observation from the summary counts as a wellbeing signal
        if body.wellbeing:
            raised = session.exec(
                select(Escalation).where(Escalation.call_id == call.id, Escalation.rule_id == "FR-37")
            ).first()
            if raised is None:
                raise_wellbeing(session, notifier, senior, body.wellbeing, call.id)
                actions.append("wellbeing_signal")
        # FR-41: family update to the admin, with ask-list answers (FR-45) and observations (FR-37)
        admin = admin_contact(session, senior)
        if admin and shareable:
            local = (call.ended_at or utcnow()).astimezone(ZoneInfo(senior.timezone))
            msg = text("nl-BE", "family_update", name=senior.preferred_name,
                       time=local.strftime("%H:%M"), summary=shareable)
            for q in session.exec(
                select(FamilyQuestion).where(FamilyQuestion.call_id == call.id,
                                             FamilyQuestion.status == "answered")
            ).all():
                msg += text("nl-BE", "family_update_answer", question=q.text, answer=q.answer_text)
            notes = [e.trigger_text for e in session.exec(
                select(Escalation).where(Escalation.call_id == call.id,
                                         Escalation.rule_id.in_(["FR-36", "FR-37"]))
            ).all() if e.trigger_text]
            if notes:
                msg += text("nl-BE", "family_update_wellbeing", note="; ".join(notes[:2]))
            notifier.sms(admin.phone, msg)
            actions.append("family_update")
    else:
        # FR-34: missed-call rule
        records = [
            CallRecord(c.senior_id, c.idempotency_key or str(c.id), c.slot_start or c.created_at,
                       c.attempt, c.outcome)
            for c in session.exec(
                select(Call).where(Call.senior_id == senior.id, Call.direction == "outbound")
            ).all()
            if c.slot_start is not None
        ]
        action = missed_call_action(consecutive_missed_slots(records, senior.id))
        is_final_attempt = call.attempt >= 2 or call.direction != "outbound"
        if action != "none" and is_final_attempt:
            flag = SafetyFlag("FR-34", Severity.MEDIUM if action == "sms" else Severity.URGENT,
                              "geplande gesprekken niet opgenomen", "system")
            open_escalation(session, notifier, senior, flag, call.id,
                            count=consecutive_missed_slots(records, senior.id))
            actions.append(f"missed_call_{action}")
    session.commit()
    return {"id": call.id, "cost_total": breakdown.total, "actions": actions}


@app.post("/internal/escalations", status_code=201)
def raise_escalation(
    body: EscalationIn,
    session: Session = Depends(get_session),
    notifier: Notifier = Depends(get_notifier),
    _: str = Depends(require_service),
) -> dict:
    if body.rule_id not in RULE_SEVERITY:
        raise HTTPException(400, "unknown rule")
    senior = _senior_or_404(session, body.senior_id)
    if body.rule_id == "FR-37":
        row = raise_wellbeing(session, notifier, senior, body.trigger_text, body.call_id)
    else:
        flag = SafetyFlag(body.rule_id, RULE_SEVERITY[body.rule_id], body.trigger_text, "agent_tool")
        row = open_escalation(session, notifier, senior, flag, body.call_id)
    session.commit()
    return {"id": row.id, "severity": row.severity}


def raise_wellbeing(
    session: Session, notifier: Notifier, senior: Senior, trigger: str, call_id: Optional[int]
) -> Escalation:
    """FR-37: Low (noted in the family update); Medium (SMS to the family admin) when it grows,
    i.e. when this is the Nth signal in 7 days (BABBEL_WELLBEING_MEDIUM_AFTER, default 3)."""
    week_ago = utcnow() - timedelta(days=7)
    recent = [
        e for e in session.exec(
            select(Escalation).where(Escalation.senior_id == senior.id, Escalation.rule_id == "FR-37")
        ).all()
        if _aware(e.created_at) >= week_ago
    ]
    already_medium = any(e.severity == "medium" for e in recent)
    grows = len(recent) + 1 >= get_settings().wellbeing_medium_after and not already_medium
    flag = SafetyFlag("FR-37", Severity.MEDIUM if grows else Severity.LOW, trigger, "agent_tool")
    return open_escalation(session, notifier, senior, flag, call_id)


@app.post("/internal/seniors/{senior_id}/pause")
def pause_calls(
    senior_id: int,
    session: Session = Depends(get_session),
    notifier: Notifier = Depends(get_notifier),
    _: str = Depends(require_service),
) -> dict:
    """FR-06: the senior asked to stop calls."""
    senior = _senior_or_404(session, senior_id)
    senior.status = "paused"
    admin = admin_contact(session, senior)
    if admin:
        notifier.sms(admin.phone, f"Babbel: {senior.preferred_name} vroeg om niet meer te bellen. "
                     "De gesprekken zijn gepauzeerd.")
    audit(session, "agent", "pause_calls", f"senior:{senior_id}")
    session.commit()
    return {"status": "paused"}


@app.post("/internal/memory/forget")
def forget_by_voice(
    body: ForgetIn, session: Session = Depends(get_session), _: str = Depends(require_service)
) -> dict:
    """FR-13: delete facts that match what the senior asked to forget."""
    needle = body.text.lower().strip()
    hits = []
    for fact in session.exec(
        select(MemoryFact).where(MemoryFact.senior_id == body.senior_id, MemoryFact.deleted_at.is_(None))
    ).all():
        if needle and (needle in fact.text.lower() or fact.text.lower() in needle):
            fact.deleted_at = utcnow()
            hits.append(fact.id)
    audit(session, "agent", "forget_by_voice", f"senior:{body.senior_id}:{hits}")
    session.commit()
    return {"deleted": hits}


@app.post("/internal/calls/{call_id}/feedback")
def call_feedback(
    call_id: int,
    body: FeedbackIn,
    session: Session = Depends(get_session),
    _: str = Depends(require_service),
) -> dict:
    session.merge(Feedback(call_id=call_id, score=body.score))
    session.commit()
    return {"call_id": call_id, "score": body.score}


@app.post("/escalations/{escalation_id}/ack")
def acknowledge(
    escalation_id: int,
    by: str = "family",
    session: Session = Depends(get_session),
    actor: str = Depends(require_admin),
) -> dict:
    row = session.get(Escalation, escalation_id)
    if not row:
        raise HTTPException(404, "escalation not found")
    row.status = "acknowledged"
    row.acknowledged_by = by
    audit(session, actor, "ack_escalation", f"escalation:{escalation_id}")
    session.commit()
    return {"id": row.id, "status": row.status}


# ---------- routes: family ask list and follow-up (FR-45, FR-46) ----------
# Prototype auth: the admin token. Family logins (magic link per verified contact) come next.


@app.get("/seniors/{senior_id}/questions")
def list_questions(
    senior_id: int, session: Session = Depends(get_session), _: str = Depends(require_admin)
) -> list[FamilyQuestion]:
    return session.exec(
        select(FamilyQuestion).where(FamilyQuestion.senior_id == senior_id)
        .order_by(FamilyQuestion.created_at.desc())
    ).all()


@app.post("/seniors/{senior_id}/questions", status_code=201)
def add_question(
    senior_id: int,
    body: QuestionIn,
    session: Session = Depends(get_session),
    actor: str = Depends(require_admin),
) -> FamilyQuestion:
    _senior_or_404(session, senior_id)
    name = ""
    if body.added_by_contact_id is not None:
        contact = session.get(Contact, body.added_by_contact_id)
        if not contact or contact.senior_id != senior_id or contact.verified_at is None:
            raise HTTPException(400, "only verified contacts can add questions")
        name = f"{contact.relation} {contact.name}".strip() if contact.relation else contact.name
    row = FamilyQuestion(senior_id=senior_id, added_by_contact_id=body.added_by_contact_id,
                         added_by_name=name, text=body.text.strip(), kind=body.kind,
                         recurring=body.recurring)
    session.add(row)
    audit(session, actor, "add_question", f"senior:{senior_id}")
    session.commit()
    session.refresh(row)
    return row


@app.delete("/questions/{question_id}")
def cancel_question(
    question_id: int, session: Session = Depends(get_session), actor: str = Depends(require_admin)
) -> dict:
    row = session.get(FamilyQuestion, question_id)
    if not row:
        raise HTTPException(404, "question not found")
    row.status = "cancelled"
    audit(session, actor, "cancel_question", f"question:{question_id}")
    session.commit()
    return {"id": question_id, "status": "cancelled"}


@app.post("/internal/questions/{question_id}/answer")
def answer_question(
    question_id: int,
    body: AnswerIn,
    session: Session = Depends(get_session),
    _: str = Depends(require_service),
) -> dict:
    """The agent stores the answer as said. A recurring check-in stays open for the next call;
    its answer is kept as a separate answered row so the family sees the history."""
    row = session.get(FamilyQuestion, question_id)
    if not row or row.status != "open":
        raise HTTPException(404, "open question not found")
    now = utcnow()
    if row.recurring:
        row.answered_at = now
        answered = FamilyQuestion(
            senior_id=row.senior_id, added_by_contact_id=row.added_by_contact_id,
            added_by_name=row.added_by_name, text=row.text, kind=row.kind, recurring=False,
            status="answered", answer_text=body.answer.strip(), call_id=body.call_id, answered_at=now,
        )
        session.add(answered)
    else:
        row.status, row.answer_text = "answered", body.answer.strip()
        row.call_id, row.answered_at = body.call_id, now
    session.commit()
    return {"id": question_id, "status": "answered"}


@app.get("/seniors/{senior_id}/summaries")
def family_summaries(
    senior_id: int,
    limit: int = Query(default=30, le=100),
    session: Session = Depends(get_session),
    _: str = Depends(require_admin),
) -> list[dict]:
    """FR-46: what was said, per call, for family follow-up. Only shareable text, never private."""
    senior = _senior_or_404(session, senior_id)
    tz = ZoneInfo(senior.timezone)
    out = []
    for call in session.exec(
        select(Call).where(Call.senior_id == senior_id, Call.outcome == "answered")
        .order_by(Call.created_at.desc()).limit(limit)
    ).all():
        summary = session.get(CallSummary, call.id)
        answers = session.exec(
            select(FamilyQuestion).where(FamilyQuestion.call_id == call.id,
                                         FamilyQuestion.status == "answered")
        ).all()
        helps = session.exec(select(HelpRequest).where(HelpRequest.call_id == call.id)).all()
        when = _aware(call.started_at or call.created_at).astimezone(tz)
        out.append({
            "call_id": call.id,
            "when": when.isoformat(),
            "summary": summary.shareable_text if summary else "",
            "answers": [{"question": q.text, "answer": q.answer_text, "asked_by": q.added_by_name}
                        for q in answers],
            "help_requests": [{"id": h.id, "kind": h.kind, "details": h.details, "status": h.status}
                              for h in helps],
        })
    return out


# ---------- routes: proactive topics and local activities (FR-14, FR-15) ----------


@app.post("/seniors/{senior_id}/topics", status_code=201)
def add_topics(
    senior_id: int,
    items: list[TopicIn],
    session: Session = Depends(get_session),
    _: str = Depends(require_admin),
) -> dict:
    """Used by the topic fetcher (babbel-topics) and the operator. Duplicate URLs are skipped."""
    _senior_or_404(session, senior_id)
    added = 0
    for item in items:
        if item.url and session.exec(
            select(TopicItem).where(TopicItem.senior_id == senior_id, TopicItem.url == item.url)
        ).first():
            continue
        session.add(TopicItem(senior_id=senior_id, **item.model_dump()))
        added += 1
    session.commit()
    return {"added": added}


@app.get("/activities")
def list_activities(
    municipality: Optional[str] = None,
    session: Session = Depends(get_session),
    _: str = Depends(require_admin),
) -> list[LocalActivity]:
    query = select(LocalActivity).order_by(LocalActivity.starts_at)
    if municipality:
        query = query.where(LocalActivity.municipality == municipality)
    return session.exec(query).all()


@app.post("/activities", status_code=201)
def add_activity(
    body: ActivityIn, session: Session = Depends(get_session), actor: str = Depends(require_admin)
) -> LocalActivity:
    row = LocalActivity(**{**body.model_dump(), "tags": ", ".join(body.tags)})
    session.add(row)
    audit(session, actor, "add_activity", body.municipality)
    session.commit()
    session.refresh(row)
    return row


# ---------- routes: help requests (FR-16, D14) ----------


@app.post("/internal/help-requests", status_code=201)
def create_help_request(
    body: HelpRequestIn,
    session: Session = Depends(get_session),
    notifier: Notifier = Depends(get_notifier),
    _: str = Depends(require_service),
) -> dict:
    """The person said yes to help. The family hears first; the partner gets the request by SMS
    (D14 first version) when one is configured. No partner API until a partnership is signed."""
    senior = _senior_or_404(session, body.senior_id)
    s = get_settings()
    row = HelpRequest(senior_id=senior.id, kind=body.kind, details=body.details[:300],
                      call_id=body.call_id, partner=s.partner_name if s.partner_phone else "")
    session.add(row)
    session.flush()
    admin = admin_contact(session, senior)
    if admin:
        notifier.sms(admin.phone, text("nl-BE", "help_request_family", name=senior.preferred_name,
                                       kind=body.kind, details=body.details or "-",
                                       partner=s.partner_name))
    if s.partner_phone and admin:
        notifier.sms(s.partner_phone, text("nl-BE", "help_request_partner",
                                           name=senior.preferred_name,
                                           municipality=senior.municipality or "?",
                                           kind=body.kind, details=body.details or "-",
                                           admin_phone=admin.phone))
    audit(session, "agent", "help_request", f"senior:{senior.id}:{body.kind}")
    session.commit()
    return {"id": row.id, "status": row.status, "partner": row.partner}


@app.patch("/help-requests/{request_id}")
def update_help_request(
    request_id: int,
    body: HelpStatusIn,
    session: Session = Depends(get_session),
    actor: str = Depends(require_admin),
) -> HelpRequest:
    row = session.get(HelpRequest, request_id)
    if not row:
        raise HTTPException(404, "help request not found")
    row.status = body.status
    audit(session, actor, "help_request_status", f"help:{request_id}:{body.status}")
    session.commit()
    session.refresh(row)
    return row


# ---------- routes: admin view (FR-50) ----------


def dashboard_data(session: Session) -> dict:
    rows = []
    week_ago = utcnow() - timedelta(days=7)
    cost_rows: list[tuple[float, float]] = []
    for senior in session.exec(select(Senior).where(Senior.status != "deleted")).all():
        last = session.exec(
            select(Call).where(Call.senior_id == senior.id).order_by(Call.created_at.desc())
        ).first()
        open_flags = session.exec(
            select(Escalation).where(Escalation.senior_id == senior.id, Escalation.status == "open")
        ).all()
        nxt = next_slot_after(schedule_for(session, senior), utcnow())
        rows.append({
            "id": senior.id,
            "name": senior.display_name,
            "status": senior.status,
            "next_call": nxt.strftime("%a %d/%m %H:%M") if nxt else "-",
            "last_outcome": last.outcome if last else "-",
            "open_flags": [f"{f.rule_id} ({f.severity})" for f in open_flags],
        })
    for call in session.exec(select(Call).where(Call.ended_at.is_not(None))).all():
        created = call.created_at if call.created_at.tzinfo else call.created_at.replace(tzinfo=timezone.utc)
        if created >= week_ago and call.seconds > 0:
            cost_rows.append((call.total_cost, call.seconds))
    avg = costmod.weekly_average_per_minute(cost_rows)
    return {
        "seniors": rows,
        "weekly_cost_per_min": avg,
        "cost_alert": avg > get_settings().cost_alert_per_min,
    }


@app.get("/admin/dashboard")
def admin_dashboard(session: Session = Depends(get_session), _: str = Depends(require_admin)) -> dict:
    return dashboard_data(session)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(
    request: Request, session: Session = Depends(get_session), _: str = Depends(require_admin)
):
    return templates.TemplateResponse(request, "dashboard.html", {"data": dashboard_data(session)})


# ---------- routes: GDPR (FR-53) ----------


@app.get("/seniors/{senior_id}/export")
def export_senior(
    senior_id: int, session: Session = Depends(get_session), actor: str = Depends(require_admin)
) -> dict:
    senior = _senior_or_404(session, senior_id)
    def rows(model, field):
        return [r.model_dump(mode="json") for r in session.exec(select(model).where(field == senior_id)).all()]
    calls = rows(Call, Call.senior_id)
    call_ids = [c["id"] for c in calls]
    summaries = [
        s.model_dump(mode="json")
        for s in session.exec(select(CallSummary).where(CallSummary.call_id.in_(call_ids))).all()
    ] if call_ids else []
    audit(session, actor, "export", f"senior:{senior_id}")
    session.commit()
    return {
        "senior": senior.model_dump(mode="json"),
        "contacts": rows(Contact, Contact.senior_id),
        "schedule": rows(ScheduleSlot, ScheduleSlot.senior_id),
        "calls": calls,
        "summaries": summaries,
        "memory": rows(MemoryFact, MemoryFact.senior_id),
        "escalations": rows(Escalation, Escalation.senior_id),
        "consents": rows(Consent, Consent.senior_id),
        "family_questions": rows(FamilyQuestion, FamilyQuestion.senior_id),
        "help_requests": rows(HelpRequest, HelpRequest.senior_id),
        "topics": rows(TopicItem, TopicItem.senior_id),
    }


@app.delete("/seniors/{senior_id}")
def delete_senior(
    senior_id: int, session: Session = Depends(get_session), actor: str = Depends(require_admin)
) -> dict:
    senior = _senior_or_404(session, senior_id)
    call_ids = [c.id for c in session.exec(select(Call).where(Call.senior_id == senior_id)).all()]
    for model in (CallSummary, Feedback):
        if call_ids:
            for row in session.exec(select(model).where(model.call_id.in_(call_ids))).all():
                session.delete(row)
    for model in (MemoryFact, Contact, ScheduleSlot, Consent, FamilyQuestion, HelpRequest, TopicItem):
        for row in session.exec(select(model).where(model.senior_id == senior_id)).all():
            session.delete(row)
    for e in session.exec(select(Escalation).where(Escalation.senior_id == senior_id)).all():
        for step in session.exec(select(EscalationStepRow).where(EscalationStepRow.escalation_id == e.id)).all():
            session.delete(step)
        session.delete(e)
    for c in session.exec(select(Call).where(Call.senior_id == senior_id)).all():
        session.delete(c)
    senior.status = "deleted"
    senior.display_name = senior.preferred_name = "deleted"
    senior.phone = f"deleted-{senior_id}"
    senior.family = senior.interests = senior.routines = senior.avoid_topics = ""
    senior.municipality = ""
    audit(session, actor, "delete_senior", f"senior:{senior_id}")  # the deletion record stays
    session.commit()
    return {"deleted": senior_id}


def run() -> None:
    import uvicorn

    uvicorn.run("babbel.api.main:app", host="0.0.0.0", port=8000)


__all__ = ["app", "process_due_steps", "open_escalation"]
