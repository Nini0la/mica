"""One validated owner-commitment lifecycle, shared by model tools and UI controls."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import (
    Commitment,
    CommitmentRevision,
    ProvenanceLink,
    Responsibility,
    ScheduledAction,
    Thread,
    User,
)
from .persistence import MutationResult, atomic_update
from .prospective import ParsedTiming, parse_commitment_timing
from .temporal import When, resolve_when

OPEN = {"open", "in_progress", "waiting"}
TERMINAL = {"complete", "completed", "cancelled", "abandoned"}
PERIODS = {"night": (0, 6), "morning": (6, 12), "afternoon": (12, 17), "evening": (17, 24)}
FIELDS = (
    "title",
    "purpose",
    "completion_criteria",
    "next_action",
    "state",
    "due_at",
    "due_granularity",
    "due_source_text",
    "due_bound",
    "importance",
    "constraints",
    "availability",
    "available_minutes",
    "dependencies",
    "followup_enabled",
    "followup_minutes",
    "max_followups",
    "completed_at",
)


def utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def normalized(value: str) -> str:
    return " ".join(value.casefold().replace("’", "'").split())


def lifecycle_message(text: str) -> bool:
    value = normalized(text)
    return bool(
        re.search(
            r"\bdeadline.*(?:moved|changed|now)|\bunavailable\b|"
            r"\bavailability\b|\bavailable (?:again|now)\b|"
            r"\b(?:update|change|revise|clear)\b.*"
            r"\b(?:plan|next action|blocked periods|follow-ups|check-ins|constraints|blocker)\b|"
            r"\bi(?:'ve| have)? (?:finished|completed)\b|\bdone with\b|"
            r"(?:^|[.!?]\s*)(?:please )?(?:pause|cancel|resume|reopen)\b",
            value,
        )
    )


class PlanFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    purpose: str | None = Field(default=None, max_length=1000)
    completion_criteria: str | None = Field(default=None, max_length=1000)
    next_action: str | None = Field(default=None, max_length=500)
    importance: int | None = Field(default=None, ge=0, le=3)
    constraints: list[str] | None = Field(default=None, max_length=10)
    blocked_periods: list[Literal["morning", "afternoon", "evening", "night"]] | None = Field(
        default=None, max_length=4
    )
    availability_until: date | None = None
    available_minutes: int | None = Field(default=None, ge=0, le=10080)
    blocker: str | None = Field(default=None, max_length=1000)
    followup_enabled: bool | None = None
    followup_minutes: int | None = Field(default=None, ge=1, le=10080)
    max_followups: int | None = Field(default=None, ge=0, le=10)
    estimated_effort_minutes: int | None = Field(default=None, ge=1, le=10080)


class CreateCommitment(PlanFields):
    title: str = Field(min_length=1, max_length=500)
    project_id: str | None = Field(default=None, min_length=1, max_length=36)
    deadline_text: str | None = Field(default=None, min_length=1, max_length=500)
    deadline: When | None = None


class ChangeCommitment(PlanFields):
    id: str | None = Field(default=None, min_length=1, max_length=36)
    reference: str | None = Field(default=None, min_length=1, max_length=300)
    version: int | None = Field(default=None, ge=1)
    action: Literal["update", "complete", "pause", "resume", "cancel", "reopen"] = "update"
    title: str | None = Field(default=None, min_length=1, max_length=500)
    deadline_text: str | None = Field(default=None, min_length=1, max_length=500)
    clear_deadline: bool = False
    deadline: When | None = None


class ListCommitments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(default="", max_length=200)


def reject(reason: str, question: str) -> dict[str, Any]:
    return {"status": "rejected", "reason": reason, "question": question}


def revision(db: Session, item: Commitment, source_type: str, source_id: str | None) -> None:
    db.flush()
    prior = db.scalar(
        select(CommitmentRevision).where(
            CommitmentRevision.user_id == item.user_id,
            CommitmentRevision.commitment_id == item.id,
            CommitmentRevision.version == item.version,
        )
    )
    if prior is None:
        db.add(
            CommitmentRevision(
                user_id=item.user_id,
                commitment_id=item.id,
                version=item.version,
                source_type=source_type,
                source_id=source_id,
                created_at=item.last_touched_at,
                snapshot={
                    key: utc(value).isoformat()
                    if isinstance(value, datetime)
                    else value.isoformat()
                    if isinstance(value, date)
                    else value
                    for key in FIELDS
                    for value in [getattr(item, key)]
                },
            )
        )
        db.flush()
    elif prior.source_id is None and source_id is not None:
        prior.source_id = source_id
        prior.source_type = source_type


def quiet(user: User, value: datetime) -> bool:
    hour = utc(value).astimezone(ZoneInfo(user.timezone)).hour
    start, end = user.quiet_start, user.quiet_end
    if start == end:
        return False
    return start <= hour < end if start < end else hour >= start or hour < end


def available(user: User, availability: dict, value: datetime) -> bool:
    local = utc(value).astimezone(ZoneInfo(user.timezone))
    until = availability.get("until")
    if until and local.date() > date.fromisoformat(until):
        return True
    return not any(
        PERIODS[period][0] <= local.hour < PERIODS[period][1]
        for period in availability.get("blocked_periods", [])
        if period in PERIODS
    )


def next_allowed(user: User, availability: dict, candidate: datetime) -> datetime | None:
    # Walk UTC instants, not wall times: gaps/folds cannot create nonexistent wake-ups.
    candidate = utc(candidate)
    for _ in range(8 * 24 * 4):
        if not quiet(user, candidate) and available(user, availability, candidate):
            return candidate
        candidate += timedelta(minutes=15)
    return None


def plan_next(user: User, item: Commitment, now: datetime) -> datetime | None:
    if (
        item.deleted
        or item.state not in OPEN
        or not item.followup_enabled
        or not user.checkins_enabled
        or item.followup_count >= item.max_followups
        or item.ambiguity != "none"
    ):
        return None
    earliest = utc(now) + timedelta(minutes=1)
    candidate = utc(now) + timedelta(minutes=item.followup_minutes)
    if item.last_surfaced_at is None and item.due_at and utc(item.due_at) > earliest:
        lead = timedelta(minutes=max(item.estimated_effort_minutes or 120, 15))
        candidate = min(candidate, max(earliest, utc(item.due_at) - lead))
    return next_allowed(user, item.availability or {}, max(earliest, candidate))


def invalidate_and_replan(db: Session, item: Commitment, now: datetime) -> None:
    db.execute(
        update(ScheduledAction)
        .where(
            ScheduledAction.user_id == item.user_id,
            ScheduledAction.commitment_id == item.id,
            ScheduledAction.state.in_(["pending", "executing", "retryable"]),
        )
        .values(state="cancelled", reason="commitment_changed", lease_token=None, lease_until=None)
    )
    user = db.get(User, item.user_id)
    if user is not None:
        item.next_relevant_moment = plan_next(user, item, now)
    if item.deleted:
        db.execute(
            update(CommitmentRevision)
            .where(
                CommitmentRevision.user_id == item.user_id,
                CommitmentRevision.commitment_id == item.id,
            )
            .values(snapshot={})
        )


def explicit_followup(text: str) -> bool:
    text = normalized(text)
    if re.search(
        r"\b(?:don't|do not|no|stop|pause|cancel|without)\s+(?:any\s+)?"
        r"(?:remind|reminders|check|follow|nudg)",
        text,
    ):
        return False
    return bool(
        re.search(
            r"stay on track|check[ -]?in|follow[ -]?up|remind me|"
            r"keep me accountable|track (?:this|it)",
            text,
        )
    )


def temporal_input(phrase: str | None, text: str, trusted: bool) -> str:
    if not phrase:
        return text
    source, proposed = normalized(text), normalized(phrase)
    if not trusted and proposed not in source:
        raise ValueError("Use the owner's actual deadline phrase, not an invented timestamp")
    if not trusted:
        before = source[: source.index(proposed)].strip().split()
        if before and before[-1] == "before" and not proposed.startswith("before"):
            return "before " + phrase
    return phrase


def structured_timing(value: When, reference: datetime, timezone_name: str, source: str):
    granularity = (
        "second" if value.kind == "after" or (value.at and len(value.at) > 5) else "minute"
    )
    if value.kind == "local" and value.date is not None and value.at is None:
        # A date-only deadline is a date bound, not an invented requested clock time.
        granularity = "date"
        value = value.model_copy(
            update={"at": "00:00" if value.bound == "exclusive" else "23:59:59"}
        )
    return ParsedTiming(
        resolve_when(value, reference, timezone_name), granularity, source, bound=value.bound
    )


def field_changes(
    args: PlanFields, user: User, text: str, now: datetime, *, trusted: bool, semantic: bool = False
) -> dict:
    changes = args.model_dump(
        exclude_none=True,
        include={
            "purpose",
            "completion_criteria",
            "next_action",
            "importance",
            "constraints",
            "available_minutes",
            "followup_enabled",
            "followup_minutes",
            "max_followups",
            "estimated_effort_minutes",
        },
    )
    if args.constraints and any(len(value) > 300 for value in args.constraints):
        raise ValueError("Each constraint must be at most 300 characters")
    if args.followup_enabled and not trusted and not semantic and not explicit_followup(text):
        raise ValueError("Ask whether the owner wants scheduled in-app follow-ups")
    if args.followup_enabled is None and not trusted and not semantic and explicit_followup(text):
        changes["followup_enabled"] = True
    cadence = (
        None if semantic else re.search(r"\bevery\s+(\d+)\s+(minutes?|hours?|days?)\b", text, re.I)
    )
    if cadence and not trusted and not semantic:
        unit = cadence.group(2).casefold()
        minutes = int(cadence.group(1)) * (
            1440 if unit.startswith("day") else 60 if unit.startswith("hour") else 1
        )
        if not 1 <= minutes <= 10080:
            raise ValueError("Choose a check-in interval between one minute and one week")
        changes["followup_minutes"] = minutes
    elif not trusted and not semantic:
        changes.pop("followup_minutes", None)
    if args.blocked_periods is not None:
        if (
            not trusted
            and not semantic
            and any(period not in normalized(text) for period in args.blocked_periods)
        ):
            raise ValueError("Availability changes need supporting owner wording")
        until = args.availability_until
        if not semantic and "this week" in normalized(text):
            local_date = utc(now).astimezone(ZoneInfo(user.timezone)).date()
            until = local_date + timedelta(days=6 - local_date.weekday())
        changes["availability"] = {
            "blocked_periods": list(dict.fromkeys(args.blocked_periods)),
            "until": until.isoformat() if until else None,
        }
        if args.available_minutes is None:
            changes["available_minutes"] = None
    if args.blocker is not None:
        changes["dependencies"] = (
            [{"kind": "blocked", "label": args.blocker}] if args.blocker else []
        )
    return changes


def describe(item: Commitment, user: User) -> dict[str, Any]:
    return {
        "id": item.id,
        "version": item.version,
        "title": item.title,
        "state": item.state,
        "purpose": item.purpose,
        "completion_criteria": item.completion_criteria,
        "next_action": item.next_action,
        "due_at": utc(item.due_at).isoformat() if item.due_at else None,
        "due_local": utc(item.due_at).astimezone(ZoneInfo(user.timezone)).isoformat()
        if item.due_at
        else None,
        "due_bound": item.due_bound,
        "due_granularity": item.due_granularity,
        "timezone": user.timezone,
        "ambiguity": item.ambiguity,
        "constraints": item.constraints,
        "availability": item.availability,
        "followup_enabled": item.followup_enabled,
        "followup_minutes": item.followup_minutes,
        "followup_count": item.followup_count,
        "max_followups": item.max_followups,
        "next_check_at": utc(item.next_relevant_moment).isoformat()
        if item.next_relevant_moment
        else None,
        "importance": item.importance,
        "available_minutes": item.available_minutes,
        "blockers": item.dependencies,
        "related_thread_id": item.related_thread_id,
    }


def create(
    db: Session,
    user_id: str,
    args: CreateCommitment,
    text: str,
    source_id: str,
    now: datetime,
    allowed_ids: set[str],
    *,
    semantic: bool = False,
    reference_time: datetime | None = None,
) -> dict:
    user = db.get(User, user_id)
    assert user is not None
    if (
        not semantic
        and re.search(r"\b(maybe|might|perhaps|sometime|considering)\b", normalized(text))
        and not re.search(r"\b(track|save|record|remind)\b", normalized(text))
    ):
        return reject(
            "ambiguous_intention",
            "Would you like this tracked as a commitment, or are we just exploring it?",
        )
    if args.project_id and (
        args.project_id not in allowed_ids
        or not db.scalar(
            select(Thread.id).where(
                Thread.id == args.project_id,
                Thread.user_id == user_id,
                Thread.deleted.is_(False),
            )
        )
    ):
        return reject("unknown_project", "Which of your projects should this belong to?")
    try:
        timing = (
            structured_timing(
                args.deadline,
                reference_time or now,
                user.timezone,
                args.deadline_text or text[:500],
            )
            if args.deadline
            else ParsedTiming()
            if semantic and args.deadline_text is None
            else parse_commitment_timing(
                temporal_input(args.deadline_text, text, semantic),
                reference_time or now,
                user.timezone,
            )
        )
        changes = field_changes(args, user, text, now, trusted=False, semantic=semantic)
    except ValueError as error:
        return reject("clarification_required", str(error))
    if timing.ambiguity != "none":
        return reject(
            timing.ambiguity,
            "What exact date and time do you mean? I haven't saved a guessed deadline.",
        )
    item = Commitment(
        user_id=user_id,
        title=args.title,
        related_thread_id=args.project_id,
        due_at=timing.due_at,
        due_source_text=timing.source_phrase,
        deadline_hint=timing.source_phrase,
        due_granularity=timing.due_granularity,
        due_bound=timing.bound if timing.due_at else "unspecified",
        estimated_effort_minutes=changes.pop(
            "estimated_effort_minutes", timing.estimated_effort_minutes
        ),
        confidence=1.0,
        derivation="explicit-intention-v1",
        created_at=now,
        last_touched_at=now,
        **changes,
    )
    db.add(item)
    db.flush()
    item.next_relevant_moment = plan_next(user, item, now)
    revision(db, item, "message", source_id)
    db.add(
        ProvenanceLink(
            user_id=user_id,
            target_id=item.id,
            source_type="message",
            source_id=source_id,
            confidence=1.0,
            derivation="explicit-intention-v1",
        )
    )
    allowed_ids.add(item.id)
    return {
        "status": "saved",
        "id": item.id,
        "kind": "commitment",
        "version": item.version,
        "delivery": "in_app" if item.next_relevant_moment else "none",
        "needs_checkin_opt_in": bool(item.followup_enabled and not user.checkins_enabled),
        "context": describe(item, user),
    }


def resolve(
    db: Session,
    user_id: str,
    args: ChangeCommitment,
    text: str,
    *,
    trusted: bool,
    semantic: bool = False,
) -> Commitment | None:
    query = select(Commitment).where(Commitment.user_id == user_id, Commitment.deleted.is_(False))
    if (trusted or semantic) and args.id:
        return db.scalar(query.where(Commitment.id == args.id))
    reference = normalized(args.reference or "")
    if not reference and args.id:
        # Models often supply a current record ID without repeating its reference.
        # Accept that only when the owner actually names its full title this turn;
        # retain the uniqueness check below so duplicate titles still disambiguate.
        named = db.scalar(query.where(Commitment.id == args.id))
        if named and re.search(
            r"(?<!\w)" + re.escape(normalized(named.title)) + r"(?!\w)", normalized(text)
        ):
            reference = normalized(named.title)
    if not semantic and reference and reference not in normalized(text):
        return None
    if reference and args.id == reference:
        return db.scalar(query.where(Commitment.id == args.id))
    if args.action != "reopen":
        query = query.where(Commitment.state.not_in(TERMINAL))
    if reference:
        tokens = [
            part
            for part in re.findall(r"\w+", reference)
            if part not in {"the", "my", "that", "it"}
        ]
        for token in tokens:
            query = query.where(Commitment.title.icontains(token, autoescape=True))
    rows = list(db.scalars(query.order_by(Commitment.created_at.desc(), Commitment.id).limit(2)))
    if len(rows) != 1 or (args.id is not None and rows[0].id != args.id):
        return None
    return rows[0]


def change(
    db: Session,
    user_id: str,
    args: ChangeCommitment,
    text: str,
    source_id: str,
    now: datetime,
    *,
    trusted: bool = False,
    semantic: bool = False,
    reference_time: datetime | None = None,
) -> dict:
    user = db.get(User, user_id)
    assert user is not None
    item = resolve(db, user_id, args, text, trusted=trusted, semantic=semantic)
    if item is None:
        return reject(
            "ambiguous_reference",
            "Which commitment do you mean? Please name it so I update the right one.",
        )
    if db.scalar(
        select(Responsibility.id).where(
            Responsibility.user_id == user_id, Responsibility.commitment_id == item.id
        )
    ):
        return reject(
            "delegated_workflow", "Use the responsibility review controls for this delegated task."
        )
    if (
        args.version is not None
        and args.version != item.version
        and args.action not in {"pause", "cancel"}
    ):
        return reject(
            "version_conflict", "That commitment changed; please refresh its current state."
        )
    if args.action == "complete" and not trusted and not semantic:
        lower = normalized(text)
        if re.search(r"\b(?:haven't|not|never|didn't|hasn't)\b", lower) or not re.search(
            r"\b(?:i(?:'ve| have)? (?:finished|completed)|done with|mark .+ complet)", lower
        ):
            return reject(
                "completion_not_confirmed", "Are you confirming that this commitment is complete?"
            )
    try:
        changes = field_changes(args, user, text, now, trusted=trusted, semantic=semantic)
        if args.clear_deadline and (args.deadline_text or args.deadline):
            return reject(
                "conflicting_deadline_change", "Should I replace the deadline or clear it?"
            )
        if (
            not semantic
            and args.blocked_periods is not None
            and any(
                period in normalized(args.next_action or item.next_action)
                for period in args.blocked_periods
            )
        ):
            changes["next_action"] = "Choose an available time for the next step"
        if args.title is not None:
            changes["title"] = args.title
        if args.deadline_text is not None or args.deadline is not None:
            timing = (
                structured_timing(
                    args.deadline,
                    reference_time or now,
                    user.timezone,
                    args.deadline_text or text[:500],
                )
                if args.deadline
                else parse_commitment_timing(
                    temporal_input(args.deadline_text, text, trusted or semantic),
                    reference_time or now,
                    user.timezone,
                )
            )
            if timing.ambiguity != "none" or timing.due_at is None:
                return reject(
                    "ambiguous_deadline",
                    "What exact date and time should replace the deadline? "
                    "The existing one is unchanged.",
                )
            changes.update(
                due_at=timing.due_at,
                due_granularity=timing.due_granularity,
                due_source_text=timing.source_phrase,
                deadline_hint=timing.source_phrase,
                due_bound=timing.bound,
                ambiguity="none",
            )
        if args.clear_deadline:
            changes.update(
                due_at=None,
                deadline_hint=None,
                due_source_text=None,
                due_granularity=None,
                due_bound="unspecified",
                ambiguity="none",
            )
        if args.action != "update":
            if item.state in TERMINAL and args.action not in {"reopen", "cancel", "complete"}:
                return reject(
                    "terminal_state", "Explicitly reopen this closed item before resuming it."
                )
            states = {
                "complete": "completed",
                "pause": "paused",
                "cancel": "cancelled",
                "resume": "open",
                "reopen": "open",
            }
            if args.action == "resume" and item.state in TERMINAL:
                return reject(
                    "terminal_state",
                    "This item is closed; explicitly reopen it if that was a mistake.",
                )
            changes["state"] = states[args.action]
            changes["completed_at"] = now if args.action == "complete" else None
        elif item.state in TERMINAL:
            return reject(
                "terminal_state", "Explicitly reopen this closed item before changing its plan."
            )
    except ValueError as error:
        return reject("clarification_required", str(error))
    if not changes:
        return reject("no_changes", "What would you like to change about this commitment?")
    changes.update(last_touched_at=now, followup_count=0)
    revision(db, item, "before_change", source_id)
    result = atomic_update(db, Commitment, item.id, user_id, item.version, changes)
    if result is not MutationResult.UPDATED:
        return reject("version_conflict", "That commitment changed; please refresh it.")
    db.refresh(item)
    revision(db, item, "owner_control" if trusted else "message", source_id)
    db.add(
        ProvenanceLink(
            user_id=user_id,
            target_id=item.id,
            source_type="owner_control" if trusted else "message",
            source_id=source_id,
            confidence=1.0,
            derivation="explicit-commitment-change-v1",
        )
    )
    return {
        "status": "updated",
        "id": item.id,
        "kind": "commitment",
        "version": item.version,
        "action": args.action,
        "delivery": "in_app" if item.next_relevant_moment else "none",
        "context": describe(item, user),
    }
