from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .persistence import append_message, get_or_create_conversation

POLICY_VERSION = "m2-v1"
BRIEFING_SECTION_LIMIT = 50
REFLECTION_RESPONSE_WINDOW = timedelta(hours=12)


@dataclass(frozen=True)
class Candidate:
    id: str
    kind: str
    title: str
    deadline_hint: str | None
    priority: int
    created_at: datetime
    urgency_score: int | None = None
    last_touched_at: datetime | None = None


@dataclass(frozen=True)
class RankedItem:
    id: str
    kind: str
    title: str
    score: int
    reason: str


def local_date_for(now: datetime, timezone_name: str):
    if now.tzinfo is None:
        raise ValueError("Clock values must be timezone-aware")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError("Unknown user timezone") from error
    return now.astimezone(zone).date()


def rank_power_list(
    candidates: list[Candidate], now: datetime, unfinished_ids: set[str] | None = None
) -> list[RankedItem]:
    unfinished_ids = unfinished_ids or set()
    ranked: list[RankedItem] = []
    for candidate in candidates:
        score = 0
        reasons: list[str] = []
        if candidate.urgency_score is not None:
            score += candidate.urgency_score
            reasons.append(f"current urgency {candidate.urgency_score}/100")
        lower = candidate.title.lower()
        if candidate.deadline_hint:
            score += 40
            reasons.append(f"deadline: {candidate.deadline_hint}")
        if candidate.id in unfinished_ids:
            score += 20
            reasons.append("unfinished yesterday")
        if candidate.priority > 0:
            points = min(candidate.priority, 3) * 10
            score += points
            reasons.append(f"user priority {candidate.priority}")
        if any(word in lower for word in ("important", "critical", "priority", "urgent")):
            score += 25
            reasons.append("explicit importance")
        age_days = max(
            0, (now.date() - (candidate.last_touched_at or candidate.created_at).date()).days
        )
        if age_days:
            points = min(age_days, 15)
            score += points
            reasons.append(f"inactive {age_days} day{'s' if age_days != 1 else ''}")
        if candidate.kind == "commitment" and age_days <= 2:
            score += 20
            reasons.append("recent promise")
        if not reasons:
            reasons.append("active work")
        ranked.append(
            RankedItem(
                candidate.id,
                candidate.kind,
                candidate.title,
                score,
                "; ".join(reasons),
            )
        )
    return sorted(ranked, key=lambda item: (-item.score, item.title.casefold(), item.id))[:3]


def deadline_label(item, timezone_name: str) -> str:
    from .commitments import utc

    if item.due_at:
        local = utc(item.due_at).astimezone(ZoneInfo(timezone_name))
        relation = "before" if item.due_bound == "exclusive" else "by"
        return f"{relation} {local.isoformat()}"
    return "needs clarification" if item.ambiguity != "none" else ""


def current_power_list(commitments, threads, user, now, unfinished_ids=None):
    from .prospective import urgency_for

    candidates = [
        Candidate(
            item.id,
            "commitment",
            item.title,
            deadline_label(item, user.timezone) or None,
            item.priority,
            item.created_at,
            urgency_for(item, now).score,
            item.last_touched_at,
        )
        for item in commitments
    ] + [
        Candidate(item.id, "thread", item.title, None, item.priority, item.created_at)
        for item in threads
    ]
    return [item.__dict__ for item in rank_power_list(candidates, now, unfinished_ids)]


