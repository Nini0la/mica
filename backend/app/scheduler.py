"""Durable wake-ups, short claims, and atomic in-app effects. No LLM polling loop."""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from datetime import time as wall_time
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .agent import AgentRuntime, RuntimeEvent
from .clock import FakeClock, SystemClock
from .commitments import OPEN, next_allowed, plan_next, utc
from .models import Commitment, ReminderPlan, ScheduledAction, User, uid
from .responsibilities import lock_user

LEASE_SECONDS = 60


def prepare(db: Session, user: User, now: datetime) -> int:
    user.autonomy_heartbeat_at = now
    count = 0

    def add(
        key: str,
        kind: str,
        due: datetime,
        *,
        item: Commitment | None = None,
        plan: ReminderPlan | None = None,
        day=None,
        state="pending",
        reason="due",
    ) -> None:
        nonlocal count
        if db.scalar(
            select(ScheduledAction.id).where(
                ScheduledAction.user_id == user.id,
                ScheduledAction.action_key == key,
            )
        ):
            return
        db.add(
            ScheduledAction(
                user_id=user.id,
                action_key=key,
                kind=kind,
                commitment_id=item.id if item else None,
                reminder_plan_id=plan.id if plan else None,
                subject_version=item.version if item else plan.version if plan else 0,
                preference_version=user.autonomy_version,
                local_date=day,
                due_at=due,
                state=state,
                reason=reason,
                created_at=now,
            )
        )
        db.flush()
        count += 1

    if user.checkins_enabled:
        items = db.scalars(
            select(Commitment)
            .where(
                Commitment.user_id == user.id,
                Commitment.deleted.is_(False),
                Commitment.followup_enabled.is_(True),
                Commitment.state.in_(OPEN),
            )
            .order_by(Commitment.next_relevant_moment.asc().nullsfirst(), Commitment.id)
            .limit(1000)
        ).all()
        for item in items:
            if item.next_relevant_moment is None:
                item.next_relevant_moment = plan_next(user, item, now)
            due = item.next_relevant_moment
            if due is None:
                add(
                    f"unavailable:{item.id}:{item.version}:{user.autonomy_version}",
                    "commitment_followup",
                    now,
                    item=item,
                    state="skipped",
                    reason="no_permitted_next_check",
                )
            elif utc(due) <= utc(now):
                key = (
                    f"commitment:{item.id}:{item.version}:"
                    f"{utc(due).isoformat()}:{user.autonomy_version}"
                )
                add(key, "commitment_followup", utc(due), item=item)

    for plan in db.scalars(
        select(ReminderPlan)
        .where(
            ReminderPlan.user_id == user.id,
            ReminderPlan.state == "active",
            ReminderPlan.next_due_at <= now,
        )
        .order_by(ReminderPlan.next_due_at, ReminderPlan.id)
        .limit(100)
    ):
        assert plan.next_due_at is not None
        add(
            f"reminder:{plan.id}:{plan.version}:{utc(plan.next_due_at).isoformat()}:{user.autonomy_version}",
            "reminder_once" if plan.mode == "once" else "reminder_checkin",
            utc(plan.next_due_at),
            plan=plan,
        )

    zone = ZoneInfo(user.timezone)
    today = utc(now).astimezone(zone).date()
    for kind, enabled, configured in (
        ("morning_briefing", user.morning_enabled, user.morning_time),
        ("evening_reflection", user.evening_enabled, user.evening_time),
    ):
        if not enabled:
            continue
        hour, minute = map(int, configured.split(":"))
        local = datetime.combine(today, wall_time(hour, minute), zone)
        due = utc(local)
        key = f"routine:{kind}:{today}:{user.autonomy_version}"
        if due.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            add(key, kind, due, day=today, state="skipped", reason="nonexistent_local_time")
            continue
        if user.autonomy_updated_at and due < utc(user.autonomy_updated_at):
            continue  # opt-in starts at the next requested occurrence, not a backlog
        effective = next_allowed(user, {}, due)
        if effective is None or effective.astimezone(zone).date() != today:
            add(key, kind, due, day=today, state="skipped", reason="quiet_hours")
        elif utc(now) >= effective:
            add(key, kind, effective, day=today)
    return count


