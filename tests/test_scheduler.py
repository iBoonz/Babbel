from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from babbel.core.scheduler import (
    CallRecord,
    SeniorSchedule,
    Slot,
    consecutive_missed_slots,
    due_calls,
    missed_call_action,
    next_slot_after,
    within_quiet_hours,
)

TZ = ZoneInfo("Europe/Brussels")
TUE = datetime(2026, 11, 24, 10, 32, tzinfo=TZ)  # a Tuesday
DAILY = SeniorSchedule(1, "Europe/Brussels", [Slot(d, time(10, 30)) for d in range(7)],
                       quiet_start=time(8, 0))


def test_first_attempt_in_window():
    due = due_calls([DAILY], [], TUE)
    assert len(due) == 1 and due[0].attempt == 1
    assert due[0].idempotency_key.startswith("1:2026-11-24T10:30")


def test_no_double_call():
    first = due_calls([DAILY], [], TUE)[0]
    rec = CallRecord(1, first.idempotency_key, first.slot_start, 1, None)
    assert due_calls([DAILY], [rec], TUE) == []


def test_one_retry_after_missed():
    first = due_calls([DAILY], [], TUE)[0]
    missed = CallRecord(1, first.idempotency_key, first.slot_start, 1, "no_answer")
    later = TUE + timedelta(minutes=14)
    retry = due_calls([DAILY], [missed], later)
    assert [d.attempt for d in retry] == [2]
    answered = CallRecord(1, first.idempotency_key, first.slot_start, 1, "answered")
    assert due_calls([DAILY], [answered], later) == []


def test_quiet_hours_from_eight():
    assert within_quiet_hours(datetime(2026, 11, 24, 8, 0, tzinfo=TZ), time(8, 0), time(20, 0))
    assert not within_quiet_hours(datetime(2026, 11, 24, 7, 59, tzinfo=TZ), time(8, 0), time(20, 0))
    early = SeniorSchedule(1, "Europe/Brussels", [Slot(1, time(7, 30))], quiet_start=time(8, 0))
    assert due_calls([early], [], datetime(2026, 11, 24, 7, 31, tzinfo=TZ)) == []


def test_paused_person_not_called():
    paused = SeniorSchedule(1, "Europe/Brussels", DAILY.slots, active=False)
    assert due_calls([paused], [], TUE) == []


def test_missed_call_escalation_levels():
    base = datetime(2026, 11, 20, 10, 30, tzinfo=TZ)
    recs = [CallRecord(1, f"k{i}", base + timedelta(days=i), 1, "no_answer") for i in range(3)]
    assert consecutive_missed_slots(recs, 1) == 3
    assert missed_call_action(1) == "none"
    assert missed_call_action(2) == "sms"
    assert missed_call_action(3) == "call"


def test_next_slot():
    nxt = next_slot_after(DAILY, TUE)
    assert nxt is not None and nxt.astimezone(TZ).date() == datetime(2026, 11, 25).date()
