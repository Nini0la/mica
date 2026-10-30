"""Bounded authoritative current state plus short-term conversation context.

Production includes at most four raw messages strictly after the user's latest
memory reset, so corrections/deletions suppress stale transcripts. Raw content is
request-only, never manifest evidence, and authoritative current state overrides
it. ``recent_only=True`` is an explicit evaluation baseline, NOT a production mode:
it replays at most four raw messages without the production correction cutoff.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, false, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AssistanceFeedback,
    AssistanceRun,
    Commitment,
    ConversationSession,
    Episode,
    EpisodeThread,
    JournalEntry,
    Message,
    ProvenanceLink,
    Responsibility,
    Thread,
    User,
)

POLICY_VERSION = "context-v2"
MAX_RECORDS = 12
MAX_RECORD_CHARS = 1500
CANDIDATES_PER_TYPE = 40
MAX_SOURCES = 3
_STOP_WORDS = frozenset(
    "a an and are as at be been but by can could do for from had has have how i in is it "
    "me my of on or our please should that the their them there these they this to us "
    "was we were what when where which who why will with would you your".split()
)


@dataclass
class ContextBundle:
    payload: dict[str, Any]
    manifest: dict[str, Any]
    related_thread_id: str | None
    related_commitment_id: str | None
    prior_unhelpful: bool


def _size(value: Any) -> int:
    # Match ordinary json.dumps, including escaping and separators, for a safe budget.
    return len(json.dumps(value))


def _truncate(record: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Share the record budget across text fields without truncating references."""
    low, high = 0, MAX_RECORD_CHARS
    while low < high:
        middle = (low + high + 1) // 2
        candidate = {**record, **{key: record[key][:middle] for key in fields}}
        if _size(candidate) <= MAX_RECORD_CHARS:
            low = middle
        else:
            high = middle - 1
    return {**record, **{key: record[key][:low] for key in fields}}