def claim(db: Session, now: datetime) -> tuple[str, str, str] | None:
    row = db.scalar(
        select(ScheduledAction)
        .where(
            or_(
                and_(
                    ScheduledAction.state.in_(["pending", "retryable"]),
                    or_(ScheduledAction.retry_at.is_(None), ScheduledAction.retry_at <= now),
                ),
                and_(ScheduledAction.state == "executing", ScheduledAction.lease_until <= now),
            ),
            ScheduledAction.due_at <= now,
        )
        .order_by(ScheduledAction.due_at, ScheduledAction.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        return None
    if row.attempt_count >= 3:
        row.state = "failed"
        row.reason = "attempt_limit"
        row.completed_at = now
        row.lease_token = None
        row.lease_until = None
        return None
    row.state = "executing"
    row.attempt_count += 1
    row.lease_token = uid()
    row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
    row.retry_at = None
    return row.id, row.user_id, row.lease_token


def tick(
    sessions,
    runtime: AgentRuntime,
    *,
    fault: Callable[[str, str], None] | None = None,
    max_actions: int = 100,
) -> dict[str, int]:
    prepared = executed = failures = 0
    with sessions() as db:
        owners = list(db.scalars(select(User.id).order_by(User.id)))
    for owner in owners:
        with sessions.begin() as db:
            lock_user(db, owner)
            user = db.get(User, owner)
            assert user is not None
            prepared += prepare(db, user, runtime.clock.now())
    if fault:
        fault("after_prepare", "")
    for _ in range(max_actions):
        now = runtime.clock.now()
        with sessions.begin() as db:
            selected = claim(db, now)
        if selected is None:
            break
        identifier, owner, token = selected
        if fault:
            fault("after_claim", identifier)
        try:
            with sessions.begin() as db:
                lock_user(db, owner)
                action = db.scalar(
                    select(ScheduledAction)
                    .where(
                        ScheduledAction.id == identifier,
                        ScheduledAction.user_id == owner,
                    )
                    .with_for_update()
                )
                if action is None or action.state != "executing" or action.lease_token != token:
                    continue
                runtime.handle(
                    db,
                    owner,
                    RuntimeEvent(
                        external_id="autonomy:" + identifier + ":" + token,
                        kind="autonomy_wake",
                        scheduled_action_id=identifier,
                    ),
                    commit=False,
                )
                if action.state == "executing":
                    action.state = "failed"
                    action.reason = "inconsistent_prior_event"
                if fault:
                    fault("before_effect_commit", identifier)
            executed += 1
            if fault:
                fault("after_effect_commit", identifier)
        except Exception:
            failures += 1
            with sessions.begin() as db:
                action = db.scalar(
                    select(ScheduledAction)
                    .where(
                        ScheduledAction.id == identifier,
                        ScheduledAction.lease_token == token,
                        ScheduledAction.state == "executing",
                    )
                    .with_for_update()
                )
                if action is not None:
                    action.state = "retryable" if action.attempt_count < 3 else "failed"
                    action.reason = "execution_failed"
                    if action.state == "failed":
                        action.completed_at = runtime.clock.now()
                    action.retry_at = runtime.clock.now() + timedelta(
                        seconds=30 * 2**action.attempt_count
                    )
                    action.lease_token = None
                    action.lease_until = None
    return {"prepared": prepared, "executed": executed, "failures": failures}


def main() -> None:
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--personal-only", action="store_true", help="Never invoke an external channel"
    )
    parser.add_argument("--at", help="Injected UTC/offset clock; APP_ENV=test and --once only")
    options = parser.parse_args()
    if options.at and (os.getenv("APP_ENV") != "test" or not options.once):
        raise SystemExit("--at is restricted to isolated test one-shot workers")
    from .main import SessionLocal

    clock = FakeClock(datetime.fromisoformat(options.at)) if options.at else SystemClock()
    if clock.now().tzinfo is None:
        raise SystemExit("Worker clock requires an offset")
    runtime = AgentRuntime(clock=clock)
    while True:
        try:
            result = tick(SessionLocal, runtime)
            if not options.personal_only:
                from .channels import BridgeChannel
                from .operations_worker import tick as responsibilities_tick

                channel = BridgeChannel.configured()
                if channel:
                    responsibilities_tick(
                        SessionLocal, channel, clock.now(), channel.account_id, clock=clock.now
                    )
            if options.once:
                import json

                print(json.dumps(result))
                raise SystemExit(int(result["failures"] > 0))
        except Exception:
            print("Autonomy worker iteration failed; persisted work will be retried.", flush=True)
            if options.once:
                raise SystemExit(1) from None
        time.sleep(10)


if __name__ == "__main__":
    main()
