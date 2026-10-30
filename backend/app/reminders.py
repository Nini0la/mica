"""Deterministic one-time/reminder-plan execution through the existing action ledger."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .commitments import available, quiet, utc
from .continuation import policy
from .models import ProvenanceLink, ReminderPlan, ScheduledAction
from .persistence import append_message, get_or_create_conversation


def next_permitted_time(plan, user, candidate):
    candidate = utc(candidate)
    for _ in range(8 * 24 * 60):
        if (not plan.respect_quiet_hours or not quiet(user, candidate)) and available(
            user, plan.availability or {}, candidate
        ):
            return candidate
        candidate += timedelta(minutes=1)
    return None


def perform_plan(db, user, action, event_id, now, finish):
    plan = db.scalar(
        select(ReminderPlan)
        .where(ReminderPlan.id == action.reminder_plan_id, ReminderPlan.user_id == user.id)
        .with_for_update()
    )
    if not plan or plan.state != "active" or plan.version != action.subject_version:
        return finish(action, "skipped", "reminder_changed", now)
    if action.kind != ("reminder_once" if plan.mode == "once" else "reminder_checkin"):
        return finish(action, "skipped", "invalid_reminder_scope", now)
    if utc(action.due_at) > utc(now):
        return finish(action, "skipped", "not_due", now)
    if plan.mode == "recurring" and not user.checkins_enabled:
        return finish(action, "skipped", "checkins_disabled", now)
    if plan.occurrence_count >= plan.max_occurrences:
        plan.state = "completed"
        plan.next_due_at = None
        return finish(action, "skipped", "occurrence_limit", now)
    permitted = next_permitted_time(plan, user, now)
    if permitted is None or permitted > utc(now):
        plan.next_due_at = permitted
        if permitted is None:
            plan.state = "paused"
        return finish(
            action,
            "skipped",
            "quiet_hours_or_availability",
            now,
            next_check_at=permitted.isoformat() if permitted else None,
        )
    zone = ZoneInfo(user.timezone)
    day = utc(now).astimezone(zone).date()
    day_start = datetime.combine(day, time.min, zone)
    recent = list(
        db.scalars(
            select(ScheduledAction.completed_at)
            .where(
                ScheduledAction.user_id == user.id,
                ScheduledAction.reminder_plan_id.is_not(None),
                ScheduledAction.state == "succeeded",
                ScheduledAction.completed_at >= utc(day_start),
            )
            .order_by(ScheduledAction.completed_at)
        ).all()
    )
    hour = list(
        db.scalars(
            select(ScheduledAction.completed_at)
            .where(
                ScheduledAction.user_id == user.id,
                ScheduledAction.reminder_plan_id.is_not(None),
                ScheduledAction.state == "succeeded",
                ScheduledAction.completed_at >= utc(now) - timedelta(hours=1),
            )
            .order_by(ScheduledAction.completed_at)
        ).all()
    )
    if len(recent) >= policy()["messages_per_day"] or len(hour) >= policy()["messages_per_hour"]:
        candidate = (
            datetime.combine(day + timedelta(days=1), time.min, zone)
            if len(recent) >= policy()["messages_per_day"]
            else utc(hour[0]) + timedelta(hours=1, seconds=1)
        )
        plan.next_due_at = next_permitted_time(plan, user, candidate)
        return finish(
            action,
            "skipped",
            "reminder_rate_limit",
            now,
            next_check_at=plan.next_due_at.isoformat() if plan.next_due_at else None,
        )
    conversation = get_or_create_conversation(db, user.id, created_at=now)
    content = (
        f"Reminder: {plan.topic}."
        if plan.mode == "once"
        else f"A check-in you requested: {plan.topic}. How is it going?"
    )
    message = append_message(
        db,
        user_id=user.id,
        conversation_id=conversation.id,
        role="assistant",
        actor="assistant",
        content=content,
        created_at=now,
    )
    action.message_id = message.id
    plan.occurrence_count += 1
    plan.updated_at = now
    if plan.mode == "once" or plan.occurrence_count >= plan.max_occurrences:
        plan.state = "completed"
        plan.next_due_at = None
    else:
        plan.next_due_at = next_permitted_time(
            plan, user, utc(now) + timedelta(seconds=plan.interval_seconds)
        )
    db.add(
        ProvenanceLink(
            user_id=user.id,
            target_id=message.id,
            source_type="agent_event",
            source_id=event_id,
            confidence=1.0,
            derivation="verified-personal-reminder",
            metadata_only=True,
        )
    )
    return finish(
        action,
        "succeeded",
        "reminder_saved_in_app",
        now,
        reminder_plan_id=plan.id,
        mode=plan.mode,
        occurrence=plan.occurrence_count,
        requested_due_at=utc(plan.due_at).isoformat(),
        next_check_at=plan.next_due_at.isoformat() if plan.next_due_at else None,
    )
