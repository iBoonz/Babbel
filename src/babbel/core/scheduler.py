"""Call scheduling (spec FR-01, FR-02, FR-04, FR-34; NFR-03 idempotency).

Pure functions: give them the schedule, existing calls and "now", get back the calls
that are due. The scheduler runner persists them with a unique idempotency key, so a
crash or a second runner can never place a double call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

MISSED_OUTCOMES = {"no_answer", "busy", "failed"}


@dataclass(frozen=True)
class Slot:
    weekday: int  # 0 = Monday
    at: time


@dataclass(frozen=True)
class SeniorSchedule:
    senior_id: int
    timezone: str
    slots: list[Slot]
    quiet_start: time = time(8, 0)
    quiet_end: time = time(20, 0)
    active: bool = True


@dataclass(frozen=True)
class CallRecord:
    senior_id: int
    idempotency_key: str
    slot_start: datetime  # timezone-aware
    attempt: int
    outcome: str | None  # None while in progress


@dataclass(frozen=True)
class DueCall:
    senior_id: int
    slot_start: datetime
    attempt: int
    idempotency_key: str = field(init=False)

    def __post_init__(self) -> None:
        key = f"{self.senior_id}:{self.slot_start.isoformat()}:{self.attempt}"
        object.__setattr__(self, "idempotency_key", key)


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def within_quiet_hours(local: datetime, start: time, end: time) -> bool:
    """True when calling is ALLOWED (FR-04): start <= local time < end."""
    return start <= local.time() < end


def due_calls(
    schedules: list[SeniorSchedule],
    calls: list[CallRecord],
    now: datetime,
    *,
    window_minutes: int = 10,
    retry_after_minutes: int = 15,
) -> list[DueCall]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    by_key = {c.idempotency_key: c for c in calls}
    out: list[DueCall] = []
    window = timedelta(minutes=window_minutes)
    retry_delay = timedelta(minutes=retry_after_minutes)

    for sched in schedules:
        if not sched.active:
            continue
        tz = ZoneInfo(sched.timezone)
        local_now = now.astimezone(tz)
        for slot in sched.slots:
            if slot.weekday != local_now.weekday():
                continue
            slot_start = datetime.combine(local_now.date(), slot.at, tzinfo=tz)

            # FR-01: first attempt inside the slot window
            first = DueCall(sched.senior_id, slot_start, 1)
            if slot_start <= local_now < slot_start + window:
                if first.idempotency_key not in by_key and within_quiet_hours(
                    local_now, sched.quiet_start, sched.quiet_end
                ):
                    out.append(first)
                continue

            # FR-02: exactly one retry, 15 minutes later, if the first was missed
            prev = by_key.get(first.idempotency_key)
            retry = DueCall(sched.senior_id, slot_start, 2)
            retry_start = slot_start + retry_delay
            if (
                prev is not None
                and prev.outcome in MISSED_OUTCOMES
                and retry_start <= local_now < retry_start + window
                and retry.idempotency_key not in by_key
                and within_quiet_hours(local_now, sched.quiet_start, sched.quiet_end)
            ):
                out.append(retry)
    return out


def consecutive_missed_slots(calls: list[CallRecord], senior_id: int) -> int:
    """Number of most recent slots where every attempt was missed (input to FR-34)."""
    slots: dict[datetime, list[CallRecord]] = {}
    for c in calls:
        if c.senior_id == senior_id and c.outcome is not None:
            slots.setdefault(c.slot_start, []).append(c)
    count = 0
    for slot_start in sorted(slots, reverse=True):
        attempts = slots[slot_start]
        if all(a.outcome in MISSED_OUTCOMES for a in attempts):
            count += 1
        else:
            break
    return count


def missed_call_action(missed: int) -> str:
    """FR-34: two missed slots in a row -> SMS family admin; three or more -> call."""
    if missed >= 3:
        return "call"
    if missed == 2:
        return "sms"
    return "none"


def next_slot_after(sched: SeniorSchedule, after: datetime) -> datetime | None:
    """Next scheduled slot strictly after `after`, used for 'I call you back on ...'."""
    if not sched.slots:
        return None
    tz = ZoneInfo(sched.timezone)
    local = after.astimezone(tz)
    for day_offset in range(0, 8):
        day = local.date() + timedelta(days=day_offset)
        candidates = sorted(
            datetime.combine(day, s.at, tzinfo=tz) for s in sched.slots if s.weekday == day.weekday()
        )
        for cand in candidates:
            if cand > local:
                return cand
    return None
