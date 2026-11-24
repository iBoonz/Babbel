"""Scheduler runner: places due calls and sends due escalation steps.

Every 30 seconds:
1. compute due calls (FR-01, FR-02, FR-04) and insert them with a unique idempotency key
   (NFR-03), then dispatch the LiveKit agent into a fresh room with the call metadata
2. send escalation steps whose time has come, unless someone acknowledged (FR-30 to FR-37)

Without LiveKit credentials it runs in dry-run mode and only logs the dispatches.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from babbel.api.db import Call, Senior, get_engine
from babbel.api.main import process_due_steps, schedule_for
from babbel.config import get_settings
from babbel.core.notify import make_notifier
from babbel.core.scheduler import CallRecord, due_calls

log = logging.getLogger("babbel.scheduler")


async def dispatch_agent(call: Call, senior: Senior) -> None:
    s = get_settings()
    metadata = {"call_id": call.id, "senior_id": senior.id, "phone": senior.phone, "direction": "outbound"}
    if not (s.livekit_url and s.livekit_api_key and s.livekit_api_secret):
        log.warning("DRY RUN dispatch: %s", metadata)
        return
    from livekit import api

    lk = api.LiveKitAPI(s.livekit_url, s.livekit_api_key, s.livekit_api_secret)
    try:
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=s.agent_name, room=f"call-{call.id}", metadata=json.dumps(metadata)
            )
        )
    finally:
        await lk.aclose()


async def tick(now: datetime | None = None) -> list[int]:
    s = get_settings()
    now = now or datetime.now(timezone.utc)
    placed: list[int] = []
    with Session(get_engine()) as session:
        seniors = session.exec(select(Senior).where(Senior.status == "active")).all()
        schedules = [schedule_for(session, sn) for sn in seniors]
        existing = [
            CallRecord(c.senior_id, c.idempotency_key, c.slot_start, c.attempt, c.outcome)
            for c in session.exec(select(Call).where(Call.idempotency_key.is_not(None))).all()
        ]
        for due in due_calls(
            schedules, existing, now,
            window_minutes=s.slot_window_minutes, retry_after_minutes=s.retry_after_minutes,
        ):
            retry_of = None
            if due.attempt == 2:
                first_key = due.idempotency_key.rsplit(":", 1)[0] + ":1"
                first = session.exec(select(Call).where(Call.idempotency_key == first_key)).first()
                retry_of = first.id if first else None
            call = Call(
                senior_id=due.senior_id, direction="outbound", idempotency_key=due.idempotency_key,
                slot_start=due.slot_start, attempt=due.attempt, retry_of=retry_of,
            )
            session.add(call)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()  # another runner placed it first: never a double call
                continue
            session.refresh(call)
            await dispatch_agent(call, session.get(Senior, due.senior_id))
            placed.append(call.id)
        process_due_steps(session, make_notifier(s.notifier, s.sms_webhook_url), now)
    return placed


async def loop(interval_seconds: int = 30) -> None:
    while True:
        try:
            placed = await tick()
            if placed:
                log.info("placed calls %s", placed)
        except Exception:  # keep the loop alive; errors are logged
            log.exception("scheduler tick failed")
        await asyncio.sleep(interval_seconds)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(loop())


if __name__ == "__main__":
    main()
