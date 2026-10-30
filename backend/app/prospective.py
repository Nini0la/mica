from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .effort import bounded_effort_minutes
from .models import Commitment, ConversationSession, Episode, ProvenanceLink, Reminder
from .persistence import append_message, get_or_create_conversation

POLICY_VERSION = "m3-v1"


@dataclass(frozen=True)
class ParsedTiming:
    due_at: datetime | None = None
    due_granularity: str | None = None
    source_phrase: str | None = None
    estimated_effort_minutes: int | None = None
    ambiguity: str = "none"
    bound: str = "inclusive"


@dataclass(frozen=True)
class Urgency:
    score: int
    bucket: str
    factors: dict[str, int | None]


_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ValueError("Unknown user timezone") from error


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _local_wall_time(
    day: date, hour: int, minute: int, zone: ZoneInfo
) -> tuple[datetime | None, str]:
    naive = datetime.combine(day, time(hour, minute))
    valid: list[datetime] = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) == naive:
            valid.append(candidate)
    if not valid:
        return None, "nonexistent_local_time"
    instants = {_utc(candidate) for candidate in valid}
    if len(instants) > 1:
        return None, "ambiguous_local_time"
    return valid[0], "none"


def parse_commitment_timing(text: str, now: datetime, timezone_name: str) -> ParsedTiming:
    """Parse only bounded, explicit date/time and effort phrases; never infer missing data."""
    if now.tzinfo is None:
        raise ValueError("Clock values must be timezone-aware")
    zone = _zone(timezone_name)
    local_now = now.astimezone(zone)
    effort: int | None = None
    effort_match = re.search(
        r"\b(?:take|takes|should take|about)\s+(\d+|one|two|three|four|five|six|seven|eight)\s+"
        r"(minutes?|hours?)\b",
        text,
        re.I,
    )
    if effort_match:
        effort = bounded_effort_minutes(effort_match.group(1), effort_match.group(2))
        if effort is None:
            return ParsedTiming(ambiguity="effort_out_of_range")

    if re.search(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", text):
        return ParsedTiming(estimated_effort_minutes=effort, ambiguity="ambiguous_numeric_date")

    # A named period is not an exact deadline. Keep the phrase and ask for a time.
    period = re.search(r"\b(morning|afternoon|evening|tonight|later|sometime)\b", text, re.I)
    temporal_cue = re.search(
        r"\b(today|tomorrow|tonight|next|deadline|by|before|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b",
        text,
        re.I,
    )
    if (
        period
        and temporal_cue
        and not re.search(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{2}:\d{2}\b", text, re.I)
    ):
        return ParsedTiming(
            source_phrase=text.strip()[:500],
            estimated_effort_minutes=effort,
            ambiguity="ambiguous_time_of_day",
        )

    iso_match = re.search(
        r"\b(?:(?:by|before|on|to)\s+)?(\d{4}-\d{2}-\d{2})"
        r"(?:(?:[ T]|\s+at\s+)(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(Z|[+-]\d{2}:\d{2})?)?(?![\d:+-])",
        text,
        re.I,
    )
    if iso_match:
        source_phrase = iso_match.group(0)
        try:
            day = datetime.strptime(iso_match.group(1), "%Y-%m-%d").date()
            if iso_match.group(2):
                hour = int(iso_match.group(2))
                minute = int(iso_match.group(3) or 0)
                meridian = iso_match.group(4)
                offset = iso_match.group(5)
                if meridian:
                    if not 1 <= hour <= 12:
                        raise ValueError
                    hour = hour % 12 + (12 if meridian.casefold() == "pm" else 0)
                elif iso_match.group(3) is None:
                    return ParsedTiming(
                        source_phrase=source_phrase,
                        estimated_effort_minutes=effort,
                        ambiguity="ambiguous_clock",
                    )
                if offset:
                    local_due = datetime.fromisoformat(
                        f"{day.isoformat()}T{hour:02d}:{minute:02d}"
                        f"{'+00:00' if offset == 'Z' else offset}"
                    )
                    ambiguity = "none"
                else:
                    resolved_due, ambiguity = _local_wall_time(day, hour, minute, zone)
                    if resolved_due is None:
                        return ParsedTiming(
                            source_phrase=source_phrase,
                            estimated_effort_minutes=effort,
                            ambiguity=ambiguity,
                        )
                    local_due = resolved_due
                granularity = "minute"
            else:
                if re.match(r"(?:T|\s+(?:at\s+)?\d)", text[iso_match.end() :]):
                    raise ValueError
                local_due = datetime.combine(day, time(23, 59, 59), zone)
                granularity = "date"
        except ValueError:
            return ParsedTiming(
                source_phrase=source_phrase,
                estimated_effort_minutes=effort,
                ambiguity="invalid_explicit_datetime",
            )
        bound = "exclusive" if re.search(r"\bbefore\b", source_phrase, re.I) else "inclusive"
        if granularity == "date" and bound == "exclusive":
            local_due = datetime.combine(day, time(0, 0), zone)
        return ParsedTiming(_utc(local_due), granularity, source_phrase, effort, "none", bound)

    weekday_match = re.search(
        r"\b(before|by|on)?\s*(next\s+)?"
        r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b",
        text,
        re.I,
    )
    relative = re.search(r"\b(today|tomorrow)\b", text, re.I)
    if weekday_match or relative:
        if relative:
            day = local_now.date() + timedelta(days=relative.group(1).lower() == "tomorrow")
            date_match = relative
        else:
            assert weekday_match is not None
            if weekday_match.group(2):
                return ParsedTiming(
                    source_phrase=weekday_match.group(0).strip(),
                    estimated_effort_minutes=effort,
                    ambiguity="ambiguous_next_weekday",
                )
            target = _WEEKDAYS[weekday_match.group(3).lower()]
            delta = (target - local_now.weekday()) % 7
            day = local_now.date() + timedelta(days=delta)
            date_match = weekday_match
        clock_match = re.search(
            r"\b(?:(before|by|at)\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b"
            r"|\b(?:(before|by|at)\s+)?(\d{1,2}):(\d{2})\b",
            text,
            re.I,
        )
        bound = "exclusive" if re.search(r"\bbefore\b", text, re.I) else "inclusive"
        start, end = date_match.span()
        if clock_match:
            start, end = min(start, clock_match.start()), max(end, clock_match.end())
            hour = int(clock_match.group(2) or clock_match.group(6))
            minute = int(clock_match.group(3) or clock_match.group(7) or 0)
            meridian = clock_match.group(4)
            if minute > 59 or (meridian and not 1 <= hour <= 12) or (not meridian and hour > 23):
                return ParsedTiming(
                    source_phrase=text[start:end],
                    estimated_effort_minutes=effort,
                    ambiguity="invalid_explicit_datetime",
                )
            if meridian:
                hour = hour % 12 + (12 if meridian.lower() == "pm" else 0)
            resolved, ambiguity = _local_wall_time(day, hour, minute, zone)
            if resolved is None:
                return ParsedTiming(
                    source_phrase=text[start:end],
                    estimated_effort_minutes=effort,
                    ambiguity=ambiguity,
                )
            return ParsedTiming(_utc(resolved), "minute", text[start:end], effort, "none", bound)
        if re.search(r"\b(?:at|before|by)\s+\d{1,2}(?!\d)|\b\d{1,2}\s*[ap]\.m", text, re.I):
            return ParsedTiming(
                source_phrase=text.strip()[:500],
                estimated_effort_minutes=effort,
                ambiguity="ambiguous_clock",
            )
        local_due = datetime.combine(
            day, time(0, 0) if bound == "exclusive" else time(23, 59, 59), zone
        )
        return ParsedTiming(_utc(local_due), "date", text[start:end].strip(), effort, "none", bound)
    if re.search(r"\b(?:deadline|before|by|next week)\b", text, re.I):
        return ParsedTiming(
            source_phrase=text.strip()[:500],
            estimated_effort_minutes=effort,
            ambiguity="unresolved_deadline",
        )
    return ParsedTiming(estimated_effort_minutes=effort)


def urgency_for(commitment: Commitment, now: datetime) -> Urgency:
    if now.tzinfo is None:
        raise ValueError("Clock values must be timezone-aware")
    due_hours: int | None = None
    slack_hours: int | None = None
    effort = commitment.estimated_effort_minutes
    due_at = commitment.due_at
    if due_at is not None:
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=timezone.utc)
        due_hours = math.floor((due_at - now).total_seconds() / 3600)
        if effort is not None:
            slack_hours = due_hours - math.ceil(effort / 60)
    touched_at = commitment.last_touched_at or commitment.created_at
    inactivity = max(0, (now.date() - touched_at.date()).days)
    blocked = sum(1 for item in (commitment.dependencies or []) if item.get("kind") == "blocked")
    factors: dict[str, int | None] = {
        "deadline_hours": due_hours,
        "effort_minutes": effort,
        "slack_hours": slack_hours,
        "importance": commitment.importance,
        "inactivity_days": inactivity,
        "blocked_dependencies": blocked,
        "availability_minutes": commitment.available_minutes,
    }
    score = min(commitment.importance, 3) * 5 + min(inactivity, 10) + blocked * 5
    if due_hours is not None:
        score += 60 if due_hours <= 0 else 40 if due_hours <= 48 else 20 if due_hours <= 168 else 5
    if slack_hours is not None:
        score += (
            40 if slack_hours <= 0 else 25 if slack_hours <= 48 else 10 if slack_hours <= 168 else 0
        )
    if commitment.available_minutes is not None and effort is not None:
        score += 20 if commitment.available_minutes < effort else 0
    score = min(score, 100)
    bucket = "high" if score >= 65 else "medium" if score >= 35 else "low"
    return Urgency(score, bucket, factors)


def _explanation(title: str, urgency: Urgency) -> str:
    factors = urgency.factors
    parts: list[str] = []
    if factors["deadline_hours"] is not None:
        parts.append(f"the deadline is in {factors['deadline_hours']} hours")
    if factors["effort_minutes"] is not None:
        parts.append(f"estimated effort is {factors['effort_minutes']} minutes")
    if factors["slack_hours"] is not None:
        parts.append(f"schedule slack is {factors['slack_hours']} hours")
    if factors["importance"]:
        parts.append(f"importance is {factors['importance']}/3")
    if factors["inactivity_days"]:
        parts.append(f"it has been inactive for {factors['inactivity_days']} days")
    if factors["blocked_dependencies"]:
        count = factors["blocked_dependencies"]
        parts.append(f"{count} dependenc{'y is' if count == 1 else 'ies are'} blocked")
    if factors["availability_minutes"] is not None:
        parts.append(f"stored availability is {factors['availability_minutes']} minutes")
    if not parts:
        reasons = "its stored next relevant moment has arrived"
    elif len(parts) == 1:
        reasons = parts[0]
    else:
        reasons = ", ".join(parts[:-1]) + ", and " + parts[-1]
    return f"I’m bringing up “{title}” because {reasons}."


class ProspectiveReminderSkill:
    name = "prospective-reminder"
    scan_limit = 100

    def run(self, db: Session, user_id: str, event_id: str, now: datetime) -> int:
        conversation = get_or_create_conversation(db, user_id, created_at=now)
        locked_conversation = db.scalar(
            select(ConversationSession)
            .where(
                ConversationSession.id == conversation.id,
                ConversationSession.user_id == user_id,
            )
            .with_for_update()
        )
        if locked_conversation is None:
            raise ValueError("Unknown conversation")
        conversation = locked_conversation
        base = select(Commitment).where(
            Commitment.user_id == user_id,
            Commitment.deleted.is_(False),
            Commitment.state.in_(("open", "in_progress", "waiting")),
            Commitment.ambiguity == "none",
        )
        cursor_created_at = conversation.reminder_scan_cursor_created_at
        cursor_id = conversation.reminder_scan_cursor_id
        query = base
        if cursor_created_at is not None and cursor_id is not None:
            query = query.where(
                or_(
                    Commitment.created_at > cursor_created_at,
                    and_(
                        Commitment.created_at == cursor_created_at,
                        Commitment.id > cursor_id,
                    ),
                )
            )
        commitments = list(
            db.scalars(
                query.order_by(Commitment.created_at, Commitment.id)
                .limit(self.scan_limit)
                .with_for_update()
            )
        )
        if not commitments and cursor_created_at is not None:
            commitments = list(
                db.scalars(
                    base.order_by(Commitment.created_at, Commitment.id)
                    .limit(self.scan_limit)
                    .with_for_update()
                )
            )
        if commitments:
            conversation.reminder_scan_cursor_created_at = commitments[-1].created_at
            conversation.reminder_scan_cursor_id = commitments[-1].id
        else:
            conversation.reminder_scan_cursor_created_at = None
            conversation.reminder_scan_cursor_id = None
        surfaced = 0
        for commitment in commitments:
            urgency = urgency_for(commitment, now)
            relevant_at = commitment.next_relevant_moment
            if relevant_at is not None and relevant_at.tzinfo is None:
                relevant_at = relevant_at.replace(tzinfo=timezone.utc)
            if urgency.score < 65 and not (relevant_at is not None and relevant_at <= now):
                continue
            due_key = (
                commitment.due_at.date().isoformat()
                if commitment.due_at
                else now.date().isoformat()
            )
            window = f"{urgency.bucket}:{due_key}"
            existing = db.scalar(
                select(Reminder.id).where(
                    Reminder.user_id == user_id,
                    Reminder.commitment_id == commitment.id,
                    Reminder.relevance_window == window,
                    Reminder.policy_version == POLICY_VERSION,
                )
            )
            if existing is not None:
                continue
            if commitment.deleted or commitment.state not in {"open", "in_progress", "waiting"}:
                continue
            metadata = {
                "score": urgency.score,
                "bucket": urgency.bucket,
                "factors": urgency.factors,
            }
            reminder = Reminder(
                user_id=user_id,
                commitment_id=commitment.id,
                thread_id=commitment.related_thread_id,
                policy_version=POLICY_VERSION,
                relevance_window=window,
                reason_code="high_urgency" if urgency.score >= 65 else "relevant_moment",
                reason_metadata=metadata,
                state="pending",
                source_event_id=event_id,
                attempt_count=1,
                created_at=now,
            )
            try:
                with db.begin_nested():
                    db.add(reminder)
                    db.flush()
            except IntegrityError:
                db.expire_all()
                continue
            message = append_message(
                db,
                user_id=user_id,
                conversation_id=conversation.id,
                role="assistant",
                actor="assistant",
                content=_explanation(commitment.title, urgency),
                created_at=now,
            )
            reminder.surfaced_message_id = message.id
            reminder.state = "surfaced"
            commitment.last_surfaced_at = now
            episode = Episode(
                user_id=user_id,
                summary=f"Reminder surfaced for commitment {commitment.id}",
                actor="assistant",
                confidence=1.0,
                derivation=POLICY_VERSION,
                source=f"reminder:{reminder.id}",
                created_at=now,
            )
            db.add(episode)
            db.flush()
            db.add_all(
                [
                    ProvenanceLink(
                        user_id=user_id,
                        target_id=reminder.id,
                        source_type="commitment",
                        source_id=commitment.id,
                        confidence=1.0,
                        derivation=POLICY_VERSION,
                        metadata_only=True,
                        created_at=now,
                    ),
                    ProvenanceLink(
                        user_id=user_id,
                        target_id=episode.id,
                        source_type="reminder",
                        source_id=reminder.id,
                        confidence=1.0,
                        derivation=POLICY_VERSION,
                        metadata_only=True,
                        created_at=now,
                    ),
                ]
            )
            surfaced += 1
        return surfaced