class MorningBriefingSkill:
    """Small deterministic policy over inspectable current state."""

    name = "morning-briefing"

    def run(
        self,
        db: Session,
        user_id: str,
        event_id: str,
        now: datetime,
        intended_local_date: date | None = None,
    ) -> bool:
        from .models import (
            Commitment,
            ConversationSession,
            DailyBriefing,
            Episode,
            JournalEntry,
            ProvenanceLink,
            Thread,
            User,
        )

        user = db.get(User, user_id)
        if user is None:
            raise ValueError("Unknown user")
        local_date = intended_local_date or local_date_for(now, user.timezone)
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
        existing = db.scalar(
            select(DailyBriefing).where(
                DailyBriefing.user_id == user_id,
                DailyBriefing.local_date == local_date,
                DailyBriefing.policy_version == POLICY_VERSION,
            )
        )
        if existing is not None:
            return False

        commitments = list(
            db.scalars(
                select(Commitment)
                .where(
                    Commitment.user_id == user_id,
                    Commitment.deleted.is_(False),
                    Commitment.state.in_(("open", "in_progress", "waiting")),
                )
                .order_by(Commitment.created_at, Commitment.id)
            )
        )
        threads = list(
            db.scalars(
                select(Thread)
                .where(
                    Thread.user_id == user_id,
                    Thread.deleted.is_(False),
                    Thread.state == "active",
                )
                .order_by(Thread.created_at, Thread.id)
            )
        )
        yesterday = db.scalar(
            select(JournalEntry).where(
                JournalEntry.user_id == user_id,
                JournalEntry.local_date == local_date - timedelta(days=1),
            )
        )
        unfinished_ids = set(
            (yesterday.structured_state or {}).get("unfinished_ids", []) if yesterday else []
        )
        open_ids = {item.id for item in commitments}
        unfinished_ids &= open_ids
        power_list = current_power_list(commitments, threads, user, now, unfinished_ids)
        sections: dict[str, Any] = {}
        from .vocabulary import VocabularyLearningSkill

        vocabulary = VocabularyLearningSkill().morning_section(
            db, user_id, event_id, now, local_date
        )
        if vocabulary:
            sections["vocabulary"] = vocabulary
        collection_meta: dict[str, dict[str, int | bool]] = {}
        if commitments:
            sections["open_commitments"] = [
                {
                    "id": item.id,
                    "title": item.title,
                    "deadline": deadline_label(item, user.timezone),
                }
                for item in commitments[:BRIEFING_SECTION_LIMIT]
            ]
            collection_meta["open_commitments"] = {
                "limit": BRIEFING_SECTION_LIMIT,
                "returned_count": len(sections["open_commitments"]),
                "total_count": len(commitments),
                "has_more": len(commitments) > BRIEFING_SECTION_LIMIT,
            }
        if unfinished_ids:
            sections["yesterday_unfinished"] = [
                {"id": item.id, "title": item.title}
                for item in commitments
                if item.id in unfinished_ids
            ][:BRIEFING_SECTION_LIMIT]
        if threads:
            sections["active_threads"] = [
                {"id": item.id, "title": item.title} for item in threads[:BRIEFING_SECTION_LIMIT]
            ]
            collection_meta["active_threads"] = {
                "limit": BRIEFING_SECTION_LIMIT,
                "returned_count": len(sections["active_threads"]),
                "total_count": len(threads),
                "has_more": len(threads) > BRIEFING_SECTION_LIMIT,
            }
        recent = list(
            db.scalars(
                select(Episode)
                .where(
                    Episode.user_id == user_id,
                    Episode.deleted.is_(False),
                    Episode.created_at >= now - timedelta(days=1),
                )
                .order_by(Episode.created_at)
            )
        )
        if recent:
            sections["recent_activity"] = [
                {"id": item.id, "summary": item.summary}
                for item in recent[-BRIEFING_SECTION_LIMIT:]
            ]
        if collection_meta:
            sections["collection_meta"] = collection_meta

        lines = ["Good morning."]
        if vocabulary:
            if vocabulary["mode"] == "new":
                lines.append(
                    f"Word of the Day: {vocabulary['word']} — {vocabulary['definition']} "
                    f"Example: {vocabulary['example']} ({vocabulary['reason']})"
                )
            elif vocabulary["mode"] == "review":
                lines.append(
                    f"Due vocabulary review: {vocabulary['word']} ({vocabulary['reason']})."
                )
        if commitments:
            lines.append("Open commitments: " + "; ".join(item.title for item in commitments[:5]))
        if unfinished_ids:
            lines.append(
                "Unfinished yesterday: "
                + "; ".join(
                    item.title
                    for item in commitments[:BRIEFING_SECTION_LIMIT]
                    if item.id in unfinished_ids
                )
            )
        if threads:
            lines.append("Active threads: " + "; ".join(item.title for item in threads[:5]))
        if recent:
            lines.append("Recent: " + "; ".join(item.summary for item in recent[-3:]))
        if power_list:
            lines.append("Power List:")
            lines.extend(
                f"{index}. {item['title']} — {item['reason']}"
                for index, item in enumerate(power_list, 1)
            )
        content = "\n".join(lines)

        briefing = DailyBriefing(
            user_id=user_id,
            local_date=local_date,
            policy_version=POLICY_VERSION,
            content=content,
            sections=sections,
            power_list=power_list,
            source_event_id=event_id,
            created_at=now,
        )
        try:
            with db.begin_nested():
                db.add(briefing)
                db.flush()
        except IntegrityError:
            db.expire_all()
            return False

        message = append_message(
            db,
            user_id=user_id,
            conversation_id=conversation.id,
            role="assistant",
            actor="assistant",
            content=content,
            created_at=now,
        )
        briefing.message_id = message.id
        episode = Episode(
            user_id=user_id,
            summary=f"Morning briefing generated for {local_date.isoformat()}",
            actor="assistant",
            confidence=1.0,
            derivation=POLICY_VERSION,
            source=f"event:{event_id}",
            created_at=now,
        )
        db.add(episode)
        db.flush()
        db.add_all(
            [
                ProvenanceLink(
                    user_id=user_id,
                    target_id=briefing.id,
                    source_type="agent_event",
                    source_id=event_id,
                    confidence=1.0,
                    derivation=POLICY_VERSION,
                    metadata_only=True,
                    created_at=now,
                ),
                ProvenanceLink(
                    user_id=user_id,
                    target_id=episode.id,
                    source_type="briefing",
                    source_id=briefing.id,
                    confidence=1.0,
                    derivation=POLICY_VERSION,
                    created_at=now,
                ),
            ]
        )
        return True


