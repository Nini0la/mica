"""Durable proposals and semantic workflow operations. Azure selects; these functions validate."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import case, func, select, update

from .commitments import utc
from .models import (
    Commitment,
    ConversationProposal,
    JournalEntry,
    Message,
    ProvenanceLink,
    ReflectionPrompt,
    ReminderPlan,
    ScheduledAction,
    TelegramConnection,
    TelegramDelivery,
    User,
    VocabularyAttempt,
    VocabularyItem,
)
from .temporal import When, resolve_when

PROPOSABLE = {
    "update_reminder",
    "schedule_reminder",
    "create_commitment",
    "update_commitment",
    "save_project",
    "save_idea",
    "update_thread",
    "manage_workflow",
}


def policy():
    return {
        "minimum_once_seconds": 1,
        "minimum_checkin_seconds": max(1, int(os.getenv("MICA_MIN_CHECKIN_SECONDS", "60"))),
        "messages_per_hour": max(1, int(os.getenv("MICA_REMINDERS_PER_HOUR", "12"))),
        "messages_per_day": max(1, int(os.getenv("MICA_REMINDERS_PER_DAY", "50"))),
        "default_checkin_count": 5,
        "maximum_occurrences": 100,
    }


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ScheduleReminder(Input):
    topic: str = Field(min_length=1, max_length=500)
    mode: Literal["once", "recurring"] = "once"
    when: When | None = None
    interval_seconds: int | None = Field(default=None, ge=1, le=604800)
    max_occurrences: int = Field(default=5, ge=1, le=100)
    respect_quiet_hours: bool | None = None
    blocked_periods: list[Literal["morning", "afternoon", "evening", "night"]] = Field(
        default_factory=list, max_length=4
    )
    availability_until: date | None = None


class ChangeReminder(Input):
    id: str | None = None
    reference: str | None = Field(default=None, max_length=500)
    version: int | None = Field(default=None, ge=1)
    action: Literal["update", "pause", "resume", "cancel", "complete"]
    when: When | None = None
    interval_seconds: int | None = Field(default=None, ge=1, le=604800)
    topic: str | None = Field(default=None, min_length=1, max_length=500)
    blocked_periods: list[Literal["morning", "afternoon", "evening", "night"]] | None = Field(
        default=None, max_length=4
    )
    availability_until: date | None = None
    respect_quiet_hours: bool | None = None


class ProposeAction(Input):
    tool_name: str
    title: str = Field(min_length=1, max_length=500)
    arguments: dict[str, Any]


class ResolveProposal(Input):
    id: str | None = None
    reference: str | None = Field(default=None, max_length=500)
    version: int | None = Field(default=None, ge=1)
    action: Literal["execute", "amend", "pause", "resume", "cancel"]
    changes: dict[str, Any] = Field(default_factory=dict)


class ManageWorkflow(Input):
    kind: Literal["reflection", "vocabulary"]
    id: str | None = None
    version: int | None = Field(default=None, ge=1)
    action: Literal["start", "respond", "correct", "pause", "resume", "cancel"]
    word: str | None = None
    source_message_ids: list[str] = Field(default_factory=list, max_length=10)
    summary: str | None = Field(default=None, max_length=1500)
    grade: Literal["correct", "incorrect", "uncertain"] | None = None
    sentence: str | None = Field(default=None, max_length=500)


def reject(reason, question):
    return {"status": "rejected", "reason": reason, "question": question}


def message_time(db, source_id, fallback):
    message = db.get(Message, source_id)
    return utc(message.created_at) if message else utc(fallback)


def evidence(db, owner, target, source, derivation):
    db.add(
        ProvenanceLink(
            user_id=owner,
            target_id=target,
            source_type="message" if db.get(Message, source) else "owner_control",
            source_id=source,
            confidence=1.0,
            derivation=derivation,
        )
    )


def proposal_data(row):
    return {
        "id": row.id,
        "version": row.version,
        "title": row.title,
        "tool_name": row.tool_name,
        "arguments": row.arguments,
        "state": row.state,
        "source_message_id": row.source_message_id,
        "result": row.result,
    }


def plan_data(row, db=None):
    result = {
        "id": row.id,
        "version": row.version,
        "topic": row.topic,
        "mode": row.mode,
        "state": row.state,
        "next_due_at": utc(row.next_due_at).isoformat() if row.next_due_at else None,
        "due_at": utc(row.due_at).isoformat(),
        "timezone": row.timezone,
        "interval_seconds": row.interval_seconds,
        "occurrences": row.occurrence_count,
        "max_occurrences": row.max_occurrences,
        "respect_quiet_hours": row.respect_quiet_hours,
        "availability": row.availability,
    }
    if db is not None:
        action = db.scalar(
            select(ScheduledAction)
            .where(
                ScheduledAction.user_id == row.user_id, ScheduledAction.reminder_plan_id == row.id
            )
            .order_by(ScheduledAction.created_at.desc())
            .limit(1)
        )
        if action:
            result["latest_execution"] = {
                "id": action.id,
                "state": action.state,
                "reason": action.reason,
                "plan_version": action.subject_version,
                "message_id": action.message_id,
                "telegram_states": list(
                    db.scalars(
                        select(TelegramDelivery.state).where(
                            TelegramDelivery.user_id == row.user_id,
                            TelegramDelivery.action_id == action.id,
                        )
                    ).all()
                ),
            }
    return result


def referenced(db, model, owner, identifier, reference, states):
    query = select(model).where(model.user_id == owner)
    if identifier:
        return db.scalar(query.where(model.id == identifier).with_for_update())
    query = query.where(model.state.in_(states))
    label = model.topic if model is ReminderPlan else model.title
    if reference:
        exact = list(
            db.scalars(
                query.where(func.lower(label) == func.lower(reference)).limit(2).with_for_update()
            )
        )
        if exact:
            return exact[0] if len(exact) == 1 else None
        query = query.where(label.icontains(reference, autoescape=True))
    matches = list(db.scalars(query.limit(2).with_for_update()))
    return matches[0] if len(matches) == 1 else None


def save_proposal(db, owner, tool, title, arguments, source, now, *, state="proposed"):
    if tool not in PROPOSABLE or len(json.dumps(arguments)) > 6000:
        return reject("unsupported_proposal", "That action cannot be proposed through chat.")
    row = ConversationProposal(
        user_id=owner,
        tool_name=tool,
        title=title,
        arguments=arguments,
        source_message_id=source,
        state=state,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    evidence(db, owner, row.id, source, "conversational-proposal")
    return {
        "status": state,
        "kind": "proposal",
        "id": row.id,
        "version": row.version,
        "context": proposal_data(row),
        "transition": {
            "type": "proposal",
            "id": row.id,
            "from": None,
            "to": state,
            "source_message_id": source,
        },
    }


def schedule(db, owner, args: ScheduleReminder, source, now, *, reference=None, store_missing=True):
    user = db.get(User, owner)
    reference = reference or message_time(db, source, now)
    try:
        if len(set(args.blocked_periods)) == 4 and args.availability_until is None:
            raise ValueError("Which time period is available for this reminder?")
        if args.mode == "recurring":
            if args.interval_seconds is None:
                raise ValueError("How often should I check in?")
            if args.interval_seconds < policy()["minimum_checkin_seconds"]:
                raise ValueError(
                    f"The minimum recurring interval is {policy()['minimum_checkin_seconds']} "
                    "seconds. What interval should I use?"
                )
        elif args.interval_seconds is not None:
            raise ValueError("Choose one-time or recurring scheduling, not both.")
        when = args.when
        if when is None and args.mode == "recurring":
            when = When(kind="after", seconds=args.interval_seconds)
        if when is None:
            raise ValueError("When should I remind you?")
        if when.bound == "exclusive":
            raise ValueError("How long before that time should the reminder arrive?")
        due = resolve_when(when, reference, user.timezone)
        if reference < message_time(db, source, now) and due <= message_time(db, source, now):
            raise ValueError("That proposed time has passed. What new time should I use?")
        if due < utc(now) - timedelta(days=1) or due > utc(now) + timedelta(days=366):
            raise ValueError("Choose a reminder time within the next year.")
        if when.kind == "local" and when.date and due <= reference:
            raise ValueError("That date/time has passed. What new time should I use?")
    except ValueError as error:
        if not store_missing:
            return reject("clarification_required", str(error))
        result = save_proposal(
            db,
            owner,
            "schedule_reminder",
            args.topic,
            args.model_dump(mode="json", exclude_none=True),
            source,
            now,
            state="needs_clarification",
        )
        result["question"] = str(error)
        return result
    row = ReminderPlan(
        user_id=owner,
        topic=args.topic,
        mode=args.mode,
        timezone=when.timezone or user.timezone,
        due_at=due,
        next_due_at=due,
        interval_seconds=args.interval_seconds,
        max_occurrences=1 if args.mode == "once" else args.max_occurrences,
        respect_quiet_hours=args.respect_quiet_hours
        if args.respect_quiet_hours is not None
        else args.mode == "recurring",
        availability={
            "blocked_periods": args.blocked_periods,
            "until": args.availability_until.isoformat() if args.availability_until else None,
        },
        source_message_id=source,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    from .reminders import next_permitted_time

    row.next_due_at = next_permitted_time(row, user, max(due, utc(now)))
    if row.next_due_at is None:
        row.state = "paused"
    evidence(db, owner, row.id, source, "explicit-personal-reminder")
    return {
        "status": "saved",
        "kind": "reminder",
        "id": row.id,
        "version": row.version,
        "requires_response_refresh": row.next_due_at != due
        or (args.mode == "recurring" and not user.checkins_enabled),
        "needs_checkin_opt_in": args.mode == "recurring" and not user.checkins_enabled,
        "context": plan_data(row),
        "transition": {
            "type": "reminder",
            "id": row.id,
            "from": None,
            "to": row.state,
            "next_due_at": utc(row.next_due_at).isoformat() if row.next_due_at else None,
            "source_message_id": source,
        },
    }


def change_plan(db, owner, args: ChangeReminder, source, now):
    row = referenced(db, ReminderPlan, owner, args.id, args.reference, ["active", "paused"])
    if not row:
        return reject("unknown_reminder", "Which reminder do you mean?")
    if args.version is not None and row.version != args.version:
        return reject("version_conflict", "That reminder changed; use its current version.")
    if row.state in {"cancelled", "completed"} and args.action not in {"cancel", "complete"}:
        return reject("closed_reminder", "This reminder is closed; schedule a new one if needed.")
    before = row.state
    if args.interval_seconds is not None and (
        row.mode != "recurring" or args.interval_seconds < policy()["minimum_checkin_seconds"]
    ):
        return reject("invalid_interval", "Use a permitted recurring interval.")
    due = None
    if args.when:
        try:
            due = resolve_when(args.when, message_time(db, source, now), row.timezone)
        except ValueError as error:
            return reject("invalid_time", str(error))
        if due <= utc(now) - timedelta(days=1) or due > utc(now) + timedelta(days=366):
            return reject("invalid_time", "Choose a current reminder time within the next year.")
        if args.when.bound == "exclusive" or (
            args.when.kind == "local" and args.when.date and due <= message_time(db, source, now)
        ):
            return reject("invalid_time", "Choose an exact future reminder time.")
    if args.action == "resume" and row.mode == "once" and due is None:
        if utc(row.due_at) <= utc(now):
            return reject(
                "time_required", "The original reminder time has passed; when should it run?"
            )
        due = utc(row.due_at)
    if args.interval_seconds and due is None and row.state == "active":
        due = message_time(db, source, now) + timedelta(seconds=args.interval_seconds)
    if args.respect_quiet_hours is not None and due is None and row.state == "active":
        due = (
            utc(now) + timedelta(seconds=row.interval_seconds)
            if row.mode == "recurring"
            else max(utc(now), utc(row.due_at))
        )
    if args.topic is not None:
        row.topic = args.topic
    if args.blocked_periods is not None or "availability_until" in args.model_fields_set:
        row.availability = {
            "blocked_periods": args.blocked_periods
            if args.blocked_periods is not None
            else row.availability.get("blocked_periods", []),
            "until": args.availability_until.isoformat() if args.availability_until else None,
        }
    if args.respect_quiet_hours is not None:
        row.respect_quiet_hours = args.respect_quiet_hours
    if args.interval_seconds is not None:
        row.interval_seconds = args.interval_seconds
    if due:
        row.next_due_at = due
        row.due_at = due
    if args.action in {"pause", "cancel", "complete"}:
        row.state = {"pause": "paused", "cancel": "cancelled", "complete": "completed"}[args.action]
        row.next_due_at = None
    elif args.action == "resume":
        row.state = "active"
        row.next_due_at = due or utc(now) + timedelta(seconds=row.interval_seconds)
    row.version += 1
    row.updated_at = now
    if row.state == "active" and row.next_due_at:
        from .reminders import next_permitted_time

        row.next_due_at = next_permitted_time(
            row, db.get(User, owner), max(utc(now), utc(row.next_due_at))
        )
        if row.next_due_at is None:
            row.state = "paused"
    db.execute(
        update(ScheduledAction)
        .where(
            ScheduledAction.reminder_plan_id == row.id,
            ScheduledAction.user_id == owner,
            ScheduledAction.state.in_(["pending", "executing", "retryable"]),
        )
        .values(state="cancelled", reason="reminder_changed", lease_token=None, lease_until=None)
    )
    evidence(db, owner, row.id, source, "semantic-reminder-change")
    return {
        "status": "updated",
        "kind": "reminder",
        "id": row.id,
        "version": row.version,
        "context": plan_data(row),
        "transition": {
            "type": "reminder",
            "id": row.id,
            "from": before,
            "to": row.state,
            "next_due_at": utc(row.next_due_at).isoformat() if row.next_due_at else None,
        },
    }


def resolve_proposal(db, owner, args: ResolveProposal, source, now, execute, schemas):
    row = referenced(
        db,
        ConversationProposal,
        owner,
        args.id,
        args.reference,
        ["proposed", "needs_clarification", "paused"],
    )
    if row is None:
        return reject("unknown_proposal", "Which proposed action do you mean?")
    if args.version is not None and row.version != args.version:
        return reject("version_conflict", "That proposal changed; use its current version.")
    if row.state in {"executed", "cancelled"}:
        return reject(
            "closed_proposal", "That proposal is already closed; it will not execute again."
        )
    before = row.state
    proposed = row.arguments | args.changes
    for key in ("when", "deadline"):
        change = args.changes.get(key)
        previous = row.arguments.get(key)
        if isinstance(change, dict) and isinstance(previous, dict):
            if change.get("kind", previous.get("kind")) == previous.get("kind"):
                proposed[key] = previous | change
            else:
                proposed[key] = {
                    k: v for k, v in previous.items() if k in {"timezone", "anchor", "bound"}
                } | change
    if row.tool_name not in PROPOSABLE or set(proposed) - set(schemas[row.tool_name].model_fields):
        return reject("invalid_proposal", "The proposed parameters are not valid for that action.")
    if args.action == "execute":
        try:
            schemas[row.tool_name].model_validate(proposed)
        except ValidationError:
            row.arguments = proposed
            row.state = "needs_clarification"
            row.version += 1
            row.updated_at = now
            return {
                "status": "needs_clarification",
                "kind": "proposal",
                "id": row.id,
                "version": row.version,
                "question": "I still need the missing action details.",
                "context": proposal_data(row),
            }
        reference = message_time(db, source, now)
        timing = proposed.get("when") or proposed.get("deadline")
        new_time = any(
            isinstance(args.changes.get(key), dict)
            and bool(set(args.changes[key]) & {"kind", "seconds", "date", "at", "anchor"})
            for key in ("when", "deadline")
        )
        if timing and timing.get("anchor", "message") == "message" and not new_time:
            reference = message_time(db, row.source_message_id, now)
        result = execute(row.tool_name, proposed, reference)
        if result["status"] in {"rejected", "needs_clarification"}:
            row.arguments = proposed
            row.state = "needs_clarification"
            row.version += 1
            row.updated_at = now
            return {
                "status": "needs_clarification",
                "kind": "proposal",
                "id": row.id,
                "version": row.version,
                "question": result.get("question", "Please clarify the proposed action."),
                "context": proposal_data(row),
                "execution": result,
            }
        row.state = "executed"
        if result.get("id"):
            db.add(
                ProvenanceLink(
                    user_id=owner,
                    target_id=result["id"],
                    source_type="conversation_proposal",
                    source_id=row.id,
                    confidence=1.0,
                    derivation="accepted-proposal-parameters",
                )
            )
        row.result = {
            key: result[key] for key in ("id", "kind", "version", "status") if key in result
        }
    else:
        result = None
        row.state = {
            "amend": "proposed",
            "pause": "paused",
            "resume": "proposed",
            "cancel": "cancelled",
        }[args.action]
    row.arguments = proposed
    row.version += 1
    row.updated_at = now
    evidence(db, owner, row.id, source, "proposal-" + args.action)
    return {
        "status": "updated",
        "kind": "proposal",
        "id": row.id,
        "version": row.version,
        "context": proposal_data(row),
        "execution": result,
        "requires_response_refresh": bool(result and result.get("requires_response_refresh")),
        "transition": {
            "type": "proposal",
            "id": row.id,
            "from": before,
            "to": row.state,
            "source_message_id": source,
        },
    }


def workflow(db, owner, args: ManageWorkflow, source, now, event_id):
    if args.action == "start":
        if args.kind == "vocabulary":
            from .vocabulary import VocabularyLearningSkill

            prompt = VocabularyLearningSkill().prompt(
                db, owner, event_id, now, append=False, target_word=args.word
            )
            attempt = db.scalar(
                select(VocabularyAttempt).where(
                    VocabularyAttempt.user_id == owner,
                    VocabularyAttempt.source_event_id == event_id,
                )
            )
            if not prompt or not attempt:
                return {
                    "status": "read",
                    "context": {"started": False, "reason": "no_new_review_due"},
                }
            return {
                "status": "saved",
                "kind": "workflow",
                "id": attempt.id,
                "version": attempt.version,
                "context": {"prompt": prompt},
                "transition": {
                    "type": "vocabulary",
                    "id": attempt.id,
                    "from": None,
                    "to": "active",
                    "source_message_id": source,
                },
            }
        from .daily import EveningReflectionSkill

        changed = EveningReflectionSkill().start(db, owner, event_id, now, append=False)
        prompt = db.scalar(
            select(ReflectionPrompt)
            .where(ReflectionPrompt.user_id == owner)
            .order_by(ReflectionPrompt.created_at.desc())
            .limit(1)
        )
        return {
            "status": "saved" if changed else "read",
            "kind": "workflow",
            "id": prompt.id,
            "version": prompt.version,
            "context": {"started": changed, "state": prompt.workflow_state},
            **(
                {
                    "transition": {
                        "type": "reflection",
                        "id": prompt.id,
                        "from": None,
                        "to": "active",
                        "source_message_id": source,
                    }
                }
                if changed
                else {}
            ),
        }
    model = ReflectionPrompt if args.kind == "reflection" else VocabularyAttempt
    row = db.scalar(
        select(model).where(model.id == args.id, model.user_id == owner).with_for_update()
    )
    if row is None:
        return reject("unknown_workflow", "Which activity do you mean?")
    if args.version != row.version:
        return reject("version_conflict", "That activity changed; use its current version.")
    before = row.workflow_state
    if args.action in {"pause", "resume", "cancel"}:
        if row.workflow_state in {"cancelled", "completed"}:
            return reject("closed_workflow", "That activity is already closed.")
        row.workflow_state = {"pause": "paused", "resume": "active", "cancel": "cancelled"}[
            args.action
        ]
    elif row.workflow_state == "cancelled":
        return reject("closed_workflow", "This activity was cancelled.")
    elif args.kind == "reflection":
        ids = list(dict.fromkeys(args.source_message_ids or [source]))
        messages = list(
            db.scalars(
                select(Message)
                .where(Message.user_id == owner, Message.id.in_(ids), Message.role == "user")
                .order_by(Message.ordinal)
            )
        )
        if len(messages) != len(ids):
            return reject("invalid_sources", "Use only the owner's actual messages.")
        original = "\n".join(m.content for m in messages)
        journal = db.scalar(
            select(JournalEntry).where(
                JournalEntry.user_id == owner, JournalEntry.local_date == row.local_date
            )
        )
        if journal is None:
            journal = JournalEntry(
                user_id=owner,
                local_date=row.local_date,
                original_content=original,
                summary=args.summary,
                structured_state={},
                source_message_id=messages[0].id,
                source_event_id=event_id,
                created_at=now,
                updated_at=now,
            )
            db.add(journal)
            db.flush()
        else:
            if journal.deleted:
                return reject("deleted_reflection", "That journal was deleted.")
            from .persistence import MutationResult, atomic_update, invalidate_journal_episodes

            content = (
                original
                if args.action == "correct"
                else (journal.corrected_content or journal.original_content) + "\n" + original
            )
            outcome = atomic_update(
                db,
                JournalEntry,
                journal.id,
                owner,
                journal.version,
                {"corrected_content": content, "summary": args.summary, "updated_at": now},
            )
            if outcome is not MutationResult.UPDATED:
                return reject("version_conflict", "The journal changed.")
            invalidate_journal_episodes(db, owner, journal.id)
            db.get(User, owner).context_reset_at = now
        for msg in messages:
            evidence(db, owner, journal.id, msg.id, "semantic-reflection")
        row.status = "completed"
        row.journal_entry_id = journal.id
        row.workflow_state = "completed"
    else:
        item = db.get(VocabularyItem, row.vocabulary_item_id)
        if item is None or item.user_id != owner or item.deleted:
            return reject("unknown_word", "That vocabulary item is unavailable.")
        if args.grade is None:
            return reject("grade_required", "Provide an explicit semantic assessment.")
        if row.user_answer is not None and args.action != "correct":
            return reject("already_answered", "Use a correction for an existing answer.")
        if args.action == "correct" and row.correct is True:
            return reject("already_correct", "This answer was already recorded as correct.")
        prior = row.correct
        if row.user_answer is None:
            answer_id = args.source_message_ids[0] if len(args.source_message_ids) == 1 else source
            answer = db.scalar(
                select(Message).where(
                    Message.id == answer_id, Message.user_id == owner, Message.role == "user"
                )
            )
            if answer is None:
                return reject("invalid_sources", "Use an actual owner answer.")
            row.user_answer = answer.content[:1000]
            row.user_sentence = args.sentence
        row.reviewed_at = now
        row.correct = {"correct": True, "incorrect": False, "uncertain": None}[args.grade]
        if row.correct is not None:
            if args.action != "correct" or prior is None:
                item.recall_attempts += 1
            if row.correct:
                item.correct_recalls += 1
            if not row.correct:
                item.mastery_state = "developing"
                item.next_review_at = now + timedelta(days=1)
            elif item.correct_recalls >= 4:
                item.mastery_state = "mastered"
                item.next_review_at = None
            elif item.correct_recalls >= 2:
                item.mastery_state = "familiar"
                item.next_review_at = now + timedelta(days=7)
            else:
                item.mastery_state = "developing"
                item.next_review_at = now + timedelta(days=2)
            item.last_reviewed_at = now
            item.updated_at = now
            item.version += 1
        if args.sentence:
            item.user_sentences = [*item.user_sentences, args.sentence][-20:]
        row.workflow_state = "active" if row.correct is None else "completed"
    row.version += 1
    evidence(db, owner, row.id, source, "semantic-workflow-" + args.action)
    return {
        "status": "updated",
        "kind": "workflow",
        "id": row.id,
        "version": row.version,
        "transition": {
            "type": args.kind,
            "id": row.id,
            "from": before,
            "to": row.workflow_state,
            "source_message_id": source,
        },
    }


def conversation_state(db, owner, now, source_id=None):
    proposals = list(
        db.scalars(
            select(ConversationProposal)
            .where(
                ConversationProposal.user_id == owner,
            )
            .order_by(
                case(
                    (
                        ConversationProposal.state.in_(
                            ["proposed", "needs_clarification", "paused"]
                        ),
                        0,
                    ),
                    else_=1,
                ),
                ConversationProposal.updated_at.desc(),
                ConversationProposal.id,
            )
            .limit(4)
        )
    )
    plans = list(
        db.scalars(
            select(ReminderPlan)
            .where(ReminderPlan.user_id == owner)
            .order_by(ReminderPlan.updated_at.desc(), ReminderPlan.id)
            .limit(6)
        )
    )
    prompts = list(
        db.scalars(
            select(ReflectionPrompt)
            .where(
                ReflectionPrompt.user_id == owner,
                ReflectionPrompt.workflow_state != "cancelled",
            )
            .order_by(ReflectionPrompt.created_at.desc())
            .limit(2)
        )
    )
    attempts = list(
        db.scalars(
            select(VocabularyAttempt)
            .where(
                VocabularyAttempt.user_id == owner,
                VocabularyAttempt.workflow_state != "cancelled",
            )
            .order_by(VocabularyAttempt.created_at.desc())
            .limit(2)
        )
    )
    link = db.scalar(select(TelegramConnection).where(TelegramConnection.user_id == owner))
    intentions = list(
        db.scalars(
            select(Commitment)
            .where(
                Commitment.user_id == owner,
                Commitment.deleted.is_(False),
                Commitment.state.in_(["open", "in_progress", "waiting", "paused"]),
            )
            .order_by(Commitment.last_touched_at.desc())
            .limit(4)
        )
    )
    result = {
        "commitments": [],
        "proposals": [],
        "reminders": [],
        "workflows": [],
        "scheduling_policy": policy(),
        "delivery": {
            "in_app": True,
            "telegram_linked": bool(link and link.linked),
            "telegram_proactive_enabled": bool(link and link.linked and link.proactive_enabled),
        },
    }
    if source_id:
        result["message_at"] = message_time(db, source_id, now).isoformat()
        from zoneinfo import ZoneInfo

        result["message_local"] = (
            message_time(db, source_id, now)
            .astimezone(ZoneInfo(db.get(User, owner).timezone))
            .isoformat()
        )
    user = db.get(User, owner)
    result["scheduling_policy"]["quiet_hours"] = {
        "start": user.quiet_start,
        "end": user.quiet_end,
        "timezone": user.timezone,
    }
    for key, entries in (
        ("proposals", [proposal_data(row) for row in proposals]),
        ("reminders", [plan_data(row, db) for row in plans]),
        (
            "commitments",
            [
                {
                    "id": row.id,
                    "version": row.version,
                    "title": row.title,
                    "state": row.state,
                    "purpose": row.purpose,
                    "next_action": row.next_action,
                    "due_at": utc(row.due_at).isoformat() if row.due_at else None,
                }
                for row in intentions
            ],
        ),
        (
            "workflows",
            [
                {
                    "kind": "reflection",
                    "id": p.id,
                    "version": p.version,
                    "state": p.workflow_state,
                    "local_date": str(p.local_date),
                    "created_at": utc(p.created_at).isoformat(),
                    "prompt": "How did things go today?",
                }
                for p in prompts
            ]
            + [
                {
                    "kind": "vocabulary",
                    "id": a.id,
                    "version": a.version,
                    "state": a.workflow_state,
                    "prompt": a.prompt,
                    "answer": a.user_answer,
                    "grade": a.correct,
                    "definition": db.get(VocabularyItem, a.vocabulary_item_id).definition,
                }
                for a in attempts
                if db.get(VocabularyItem, a.vocabulary_item_id)
                and not db.get(VocabularyItem, a.vocabulary_item_id).deleted
            ],
        ),
    ):
        for entry in entries:
            result[key].append(entry)
            if len(json.dumps(result)) > 5000:
                result[key].pop()
    return result
