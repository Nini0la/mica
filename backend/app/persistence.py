from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import AssistanceRun, Commitment, ConversationSession, Message, User, utcnow


class MutationResult(Enum):
    UPDATED = "updated"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"


def invalidate_journal_episodes(db: Session, user_id: str, journal_id: str) -> None:
    from .models import Episode, ProvenanceLink

    derived = select(ProvenanceLink.target_id).where(
        ProvenanceLink.user_id == user_id,
        ProvenanceLink.source_type == "journal_entry",
        ProvenanceLink.source_id == journal_id,
    )
    db.execute(
        update(Episode)
        .where(Episode.user_id == user_id, Episode.id.in_(derived))
        .values(summary="[source corrected or deleted]", deleted=True, version=Episode.version + 1)
    )


def get_or_create_conversation(
    db: Session, user_id: str, *, created_at: datetime | None = None
) -> ConversationSession:
    """Return the user's sole conversation, tolerating a concurrent creator."""
    conversation = db.scalar(
        select(ConversationSession).where(ConversationSession.user_id == user_id)
    )
    if conversation is not None:
        return conversation

    candidate = ConversationSession(user_id=user_id, created_at=created_at)
    try:
        with db.begin_nested():
            db.add(candidate)
            db.flush()
        return candidate
    except IntegrityError:
        db.expire_all()
        conversation = db.scalar(
            select(ConversationSession).where(ConversationSession.user_id == user_id)
        )
        if conversation is None:
            raise
        return conversation


def append_message(
    db: Session,
    *,
    user_id: str,
    conversation_id: str,
    role: str,
    actor: str,
    content: str,
    created_at: datetime,
) -> Message:
    """Append under a tenant-scoped conversation row lock."""
    conversation = db.scalar(
        select(ConversationSession)
        .where(
            ConversationSession.id == conversation_id,
            ConversationSession.user_id == user_id,
        )
        .with_for_update()
    )
    if conversation is None:
        raise ValueError("Unknown conversation")
    current = db.scalar(
        select(func.max(Message.ordinal)).where(
            Message.conversation_id == conversation_id,
            Message.user_id == user_id,
        )
    )
    message = Message(
        user_id=user_id,
        conversation_id=conversation_id,
        role=role,
        actor=actor,
        content=content,
        ordinal=(current or 0) + 1,
        created_at=created_at,
    )
    db.add(message)
    db.flush()
    conversation.last_activity_at = created_at
    return message


def atomic_update(
    db: Session,
    model: Any,
    record_id: str,
    user_id: str,
    expected_version: int,
    values: dict[str, Any],
) -> MutationResult:
    """Tenant-scoped compare-and-swap for mutable owned records."""
    if model is Commitment:
        from .commitments import revision

        before = db.scalar(
            select(Commitment)
            .where(
                Commitment.id == record_id,
                Commitment.user_id == user_id,
                Commitment.deleted.is_(False),
                Commitment.version == expected_version,
            )
            .with_for_update()
        )
        if before is not None and not values.get("deleted"):
            revision(db, before, "before_change", None)
    result = cast(
        CursorResult[Any],
        db.execute(
            update(model)
            .where(
                model.id == record_id,
                model.user_id == user_id,
                model.version == expected_version,
                model.deleted.is_(False),
            )
            .values(**values, version=expected_version + 1)
            .execution_options(synchronize_session=False)
        ),
    )
    if result.rowcount == 1:
        # Old chat may repeat corrected or deleted memory. Only later messages may
        # enter subsequent model context; the visible transcript stays unchanged.
        db.execute(
            update(User)
            .where(User.id == user_id)
            .values(context_reset_at=values.get("last_touched_at") or utcnow())
        )
        if values.get("deleted") is True:
            from .models import ConversationProposal

            db.execute(
                update(ConversationProposal)
                .where(ConversationProposal.user_id == user_id)
                .values(arguments={}, title="[cleared by memory deletion]")
            )
            db.execute(
                update(ConversationProposal)
                .where(
                    ConversationProposal.user_id == user_id,
                    ConversationProposal.state.in_(["proposed", "needs_clarification", "paused"]),
                )
                .values(state="cancelled", version=ConversationProposal.version + 1)
            )
            # Raw prompts can contain source chat as well as derived records; purge all
            # of this tenant's snapshots rather than risk incomplete dependency matching.
            db.execute(
                update(AssistanceRun)
                .where(
                    AssistanceRun.user_id == user_id,
                    AssistanceRun.trace_status == "captured",
                )
                .values(context_trace=None, trace_status="cleared_by_memory_deletion")
            )
        db.expire_all()
        if model is Commitment:
            from .commitments import invalidate_and_replan

            current = db.get(Commitment, record_id)
            if current is not None:
                invalidate_and_replan(db, current, values.get("last_touched_at") or utcnow())
                db.flush()
                if not current.deleted:
                    revision(db, current, "state_update", None)
        return MutationResult.UPDATED
    exists = db.scalar(
        select(model.id).where(
            model.id == record_id,
            model.user_id == user_id,
            model.deleted.is_(False),
        )
    )
    return MutationResult.CONFLICT if exists is not None else MutationResult.NOT_FOUND