class EveningReflectionSkill:
    name = "evening-reflection"
    checkin = "How did things go today?"

    def start(
        self,
        db: Session,
        user_id: str,
        event_id: str,
        now: datetime,
        intended_local_date: date | None = None,
        *,
        append: bool = True,
    ) -> bool:
        from .models import (
            Episode,
            ProvenanceLink,
            ReflectionPrompt,
            User,
        )

        user = db.get(User, user_id)
        if user is None:
            raise ValueError("Unknown user")
        local_date = intended_local_date or local_date_for(now, user.timezone)
        existing = db.scalar(
            select(ReflectionPrompt).where(
                ReflectionPrompt.user_id == user_id,
                ReflectionPrompt.local_date == local_date,
                ReflectionPrompt.policy_version == POLICY_VERSION,
            )
        )
        if existing is not None:
            return False
        prompt = ReflectionPrompt(
            user_id=user_id,
            local_date=local_date,
            policy_version=POLICY_VERSION,
            source_event_id=event_id,
            created_at=now,
        )
        try:
            with db.begin_nested():
                db.add(prompt)
                db.flush()
        except IntegrityError:
            db.expire_all()
            return False
        conversation = get_or_create_conversation(db, user_id, created_at=now)
        if append:
            message = append_message(
                db,
                user_id=user_id,
                conversation_id=conversation.id,
                role="assistant",
                actor="assistant",
                content=self.checkin,
                created_at=now,
            )
            prompt.message_id = message.id
        episode = Episode(
            user_id=user_id,
            summary=f"Evening reflection started for {local_date.isoformat()}",
            actor="assistant",
            confidence=1.0,
            derivation=POLICY_VERSION,
            source=f"event:{event_id}",
            created_at=now,
        )
        db.add(episode)
        db.flush()
        db.add_all(
            [
                ProvenanceLink(
                    user_id=user_id,
                    target_id=prompt.id,
                    source_type="agent_event",
                    source_id=event_id,
                    confidence=1.0,
                    derivation=POLICY_VERSION,
                    metadata_only=True,
                    created_at=now,
                ),
                ProvenanceLink(
                    user_id=user_id,
                    target_id=episode.id,
                    source_type="reflection_prompt",
                    source_id=prompt.id,
                    confidence=1.0,
                    derivation=POLICY_VERSION,
                    created_at=now,
                ),
            ]
        )
        return True

    @staticmethod
    def _normalized_subject(value: str) -> str:
        value = value.casefold().replace("’", "'")
        value = re.sub(r"[^\w\s'-]", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
        value = re.sub(r"^(?:to\s+)?(?:finish|complete)\s+", "", value)
        value = re.sub(r"^(?:prepare|preparing)\s+", "", value)
        return re.sub(r"^(?:the|a|an|my)\s+", "", value).strip()

    @classmethod
    def _matching_item(cls, items, phrase: str):
        subject = cls._normalized_subject(phrase)
        matches = [item for item in items if cls._normalized_subject(item.title) == subject]
        return matches[0] if len(matches) == 1 else None

    @classmethod
    def _mentioned_items(cls, items, clause: str):
        normalized_clause = f" {cls._normalized_subject(clause)} "
        matches = []
        for item in items:
            subject = cls._normalized_subject(item.title)
            if subject and f" {subject} " in normalized_clause:
                matches.append(item)
        return matches

    def capture_if_pending(
        self,
        db: Session,
        user_id: str,
        event_id: str,
        message_id: str,
        content: str,
        now: datetime,
        allow_episode: bool,
    ) -> bool:
        from .commitments import invalidate_and_replan, revision
        from .models import (
            Commitment,
            Episode,
            JournalEntry,
            ProvenanceLink,
            ReflectionPrompt,
            Responsibility,
            Thread,
            User,
        )

        user = db.get(User, user_id)
        if user is None:
            return False
        prompt = db.scalar(
            select(ReflectionPrompt)
            .where(
                ReflectionPrompt.user_id == user_id,
                ReflectionPrompt.policy_version == POLICY_VERSION,
                ReflectionPrompt.status == "awaiting",
                ReflectionPrompt.created_at >= now - REFLECTION_RESPONSE_WINDOW,
            )
            .order_by(ReflectionPrompt.created_at.desc(), ReflectionPrompt.id.desc())
            .with_for_update()
        )
        if prompt is None:
            return False
        local_date = prompt.local_date
        prior = db.scalar(
            select(JournalEntry).where(
                JournalEntry.user_id == user_id, JournalEntry.local_date == local_date
            )
        )
        if prior is not None:
            prompt.status = "completed"
            prompt.journal_entry_id = prior.id
            return False

        commitments = list(
            db.scalars(
                select(Commitment).where(
                    Commitment.user_id == user_id,
                    Commitment.deleted.is_(False),
                    Commitment.id.not_in(
                        select(Responsibility.commitment_id).where(
                            Responsibility.user_id == user_id
                        )
                    ),
                )
            )
        )
        threads = list(
            db.scalars(select(Thread).where(Thread.user_id == user_id, Thread.deleted.is_(False)))
        )
        state: dict[str, list] = {
            "completed_ids": [],
            "unfinished_ids": [],
            "priority_ids": [],
            "new_commitment_ids": [],
            "new_thread_ids": [],
            "blockers": [],
            "outcomes": [],
        }
        supported: list[str] = []
        clauses = [
            item.strip(" \t\r\n.")
            for item in re.split(r"(?<=[.!?])\s+|\n+|\s+but\s+", content, flags=re.I)
            if item.strip()
        ]
        prior_subject = None
        touched: dict[str, Commitment] = {}

        def before_change(item):
            if isinstance(item, Commitment):
                revision(db, item, "before_reflection", message_id)
                touched[item.id] = item

        for clause in clauses:
            lower = clause.casefold().replace("’", "'")
            mentioned = self._mentioned_items([*commitments, *threads], clause)
            unfinished = re.match(
                r"^(?:i\s+)?(?:didn't|did not|haven't|have not)\s+"
                r"(?:finish(?:ed)?|touch)\s+(.+)$",
                lower,
            )
            if unfinished:
                item = self._matching_item(commitments, unfinished.group(1))
                if item is not None:
                    before_change(item)
                    item.last_touched_at = now
                    item.version += 1
                    state["unfinished_ids"].append(item.id)
                    supported.append(f"unfinished: {item.title}")
                prior_subject = mentioned[0] if len(mentioned) == 1 else None
                continue
            completed = re.match(r"^i(?:'ve| have)?\s+(?:finished|completed)\s+(.+)$", lower)
            if completed:
                phrase = completed.group(1)
                item = (
                    prior_subject
                    if self._normalized_subject(phrase) == "it" and prior_subject in commitments
                    else self._matching_item(commitments, phrase)
                )
                if item is not None:
                    before_change(item)
                    item.state = "complete"
                    item.completed_at = now
                    item.last_touched_at = now
                    item.version += 1
                    state["completed_ids"].append(item.id)
                    supported.append(f"completed: {item.title}")
                prior_subject = mentioned[0] if len(mentioned) == 1 else None
                continue
            priority_match = re.match(r"^(.+?)\s+is now (?:my )?(?:top )?priority$", lower)
            if priority_match:
                item = self._matching_item([*commitments, *threads], priority_match.group(1))
                if item is not None:
                    before_change(item)
                    item.priority = 3
                    if isinstance(item, Commitment):
                        item.last_touched_at = now
                    item.version += 1
                    state["priority_ids"].append(item.id)
                    supported.append(f"priority: {item.title}")
                prior_subject = mentioned[0] if len(mentioned) == 1 else None
                continue
            commitment_match = re.match(r"^i need to (.+)$", clause, re.I)
            if commitment_match:
                title = commitment_match.group(1).strip()
                deadline = None
                deadline_match = re.search(r"\s+before\s+(.+)$", title, re.I)
                if deadline_match:
                    deadline = deadline_match.group(1).strip()
                    title = title[: deadline_match.start()].strip()
                from .prospective import parse_commitment_timing

                timing = parse_commitment_timing(clause, now, user.timezone)
                item = Commitment(
                    user_id=user_id,
                    title=title,
                    deadline_hint=deadline,
                    due_at=timing.due_at,
                    due_source_text=timing.source_phrase,
                    due_granularity=timing.due_granularity,
                    ambiguity=timing.ambiguity,
                    due_bound=timing.bound if timing.due_at else "unspecified",
                    confidence=1.0,
                    derivation="evening-reflection-v1",
                    created_at=now,
                )
                db.add(item)
                db.flush()
                commitments.append(item)
                revision(db, item, "reflection", message_id)
                state["new_commitment_ids"].append(item.id)
                supported.append(f"new commitment: {title}")
                prior_subject = item
                continue
            thread_match = re.match(r"^i(?:'m| am) considering (.+)$", clause, re.I)
            if thread_match:
                title = thread_match.group(1).strip()
                item = Thread(
                    user_id=user_id,
                    title=title,
                    confidence=1.0,
                    derivation="evening-reflection-v1",
                    created_at=now,
                )
                db.add(item)
                db.flush()
                threads.append(item)
                state["new_thread_ids"].append(item.id)
                supported.append(f"new thread: {title}")
                prior_subject = item
                continue
            blocker = re.match(r"^blocked (?:by|because of) (.+)$", clause, re.I)
            if blocker:
                state["blockers"].append(blocker.group(1).strip())
                supported.append(f"blocker: {blocker.group(1).strip()}")
                prior_subject = None
                continue
            outcome = re.match(r"^outcome:\s*(.+)$", clause, re.I)
            if outcome:
                state["outcomes"].append(outcome.group(1).strip())
                supported.append(f"outcome: {outcome.group(1).strip()}")
            prior_subject = mentioned[0] if len(mentioned) == 1 else None

        for item in touched.values():
            item.followup_count = 0
            invalidate_and_replan(db, item, now)
            revision(db, item, "reflection", message_id)
        if touched:
            user.context_reset_at = now
        summary = "; ".join(supported) if supported else None
        journal = JournalEntry(
            user_id=user_id,
            local_date=local_date,
            original_content=content,
            summary=summary,
            structured_state=state,
            source_message_id=message_id,
            source_event_id=event_id,
            created_at=now,
            updated_at=now,
        )
        db.add(journal)
        db.flush()
        prompt.status = "completed"
        prompt.journal_entry_id = journal.id
        links = [
            ProvenanceLink(
                user_id=user_id,
                target_id=journal.id,
                source_type="message",
                source_id=message_id,
                confidence=1.0,
                derivation="verbatim-evening-reflection-v1",
                created_at=now,
            )
        ]
        for target_id in (
            state["completed_ids"]
            + state["unfinished_ids"]
            + state["priority_ids"]
            + state["new_commitment_ids"]
            + state["new_thread_ids"]
        ):
            links.append(
                ProvenanceLink(
                    user_id=user_id,
                    target_id=target_id,
                    source_type="journal_entry",
                    source_id=journal.id,
                    confidence=1.0,
                    derivation="evening-reflection-v1",
                    created_at=now,
                )
            )
        if supported and allow_episode:
            episode = Episode(
                user_id=user_id,
                summary=summary or "",
                actor="user",
                confidence=1.0,
                derivation="evening-reflection-v1",
                source=f"message:{message_id}",
                created_at=now,
            )
            db.add(episode)
            db.flush()
            links.append(
                ProvenanceLink(
                    user_id=user_id,
                    target_id=episode.id,
                    source_type="journal_entry",
                    source_id=journal.id,
                    confidence=1.0,
                    derivation="evening-reflection-v1",
                    created_at=now,
                )
            )
        db.add_all(links)
        return True
