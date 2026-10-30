"""Deterministic should-act policy. All self-directed delivery is in-app in Milestone 1."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .commitments import OPEN, available, next_allowed, plan_next, quiet, utc
from .models import (
    Commitment,
    DailyBriefing,
    ProvenanceLink,
    ReflectionPrompt,
    Reminder,
    ScheduledAction,
    User,
)
from .persistence import append_message, get_or_create_conversation
from .prospective import urgency_for

DAILY_CHECKIN_LIMIT = 3


def finish(action: ScheduledAction, state: str, reason: str, now: datetime, **details: Any) -> dict:
    action.state = state
    action.reason = reason
    action.decision = {"delivery": "in_app" if state == "succeeded" else "none", **details}
    action.completed_at = now
    action.lease_token = None
    action.lease_until = None
    return {"state": state, "reason": reason}


def perform(
    db: Session, user_id: str, event_id: str, action_id: str, now: datetime, morning, evening
) -> dict:
    action = db.scalar(
        select(ScheduledAction)
        .where(
            ScheduledAction.id == action_id,
            ScheduledAction.user_id == user_id,
        )
        .with_for_update()
    )
    user = db.get(User, user_id)
    if action is None or user is None or action.state != "executing":
        return {"state": "skipped", "reason": "not_claimed"}
    action.source_event_id = event_id
    if action.reminder_plan_id:
        from .reminders import perform_plan

        return perform_plan(db, user, action, event_id, now, finish)
    local_day = utc(now).astimezone(ZoneInfo(user.timezone)).date()
    if action.preference_version != user.autonomy_version:
        return finish(action, "skipped", "preferences_changed", now)
    if action.kind not in {"commitment_followup", "morning_briefing", "evening_reflection"}:
        return finish(action, "skipped", "external_action_not_authorized", now)
    if utc(action.due_at) > utc(now):
        return finish(action, "skipped", "not_due", now)
    if action.kind != "commitment_followup":
        enabled = (
            user.morning_enabled if action.kind == "morning_briefing" else user.evening_enabled
        )
        if not enabled:
            return finish(action, "skipped", "routine_disabled", now)
        if action.local_date != local_day:
            return finish(action, "skipped", "routine_day_expired", now)
        if quiet(user, now):
            later = next_allowed(user, {}, now + timedelta(minutes=15))
            if later and later.astimezone(ZoneInfo(user.timezone)).date() == local_day:
                action.state = "pending"
                action.retry_at = later
                action.reason = "quiet_hours"
                action.attempt_count = max(0, action.attempt_count - 1)
                action.lease_token = None
                action.lease_until = None
                return {"state": "deferred", "reason": "quiet_hours"}
            return finish(action, "skipped", "quiet_hours", now)
        record: DailyBriefing | ReflectionPrompt | None
        if action.kind == "morning_briefing":
            changed = morning.run(db, user_id, event_id, now, intended_local_date=local_day)
            record = db.scalar(
                select(DailyBriefing).where(
                    DailyBriefing.user_id == user_id,
                    DailyBriefing.local_date == local_day,
                )
            )
        else:
            changed = evening.start(db, user_id, event_id, now, intended_local_date=local_day)
            record = db.scalar(
                select(ReflectionPrompt).where(
                    ReflectionPrompt.user_id == user_id,
                    ReflectionPrompt.local_date == local_day,
                )
            )
        if changed and record:
            action.message_id = record.message_id
            return finish(
                action, "succeeded", "routine_saved_in_app", now, local_date=str(local_day)
            )
        return finish(action, "skipped", "routine_already_generated", now)

    item = db.scalar(
        select(Commitment)
        .where(
            Commitment.id == action.commitment_id,
            Commitment.user_id == user_id,
        )
        .with_for_update()
    )
    if item is None or item.deleted:
        return finish(action, "skipped", "commitment_deleted", now)
    if item.version != action.subject_version:
        return finish(action, "skipped", "superseded_plan", now)
    if not user.checkins_enabled or not item.followup_enabled:
        return finish(action, "skipped", "checkins_disabled", now)
    if item.state not in OPEN:
        return finish(action, "skipped", "commitment_not_active", now)
    if item.ambiguity != "none":
        item.next_relevant_moment = None
        return finish(action, "skipped", "deadline_needs_clarification", now)
    if item.followup_count >= item.max_followups:
        item.next_relevant_moment = None
        return finish(action, "skipped", "followup_limit", now)
    if quiet(user, now) or not available(user, item.availability or {}, now):
        item.next_relevant_moment = next_allowed(
            user, item.availability or {}, now + timedelta(minutes=15)
        )
        return finish(
            action,
            "skipped",
            "quiet_hours_or_availability",
            now,
            next_check_at=item.next_relevant_moment.isoformat()
            if item.next_relevant_moment
            else None,
        )
    zone = ZoneInfo(user.timezone)
    day_start = datetime.combine(local_day, time.min, zone)
    day_end = datetime.combine(local_day + timedelta(days=1), time.min, zone)
    used = (
        db.scalar(
            select(func.count())
            .select_from(ScheduledAction)
            .where(
                ScheduledAction.user_id == user_id,
                ScheduledAction.kind == "commitment_followup",
                ScheduledAction.state == "succeeded",
                ScheduledAction.completed_at >= utc(day_start),
                ScheduledAction.completed_at < utc(day_end),
            )
        )
        or 0
    )
    if used >= DAILY_CHECKIN_LIMIT:
        item.next_relevant_moment = next_allowed(user, item.availability or {}, day_end)
        return finish(action, "skipped", "daily_attention_limit", now)
    urgency = urgency_for(item, now)
    due = utc(item.due_at).astimezone(zone).isoformat() if item.due_at else "not specified"
    bound = "before" if item.due_bound == "exclusive" else "by"
    content = f"A check-in you enabled for “{item.title}”: it's still marked {item.state}."
    if item.due_at:
        content += f" Deadline: {bound} {due}."
    if item.next_action:
        content += f" Next action: {item.next_action}."
    if item.purpose:
        content += f" Why it matters: {item.purpose}."
    if item.dependencies:
        labels = [
            entry.get("label", "") for entry in item.dependencies if entry.get("kind") == "blocked"
        ]
        if labels:
            content += " Recorded blocker: " + "; ".join(labels) + "."
    content += " How is it going? You can update the plan, pause check-ins, or mark it complete."
    conversation = get_or_create_conversation(db, user_id, created_at=now)
    message = append_message(
        db,
        user_id=user_id,
        conversation_id=conversation.id,
        role="assistant",
        actor="assistant",
        content=content,
        created_at=now,
    )
    action.message_id = message.id
    item.last_surfaced_at = now
    item.followup_count += 1
    item.next_relevant_moment = plan_next(user, item, now)
    db.add(
        Reminder(
            user_id=user_id,
            commitment_id=item.id,
            thread_id=item.related_thread_id,
            policy_version="autonomy-v1",
            relevance_window=action.id,
            reason_code="owner_enabled_checkin",
            reason_metadata={
                "urgency": urgency.factors,
                "plan_version": item.version,
                "scheduled_action_id": action.id,
            },
            state="surfaced",
            attempt_count=1,
            surfaced_message_id=message.id,
            source_event_id=event_id,
            created_at=now,
        )
    )
    db.add(
        ProvenanceLink(
            user_id=user_id,
            target_id=message.id,
            source_type="agent_event",
            source_id=event_id,
            confidence=1.0,
            derivation="verified-in-app-delivery",
            metadata_only=True,
        )
    )
    return finish(
        action,
        "succeeded",
        "checkin_saved_in_app",
        now,
        urgency=urgency.factors,
        commitment_id=item.id,
        plan_version=item.version,
        next_check_at=item.next_relevant_moment.isoformat() if item.next_relevant_moment else None,
    )
