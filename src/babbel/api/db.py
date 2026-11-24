"""Database tables (spec "Data model and API"). SQLite for development, Postgres in the pilot."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Session, SQLModel, create_engine

from babbel.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Senior(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    display_name: str
    preferred_name: str
    address_form: str = "u"  # u | je (D3)
    phone: str = Field(index=True, unique=True)
    timezone: str = "Europe/Brussels"
    quiet_start: str = "08:00"
    quiet_end: str = "20:00"
    max_call_minutes: int = 12
    municipality: str = ""  # for local activities (FR-15)
    status: str = "active"  # active | paused | deleted
    family: str = ""  # comma-separated (FR-10)
    interests: str = ""
    routines: str = ""
    avoid_topics: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class Contact(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    senior_id: int = Field(foreign_key="senior.id", index=True)
    name: str
    relation: str = ""
    phone: str
    role: str = "contact"  # admin | contact | care_partner
    priority: int = 100
    verified_at: Optional[datetime] = None
    verification_code: Optional[str] = None
    whatsapp_opt_in: bool = False
    quiet_start: str = "09:00"
    quiet_end: str = "21:00"
    opted_out: bool = False


class ScheduleSlot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    senior_id: int = Field(foreign_key="senior.id", index=True)
    weekday: int  # 0 = Monday
    time: str  # HH:MM


class Call(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("idempotency_key"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    senior_id: int = Field(foreign_key="senior.id", index=True)
    direction: str = "outbound"  # outbound | inbound | bridge
    idempotency_key: Optional[str] = None  # NFR-03
    slot_start: Optional[datetime] = None
    attempt: int = 1
    retry_of: Optional[int] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    outcome: Optional[str] = None  # answered | no_answer | busy | failed | not_now
    cost_telephony: float = 0.0
    cost_stt: float = 0.0
    cost_llm: float = 0.0
    cost_tts: float = 0.0
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def total_cost(self) -> float:
        return self.cost_telephony + self.cost_stt + self.cost_llm + self.cost_tts

    @property
    def seconds(self) -> float:
        if self.started_at and self.ended_at:
            return (self.ended_at - self.started_at).total_seconds()
        return 0.0


class CallSummary(SQLModel, table=True):
    call_id: int = Field(foreign_key="call.id", primary_key=True)
    text: str
    shareable_text: str = ""


class MemoryFact(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    senior_id: int = Field(foreign_key="senior.id", index=True)
    text: str
    category: str = "other"
    source_call_id: Optional[int] = None
    private: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    deleted_at: Optional[datetime] = None


class Escalation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    senior_id: int = Field(foreign_key="senior.id", index=True)
    call_id: Optional[int] = None
    rule_id: str
    severity: str
    trigger_text: str = ""
    status: str = "open"  # open | acknowledged | closed
    acknowledged_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class EscalationStepRow(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    escalation_id: int = Field(foreign_key="escalation.id", index=True)
    due_at: datetime
    to_name: str
    to_phone: str
    channels: str  # "call,sms" | "sms"
    message: str
    sent_at: Optional[datetime] = None
    skipped: bool = False


class Consent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    senior_id: int = Field(foreign_key="senior.id", index=True)
    kind: str  # calls | family_updates | escalation_to_care_partner | dialect_recording
    given_by: str
    evidence: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class Feedback(SQLModel, table=True):
    call_id: int = Field(foreign_key="call.id", primary_key=True)
    score: str  # good | okay | not_good


class FamilyQuestion(SQLModel, table=True):
    """FR-45: something the family wants Babbel to ask or check, answered as said."""

    id: Optional[int] = Field(default=None, primary_key=True)
    senior_id: int = Field(foreign_key="senior.id", index=True)
    added_by_contact_id: Optional[int] = None
    added_by_name: str = ""
    text: str
    kind: str = "question"  # question | check_in
    recurring: bool = False  # check-ins like "pillen genomen?" come back every call
    status: str = "open"  # open | answered | cancelled
    answer_text: str = ""
    call_id: Optional[int] = None
    answered_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)


class TopicItem(SQLModel, table=True):
    """FR-14: a fresh item that matches an interest, offered at most once."""

    id: Optional[int] = Field(default=None, primary_key=True)
    senior_id: int = Field(foreign_key="senior.id", index=True)
    interest: str
    title: str
    summary: str = ""
    source: str = ""
    url: str = Field(default="", index=True)
    published_at: Optional[datetime] = None
    offered_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)


class LocalActivity(SQLModel, table=True):
    """FR-15: something happening nearby, curated by the operator per municipality (D13)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    municipality: str = Field(index=True)
    title: str
    starts_at: datetime
    place: str = ""
    tags: str = ""  # comma-separated, matched against interests
    source: str = "operator"


class HelpRequest(SQLModel, table=True):
    """FR-16: the person wants to do something and needs company or help."""

    id: Optional[int] = Field(default=None, primary_key=True)
    senior_id: int = Field(foreign_key="senior.id", index=True)
    kind: str  # shopping | walk | outing | transport | other
    details: str = ""
    status: str = "requested"  # requested | confirmed | done | cancelled
    partner: str = ""
    call_id: Optional[int] = None
    created_at: datetime = Field(default_factory=utcnow)


class AuditEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    actor: str
    action: str
    target: str
    created_at: datetime = Field(default_factory=utcnow)


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        url = get_settings().database_url
        kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        _engine = create_engine(url, **kwargs)
        SQLModel.metadata.create_all(_engine)
    return _engine


def reset_engine(url: str) -> None:
    """Tests use this to point at a fresh database."""
    global _engine
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    _engine = create_engine(url, **kwargs)
    SQLModel.metadata.create_all(_engine)


def get_session():
    with Session(get_engine()) as session:
        yield session