def build_context(
    db: Session,
    user_id: str,
    text: str,
    active_thread_id: str | None = None,
    *,
    max_chars: int = 12000,
    recent_only: bool = False,
    now: datetime | None = None,
    source_id: str | None = None,
) -> ContextBundle:
    """Retrieve deterministic lexical/link context, never revisions or cached state.

    ``text`` is a retrieval query, not copied into context. SQL ranks exact,
    case-insensitive word matches (no stemming/vectors) before candidate limits.
    Manifest character/token counts describe serialized payload, not the manifest.
    Budgets too small for the envelope raise ValueError rather than exceed the cap.
    Recent messages share these budgets and may include the current turn, since
    this builder receives no current message ID to exclude.
    """
    active = None
    if active_thread_id is not None:
        # This must be the first lookup, including for the evaluation baseline.
        active = db.scalar(
            select(Thread)
            .where(
                Thread.user_id == user_id, Thread.id == active_thread_id, Thread.deleted.is_(False)
            )
            .execution_options(populate_existing=True)
        )
        if active is None:
            raise ValueError("Unknown active context")
    if type(max_chars) is not int or max_chars < 2:
        raise ValueError("Context budget too small")

    payload: dict[str, Any] = {"recent_conversation": []}
    if not recent_only:
        profile = db.scalar(
            select(User).where(User.id == user_id).execution_options(populate_existing=True)
        )
        if profile is None:
            raise ValueError("Unknown user")
        payload.update(
            profile=_truncate(
                {
                    "id": profile.id,
                    "name": profile.pet_name,
                    "temperament": profile.temperament,
                },
                ("name", "temperament"),
            ),
            records=[],
            feedback=None,
        )
        if now is not None:
            if now.tzinfo is None:
                raise ValueError("Context clock must be timezone-aware")
            payload["clock"] = {
                "utc": now.astimezone(timezone.utc).isoformat(),
                "local": now.astimezone(ZoneInfo(profile.timezone)).isoformat(),
                "timezone": profile.timezone,
                "checkins_enabled": profile.checkins_enabled,
            }
            from .continuation import conversation_state

            payload["conversation_state"] = conversation_state(db, user_id, now, source_id)
    if _size(payload) > max_chars:
        raise ValueError("Context budget too small")
    manifest: dict[str, Any] = {
        "policy_version": POLICY_VERSION,
        "included": [],
        "excluded": [],
        "limits": {
            "max_chars": max_chars,
            "max_records": MAX_RECORDS,
            "max_record_chars": MAX_RECORD_CHARS,
            "candidates_per_type": CANDIDATES_PER_TYPE,
            "sources_per_record": MAX_SOURCES,
            "recent_messages": 4,
            "query_terms": 24,
            "recent_only": recent_only,
            "conversation_state_max_chars": 5000,
        },
    }
    included = manifest["included"]
    excluded = manifest["excluded"]
    related_thread_id = None
    related_commitment_id = None
    prior_unhelpful = False

    if not recent_only:
        assert profile is not None
        included.append(
            {
                "id": profile.id,
                "type": "profile",
                "version": profile.version,
                "reason": "profile",
                "sources": [],
            }
        )

    # Budget short-term conversation before optional retrieved memory records.
    messages_query = (
        select(Message.id, Message.ordinal, Message.role, Message.content)
        .join(
            ConversationSession,
            ConversationSession.id == Message.conversation_id,
        )
        .where(Message.user_id == user_id, ConversationSession.user_id == user_id)
    )
    if not recent_only and profile is not None and profile.context_reset_at is not None:
        messages_query = messages_query.where(Message.created_at > profile.context_reset_at)
    messages = db.execute(
        messages_query.order_by(
            Message.created_at.desc(), Message.ordinal.desc(), Message.id.desc()
        ).limit(4)
    )
    for message in messages:
        data = _truncate(
            {
                "id": message.id,
                "ordinal": message.ordinal,
                "role": message.role,
                "content": message.content,
            },
            ("content",),
        )
        reference = {"id": message.id, "type": "message"}
        payload["recent_conversation"].insert(0, data)
        if _size(data) > MAX_RECORD_CHARS or _size(payload) > max_chars:
            payload["recent_conversation"].pop(0)
            excluded.append({**reference, "reason": "char_limit"})
        else:
            included.append(
                {
                    **reference,
                    "version": None,
                    "sources": [],
                    "reason": ("evaluation_transcript" if recent_only else "recent_conversation"),
                }
            )

    if not recent_only:
        terms = list(
            dict.fromkeys(
                word for word in re.findall(r"[a-z0-9_]+", text.lower()) if word not in _STOP_WORDS
            )
        )[:24]

        def lexical_score(columns: tuple[Any, ...]) -> Any:
            return (
                sum(
                    (
                        case(
                            (
                                or_(
                                    *(
                                        func.lower(column).regexp_match(
                                            r"(^|[^a-z0-9_])" + re.escape(term) + r"([^a-z0-9_]|$)"
                                        )
                                        for column in columns
                                    )
                                ),
                                1,
                            ),
                            else_=0,
                        )
                        for term in terms
                    ),
                    start=0,
                )
                if terms
                else case((false(), 1), else_=0)
            )

        # Entries carry a link priority, exact lexical score and recency tie-break.
        candidates: list[tuple[Any, str, int, int, str]] = []
        current_ids: set[str] = set()  # Intentions are provided in conversation_state.
        delegation_score = lexical_score(
            (
                Responsibility.recipient_name,
                Responsibility.last_report,
            )
        )
        delegation_query = (
            select(Responsibility.commitment_id)
            .where(
                Responsibility.user_id == user_id,
                Responsibility.state.not_in(("cancelled", "verified"))
                if set(terms) & {"responsibility", "responsibilities", "delegated", "delegation"}
                else delegation_score > 0,
            )
            .order_by(delegation_score.desc(), Responsibility.created_at.desc())
            .limit(20)
        )
        delegated_ids = set(db.scalars(delegation_query))
        thread_score = lexical_score(
            (Thread.title, Thread.description, Thread.why, Thread.next_action)
        )
        active_ids = {active.id} if active else set()
        if active and active.parent_thread_id:
            active_ids.add(active.parent_thread_id)
        # A task can name the relevant project even when the project itself has
        # no matching words. Validate these references through the scoped Thread query.
        task_score = lexical_score((Commitment.title,))
        task_threads = (
            select(Commitment.related_thread_id)
            .where(
                Commitment.user_id == user_id,
                Commitment.deleted.is_(False),
                task_score > 0,
            )
            .order_by(task_score.desc(), Commitment.last_touched_at.desc(), Commitment.id)
        )
        task_threads = task_threads.limit(CANDIDATES_PER_TYPE)
        thread_link = case(
            (Thread.id == active_thread_id, 3),
            (Thread.id.in_(active_ids), 2),
            (Thread.parent_thread_id == active_thread_id, 1) if active else (false(), 1),
            (Thread.id.in_(task_threads), 1),
            else_=0,
        )
        thread_rows = list(
            db.execute(
                select(Thread, thread_link, thread_score)
                .where(
                    Thread.user_id == user_id,
                    Thread.deleted.is_(False),
                    or_(thread_link > 0, thread_score > 0),
                )
                .order_by(
                    thread_link.desc(), thread_score.desc(), Thread.created_at.desc(), Thread.id
                )
                .limit(28)
                .execution_options(populate_existing=True)
            )
        )
        thread_ids = {row[0].id for row in thread_rows}
        # Reserve twelve candidate slots to recover projects/ideas linked to lexical hits.
        seeds = thread_rows[:12]
        parents = {row[0].parent_thread_id for row in seeds if row[0].parent_thread_id}
        expanded = db.execute(
            select(Thread, thread_score)
            .where(
                Thread.user_id == user_id,
                Thread.deleted.is_(False),
                Thread.id.not_in(thread_ids),
                or_(
                    Thread.id.in_(parents),
                    Thread.parent_thread_id.in_([row[0].id for row in seeds]),
                ),
            )
            .order_by(
                case((Thread.id.in_(parents), 1), else_=0).desc(),
                thread_score.desc(),
                Thread.created_at.desc(),
                Thread.id,
            )
            .limit(12)
            .execution_options(populate_existing=True)
        ).all()
        for thread, link, score in thread_rows:
            reason = "active_context" if link == 3 else "thread_link" if link else "lexical_match"
            candidates.append((thread, "thread", link, score, reason))
        for thread, score in expanded:
            candidates.append((thread, "thread", 1, score, "thread_link"))
            thread_ids.add(thread.id)

        journal_content = func.coalesce(
            JournalEntry.corrected_content, JournalEntry.original_content
        )
        episode_link = (
            select(EpisodeThread.episode_id)
            .join(Thread, Thread.id == EpisodeThread.thread_id)
            .where(
                Thread.user_id == user_id,
                Thread.deleted.is_(False),
                Thread.id.in_(thread_ids),
                EpisodeThread.episode_id == Episode.id,
            )
            .exists()
        )
        for model, kind, columns, linked, recency in (
            (
                Commitment,
                "commitment",
                (Commitment.title,),
                or_(
                    Commitment.related_thread_id.in_(thread_ids),
                    Commitment.id.in_(delegated_ids | current_ids),
                ),
                Commitment.last_touched_at,
            ),
            (Episode, "episode", (Episode.summary,), episode_link, Episode.created_at),
            (JournalEntry, "journal", (journal_content,), false(), JournalEntry.updated_at),
        ):
            score = lexical_score(columns)
            link_score = case((linked, 1), else_=0)
            rows = db.execute(
                select(model, link_score, score)
                .where(
                    model.user_id == user_id,
                    model.deleted.is_(False),
                    or_(link_score > 0, score > 0),
                )
                .order_by(link_score.desc(), score.desc(), recency.desc(), model.id)
                .limit(CANDIDATES_PER_TYPE)
                .execution_options(populate_existing=True)
            )
            for record, link, matches in rows:
                candidates.append(
                    (
                        record,
                        kind,
                        link,
                        matches,
                        "current_intention"
                        if kind == "commitment" and record.id in current_ids
                        else "responsibility_match"
                        if kind == "commitment" and record.id in delegated_ids
                        else "thread_link"
                        if link
                        else "lexical_match",
                    )
                )

        def rank(item: tuple[Any, str, int, int, str]) -> tuple[Any, ...]:
            record, kind, link, score, _ = item
            recent = (
                record.last_touched_at
                if kind == "commitment"
                else record.updated_at
                if kind == "journal"
                else record.created_at
            )
            return link, score, recent.isoformat(), record.id

        candidates.sort(key=rank, reverse=True)
        for record, kind, _, _, reason in candidates:
            reference = {"id": record.id, "type": kind}
            # Reserve one slot for relevant feedback; the profile also counts as a record.
            if len(included) >= MAX_RECORDS - 1:
                excluded.append({**reference, "reason": "record_limit"})
                continue
            sources = [
                {"id": source.id, "source_type": source.source_type, "source_id": source.source_id}
                for source in db.scalars(
                    select(ProvenanceLink)
                    .where(
                        ProvenanceLink.user_id == user_id,
                        ProvenanceLink.target_id == record.id,
                    )
                    .order_by(ProvenanceLink.created_at.desc(), ProvenanceLink.id)
                    .limit(MAX_SOURCES)
                )
            ]
            data = {**reference, "version": record.version, "sources": sources}
            fields: tuple[str, ...]
            if kind == "thread":
                data.update(
                    title=record.title,
                    kind=record.kind,
                    state=record.state,
                    epistemic_status=record.epistemic_status,
                    parent_thread_id=(
                        record.parent_thread_id if record.parent_thread_id in thread_ids else None
                    ),
                    why=record.why,
                    what=record.description,
                    how=record.next_action,
                )
                fields = ("title", "why", "what", "how")
            elif kind == "commitment":
                data.update(
                    title=record.title,
                    state=record.state,
                    related_thread_id=(
                        record.related_thread_id if record.related_thread_id in thread_ids else None
                    ),
                    due_at=record.due_at.isoformat() if record.due_at else None,
                    estimated_effort_minutes=record.estimated_effort_minutes,
                    confidence=record.confidence,
                )
                data.update(
                    purpose=record.purpose,
                    completion_criteria=record.completion_criteria,
                    next_action=record.next_action,
                    due_bound=record.due_bound,
                    constraints=[str(value)[:100] for value in (record.constraints or [])[:3]],
                    availability=record.availability,
                    followup_enabled=record.followup_enabled,
                    next_check_at=record.next_relevant_moment.isoformat()
                    if record.next_relevant_moment
                    else None,
                )
                delegated = db.scalar(
                    select(Responsibility).where(
                        Responsibility.user_id == user_id,
                        Responsibility.commitment_id == record.id,
                    )
                )
                if delegated:
                    data["responsibility"] = {
                        "id": delegated.id,
                        "state": delegated.state,
                        "assignee": delegated.recipient_name[:100],
                        "last_report": delegated.last_report[:200],
                        "attention_reason": delegated.attention_reason,
                        "verified_by_owner": delegated.verified_at is not None,
                        "contact_authorized": delegated.approved_at is not None,
                        "next_action_at": delegated.next_action_at.isoformat()
                        if delegated.next_action_at
                        else None,
                        "followups_sent": delegated.followup_count,
                        "max_followups": delegated.max_followups,
                    }
                fields = ("title", "purpose", "completion_criteria", "next_action")
            elif kind == "episode":
                data.update(
                    summary=record.summary, actor=record.actor, confidence=record.confidence
                )
                fields = ("summary",)
            else:
                # Derived summary/structured_state may still describe pre-correction text.
                data.update(
                    content=(
                        record.corrected_content
                        if record.corrected_content is not None
                        else record.original_content
                    ),
                    local_date=str(record.local_date),
                )
                fields = ("content",)
            data = _truncate(data, fields)
            payload["records"].append(data)
            if _size(data) > MAX_RECORD_CHARS or _size(payload) > max_chars:
                payload["records"].pop()
                excluded.append({**reference, "reason": "char_limit"})
                continue
            included.append(
                {**reference, "version": record.version, "reason": reason, "sources": sources}
            )
            if kind == "thread" and related_thread_id is None:
                related_thread_id = record.id
            if kind == "commitment" and related_commitment_id is None:
                related_commitment_id = record.id

        relevant = []
        if related_thread_id:
            relevant.append(AssistanceRun.related_thread_id == related_thread_id)
        if related_commitment_id:
            relevant.append(AssistanceRun.related_commitment_id == related_commitment_id)
        if relevant:
            # Silence is neither success nor failure: retain the latest explicit
            # feedback, but do not force another clarification immediately after one.
            latest_intervention = db.scalar(
                select(AssistanceRun.intervention_type)
                .where(
                    AssistanceRun.user_id == user_id,
                    or_(*relevant),
                )
                .order_by(AssistanceRun.created_at.desc(), AssistanceRun.id.desc())
                .limit(1)
            )
            feedback = db.execute(
                select(AssistanceFeedback, AssistanceRun)
                .join(
                    AssistanceRun,
                    (AssistanceRun.id == AssistanceFeedback.run_id)
                    & (AssistanceRun.user_id == AssistanceFeedback.user_id),
                )
                .where(
                    AssistanceFeedback.user_id == user_id,
                    AssistanceRun.user_id == user_id,
                    or_(*relevant),
                )
                .order_by(AssistanceFeedback.created_at.desc(), AssistanceFeedback.id.desc())
                .limit(1)
                .execution_options(populate_existing=True)
            ).first()
            if feedback:
                verdict, run = feedback
                data = {
                    "id": verdict.id,
                    "run_id": run.id,
                    "verdict": verdict.verdict,
                    "intervention_type": run.intervention_type,
                    "related_thread_id": (
                        related_thread_id if run.related_thread_id == related_thread_id else None
                    ),
                    "related_commitment_id": (
                        related_commitment_id
                        if run.related_commitment_id == related_commitment_id
                        else None
                    ),
                }
                payload["feedback"] = data
                if _size(data) > MAX_RECORD_CHARS or _size(payload) > max_chars:
                    payload["feedback"] = None
                    excluded.append({"id": verdict.id, "type": "feedback", "reason": "char_limit"})
                else:
                    prior_unhelpful = (
                        verdict.verdict == "unhelpful" and latest_intervention != "clarify_support"
                    )
                    included.append(
                        {
                            "id": verdict.id,
                            "type": "feedback",
                            "version": None,
                            "reason": "same_context_feedback",
                            "sources": [],
                        }
                    )

    manifest["chars"] = _size(payload)
    manifest["approximate_tokens"] = manifest["chars"] // 4
    return ContextBundle(
        payload, manifest, related_thread_id, related_commitment_id, prior_unhelpful
    )
