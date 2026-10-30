from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .assistance import contextual_reply
from .clock import Clock, SystemClock
from .daily import EveningReflectionSkill, MorningBriefingSkill
from .gateway import Gateway
from .history import SharedHistoryRecallSkill
from .models import (
    AgentEvent,
    Commitment,
    ConversationSession,
    Episode,
    ProvenanceLink,
    ReflectionPrompt,
    SkillRun,
    Thread,
    User,
    VocabularyItem,
)
from .persistence import MutationResult, append_message, atomic_update
from .personality import REFLECTION_ACKNOWLEDGEMENT
from .prospective import ProspectiveReminderSkill, parse_commitment_timing
from .runtime import (
    DeterministicModel,
    StateExtractor,
    StateProposal,
    is_demonstrative_completion,
)
from .vocabulary import VocabularyLearningSkill


@dataclass(frozen=True)
class RuntimeEvent:
    external_id: str
    kind: str
    content: str | None = None
    conversation_id: str | None = None
    intended_local_date: date | None = None
    intended_at: datetime | None = None
    window_minutes: int | None = None
    active_thread_id: str | None = None
    allow_memory_write: bool = False
    scheduled_action_id: str | None = None
    message_at: datetime | None = None


class IdempotencyConflict(Exception):
    pass


class ConcurrentMutation(Exception):
    pass


class AgentRuntime:
    """The single entry point for reactive and scheduled events."""

    def __init__(
        self,
        model: DeterministicModel | None = None,
        clock: Clock | None = None,
        gateway: Gateway | None = None,
    ) -> None:
        self.extractor = StateExtractor()
        self.model = model or DeterministicModel()
        self.gateway = gateway
        self.clock = clock or SystemClock()
        self.history = SharedHistoryRecallSkill(self.clock)
        self.morning = MorningBriefingSkill()
        self.evening = EveningReflectionSkill()
        self.prospective = ProspectiveReminderSkill()
        self.vocabulary = VocabularyLearningSkill()

    @staticmethod
    def _conversation_target(event: RuntimeEvent) -> str | None:
        return event.conversation_id if event.kind == "user_message" else None

    @staticmethod
    def _canonical_request(user_id: str, event: RuntimeEvent, *, include_target: bool) -> bytes:
        relevant = {
            "content": event.content,
            "intended_local_date": (
                event.intended_local_date.isoformat() if event.intended_local_date else None
            ),
            "intended_at": event.intended_at.isoformat() if event.intended_at else None,
            "window_minutes": event.window_minutes,
            "kind": event.kind,
            "user_id": user_id,
        }
        if include_target:
            relevant["conversation_target"] = AgentRuntime._conversation_target(event)
            # Preserve shipped fingerprints for requests with no new capabilities.
            if event.active_thread_id is not None or event.allow_memory_write:
                relevant["active_thread_id"] = event.active_thread_id
                relevant["allow_memory_write"] = event.allow_memory_write
            if event.scheduled_action_id is not None:
                relevant["scheduled_action_id"] = event.scheduled_action_id
        return json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode()

    @staticmethod
    def _fingerprint(user_id: str, event: RuntimeEvent) -> str:
        digest = hashlib.sha256(
            AgentRuntime._canonical_request(user_id, event, include_target=True)
        ).digest()
        return "v2:" + base64.urlsafe_b64encode(digest).decode().rstrip("=")

    @staticmethod
    def _legacy_fingerprints(user_id: str, event: RuntimeEvent) -> tuple[str, ...]:
        legacy_request = {
            "content": event.content,
            "intended_local_date": (
                event.intended_local_date.isoformat() if event.intended_local_date else None
            ),
            "kind": event.kind,
            "user_id": user_id,
        }
        later_digest = hashlib.sha256(
            json.dumps(legacy_request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if event.intended_local_date is not None:
            return (later_digest,)
        original_request = {
            "content": event.content,
            "kind": event.kind,
            "user_id": user_id,
        }
        original_canonical = json.dumps(
            original_request, sort_keys=True, separators=(",", ":")
        ).encode()
        return (later_digest, hashlib.sha256(original_canonical).hexdigest())

    @staticmethod
    def _idempotent_result(
        db: Session,
        prior: AgentEvent,
        user_id: str,
        event: RuntimeEvent,
        fingerprint: str,
        commit: bool = True,
    ) -> dict[str, Any]:
        if hmac.compare_digest(prior.request_fingerprint, fingerprint):
            return {"idempotent": True, "handled_by": "AgentRuntime.handle"}
        legacy_fingerprint = prior.request_fingerprint
        is_unversioned_sha256 = len(legacy_fingerprint) == 64 and all(
            character in "0123456789abcdef" for character in legacy_fingerprint
        )
        legacy_matches = (
            is_unversioned_sha256
            and not event.allow_memory_write
            and event.active_thread_id is None
            and prior.kind == event.kind
            and any(
                hmac.compare_digest(legacy_fingerprint, candidate)
                for candidate in AgentRuntime._legacy_fingerprints(user_id, event)
            )
        )
        if event.kind == "user_message":
            legacy_matches = (
                legacy_matches
                and event.conversation_id is not None
                and db.scalar(
                    select(ConversationSession.id).where(
                        ConversationSession.id == event.conversation_id,
                        ConversationSession.user_id == user_id,
                    )
                )
                is not None
            )
        if not legacy_matches:
            raise IdempotencyConflict("Idempotency key already used for different input")
        prior.request_fingerprint = fingerprint
        if commit:
            db.commit()
        else:
            db.flush()
        return {"idempotent": True, "handled_by": "AgentRuntime.handle"}

    def handle(
        self, db: Session, user_id: str, event: RuntimeEvent, *, commit: bool = True
    ) -> dict[str, Any]:
        fingerprint = self._fingerprint(user_id, event)
        prior = db.scalar(
            select(AgentEvent).where(
                AgentEvent.user_id == user_id, AgentEvent.external_id == event.external_id
            )
        )
        if prior:
            return self._idempotent_result(db, prior, user_id, event, fingerprint, commit)
        recorded = AgentEvent(
            user_id=user_id,
            external_id=event.external_id,
            kind=event.kind,
            request_fingerprint=fingerprint,
            created_at=self.clock.now(),
        )
        try:
            with db.begin_nested():
                db.add(recorded)
                db.flush()
        except IntegrityError:
            db.expire_all()
            prior = db.scalar(
                select(AgentEvent).where(
                    AgentEvent.user_id == user_id,
                    AgentEvent.external_id == event.external_id,
                )
            )
            if prior is None:
                raise
            return self._idempotent_result(db, prior, user_id, event, fingerprint, commit)
        logical_idempotent = False
        reflection_handled = False
        vocabulary_handled = False
        vocabulary_reply: str | None = None
        vocabulary_reason: str | None = None
        surfaced = 0
        transition_reply: str | None = None
        autonomy_result: dict[str, Any] = {}
        if event.kind == "user_message" and event.content and event.conversation_id:
            user_message = append_message(
                db,
                user_id=user_id,
                conversation_id=event.conversation_id,
                role="user",
                actor="user",
                content=event.content,
                created_at=event.message_at or self.clock.now(),
            )
            reflection_handled = self.gateway is None and self.evening.capture_if_pending(
                db,
                user_id,
                recorded.id,
                user_message.id,
                event.content,
                self.clock.now(),
                self.history.allows_episodic_content(event.content),
            )
            if reflection_handled:
                vocabulary_prompt = self.vocabulary.prompt(
                    db, user_id, recorded.id, self.clock.now(), append=False
                )
                if vocabulary_prompt:
                    vocabulary_reply = REFLECTION_ACKNOWLEDGEMENT + " " + vocabulary_prompt
            elif self.gateway is None:
                vocabulary_handled, vocabulary_reply = self.vocabulary.capture_if_pending(
                    db, user_id, recorded.id, event.content, self.clock.now()
                )
                review_words = event.content.strip().split()
                if (
                    not vocabulary_handled
                    and len(review_words) == 2
                    and review_words[0].casefold() == "review"
                ):
                    vocabulary_reply = self.vocabulary.prompt(
                        db,
                        user_id,
                        recorded.id,
                        self.clock.now(),
                        append=False,
                        target_word=review_words[1],
                    )
                    vocabulary_handled = vocabulary_reply is not None
                if not vocabulary_handled:
                    reusable_items = db.scalars(
                        select(VocabularyItem)
                        .where(
                            VocabularyItem.user_id == user_id,
                            VocabularyItem.deleted.is_(False),
                            or_(
                                VocabularyItem.mastery_state.in_(("familiar", "mastered")),
                                VocabularyItem.last_reviewed_at
                                >= self.clock.now() - timedelta(days=30),
                            ),
                        )
                        .order_by(
                            VocabularyItem.last_reviewed_at.desc().nullslast(),
                            VocabularyItem.id,
                        )
                    ).all()
                    for reusable in reusable_items:
                        vocabulary_reply = self.vocabulary.contextual_reuse(event.content, reusable)
                        if vocabulary_reply is not None:
                            break
            recalling_history = self.gateway is None and self.history.matches(event.content)
            episode_actor = self.history.actor_for(event.content) if self.gateway is None else None
            if self.gateway is None and not recalling_history and episode_actor is not None:
                episode = Episode(
                    user_id=user_id,
                    summary=" ".join(event.content.strip().split()),
                    actor=episode_actor,
                    confidence=0.9,
                    derivation="explicit-chat-episode-v1",
                    source=f"message:{user_message.id}",
                    created_at=self.clock.now(),
                )
                db.add(episode)
                db.flush()
                db.add(
                    ProvenanceLink(
                        user_id=user_id,
                        target_id=episode.id,
                        source_type="message",
                        source_id=user_message.id,
                        confidence=episode.confidence,
                        derivation=episode.derivation,
                    )
                )
            proposal = (
                StateProposal("none")
                if reflection_handled or vocabulary_handled or self.gateway is not None
                else self.extractor.extract(event.content)
            )
            target: Commitment | Thread | None = None
            if proposal.kind == "commitment":
                user = db.get(User, user_id)
                if user is None:
                    raise ValueError("Unknown user")
                timing = parse_commitment_timing(event.content, self.clock.now(), user.timezone)
                target = Commitment(
                    user_id=user_id,
                    title=proposal.title or "",
                    deadline_hint=proposal.deadline_hint,
                    effort_minutes=(
                        None
                        if timing.ambiguity == "effort_out_of_range"
                        else proposal.effort_minutes
                    ),
                    due_at=timing.due_at,
                    due_granularity=timing.due_granularity,
                    due_bound=timing.bound,
                    due_source_text=timing.source_phrase,
                    estimated_effort_minutes=(
                        None
                        if timing.ambiguity == "effort_out_of_range"
                        else timing.estimated_effort_minutes or proposal.effort_minutes
                    ),
                    ambiguity=timing.ambiguity,
                    confidence=proposal.confidence,
                    derivation=proposal.derivation,
                    created_at=self.clock.now(),
                )
            elif proposal.kind == "thread":
                target = Thread(
                    user_id=user_id,
                    title=proposal.title or "",
                    confidence=proposal.confidence,
                    derivation=proposal.derivation,
                    created_at=self.clock.now(),
                )
            if target is not None:
                db.add(target)
                db.flush()
                db.add(
                    ProvenanceLink(
                        user_id=user_id,
                        target_id=target.id,
                        source_type="message",
                        source_id=user_message.id,
                        confidence=proposal.confidence,
                        derivation=proposal.derivation,
                    )
                )
            elif proposal.kind == "temporal_update" and proposal.title:
                temporal_matches = list(
                    db.scalars(
                        select(Commitment).where(
                            Commitment.user_id == user_id,
                            Commitment.deleted.is_(False),
                            Commitment.state.in_(("open", "in_progress", "waiting")),
                            Commitment.title.icontains(proposal.title.casefold(), autoescape=True),
                        )
                    )
                )
                if len(temporal_matches) == 1:
                    temporal_target = temporal_matches[0]
                    outcome = atomic_update(
                        db,
                        Commitment,
                        temporal_target.id,
                        user_id,
                        temporal_target.version,
                        {
                            "estimated_effort_minutes": proposal.effort_minutes,
                            "effort_minutes": proposal.effort_minutes,
                            "last_touched_at": self.clock.now(),
                        },
                    )
                    if outcome is not MutationResult.UPDATED:
                        raise ConcurrentMutation(outcome.value)
                    db.add(
                        ProvenanceLink(
                            user_id=user_id,
                            target_id=temporal_target.id,
                            source_type="message",
                            source_id=user_message.id,
                            confidence=proposal.confidence,
                            derivation="explicit-temporal-update-v1",
                        )
                    )
                    transition_reply = (
                        f"I updated the estimate for “{temporal_target.title}” to "
                        f"{proposal.effort_minutes} minutes."
                    )
                elif len(temporal_matches) > 1:
                    transition_reply = "Which commitment should I apply that estimate to?"
            elif proposal.kind == "transition" and proposal.transition:
                words = {w.lower().strip(".,!?'\"") for w in event.content.split() if len(w) > 3}
                if proposal.transition == "complete":
                    candidates: list[Commitment | Thread] = list(
                        db.scalars(
                            select(Commitment)
                            .where(
                                Commitment.user_id == user_id,
                                Commitment.deleted.is_(False),
                            )
                            .order_by(Commitment.created_at)
                        )
                    )
                else:
                    candidates = list(
                        db.scalars(
                            select(Thread)
                            .where(Thread.user_id == user_id, Thread.deleted.is_(False))
                            .order_by(Thread.created_at)
                        )
                    )
                matches = [
                    item for item in candidates if words & {w.lower() for w in item.title.split()}
                ]
                demonstrative_completion = (
                    proposal.transition == "complete" and is_demonstrative_completion(event.content)
                )
                if demonstrative_completion:
                    surfaced_candidates = [
                        item
                        for item in candidates
                        if isinstance(item, Commitment)
                        and item.state in {"open", "in_progress", "waiting"}
                        and item.last_surfaced_at is not None
                    ]
                    surfaced_candidates.sort(
                        key=lambda item: (item.last_surfaced_at, item.id), reverse=True
                    )
                    matches = []
                    if surfaced_candidates:
                        latest_at = surfaced_candidates[0].last_surfaced_at
                        latest: list[Commitment | Thread] = [
                            item
                            for item in surfaced_candidates
                            if item.last_surfaced_at == latest_at
                        ]
                        if len(latest) == 1:
                            matches = latest
                        else:
                            transition_reply = (
                                "Which commitment did you finish? I surfaced more than one "
                                "at the same time."
                            )
                    else:
                        transition_reply = (
                            "Which commitment did you finish? I don’t have a uniquely recent "
                            "surfaced commitment."
                        )
                if (
                    not demonstrative_completion
                    and not matches
                    and transition_reply is None
                    and "that" in event.content.lower()
                    and candidates
                ):
                    matches = [candidates[-1]]
                if len(matches) > 1:
                    transition_reply = "Which item did you mean?"
                elif matches:
                    target = matches[0]
                    state = "active" if proposal.transition == "reactivate" else proposal.transition
                    state = "abandoned" if state == "abandon" else state
                    changes: dict[str, Any] = {
                        "state": state,
                        **(
                            {"last_touched_at": self.clock.now()}
                            if isinstance(target, Commitment)
                            else {}
                        ),
                    }
                    if isinstance(target, Commitment) and state == "complete":
                        changes["completed_at"] = self.clock.now()
                    outcome = atomic_update(
                        db,
                        type(target),
                        target.id,
                        user_id,
                        target.version,
                        changes,
                    )
                    if outcome is not MutationResult.UPDATED:
                        raise ConcurrentMutation(outcome.value)
                    db.add(
                        ProvenanceLink(
                            user_id=user_id,
                            target_id=target.id,
                            source_type="message",
                            source_id=user_message.id,
                            confidence=proposal.confidence,
                            derivation="explicit-state-transition-v1",
                        )
                    )
                    if isinstance(target, Commitment) and state == "complete":
                        transition_reply = f"Got it — I marked “{target.title}” complete."
            assistance_run = None
            if self.gateway is not None and not reflection_handled and not vocabulary_handled:
                reply, assistance_run = contextual_reply(
                    db,
                    user_id,
                    recorded.id,
                    user_message.id,
                    event.content,
                    gateway=self.gateway,
                    now=self.clock.now(),
                    active_thread_id=event.active_thread_id,
                    allow_write=event.allow_memory_write,
                )
            elif transition_reply:
                reply = transition_reply
            elif vocabulary_reply:
                reply = vocabulary_reply
            elif reflection_handled:
                reply = REFLECTION_ACKNOWLEDGEMENT
            elif recalling_history:
                reply = self.history.answer(
                    db,
                    user_id,
                    event.content,
                    conversation_id=event.conversation_id,
                    current_message_id=user_message.id,
                )
            elif proposal.kind == "clarification":
                reply = "What should I remind you about?"
            elif (
                proposal.kind == "commitment"
                and isinstance(target, Commitment)
                and target.ambiguity == "ambiguous_numeric_date"
            ):
                reply = "I saved that commitment, but is 10/11 October 11 or November 10?"
            elif (
                proposal.kind == "commitment"
                and isinstance(target, Commitment)
                and target.ambiguity == "invalid_explicit_datetime"
            ):
                reply = (
                    f"I saved that commitment, but “{target.due_source_text}” is not a valid "
                    "date or time. What date and time did you mean?"
                )
            elif (
                proposal.kind == "commitment"
                and isinstance(target, Commitment)
                and target.ambiguity == "effort_out_of_range"
            ):
                reply = (
                    "I saved that commitment without an effort estimate. Please give an estimate "
                    "between 1 minute and 7 days."
                )
            elif (
                proposal.kind == "commitment"
                and isinstance(target, Commitment)
                and target.ambiguity in {"nonexistent_local_time", "ambiguous_local_time"}
            ):
                reply = (
                    "I saved that commitment without a deadline because "
                    f"“{target.due_source_text}” "
                    "is ambiguous in your timezone. Please include an explicit UTC offset."
                )
            elif (
                proposal.kind == "commitment"
                and isinstance(target, Commitment)
                and target.ambiguity != "none"
            ):
                reply = (
                    "I saved the intention without guessing a deadline. "
                    "What exact date and time do you mean?"
                )
            else:
                reply = self.model.respond(event.content)
            assistant_message = append_message(
                db,
                user_id=user_id,
                conversation_id=event.conversation_id,
                role="assistant",
                actor="assistant",
                content=reply,
                created_at=self.clock.now(),
            )
            if assistance_run is not None:
                assistance_run.message_id = assistant_message.id
                for prompt in db.scalars(
                    select(ReflectionPrompt).where(
                        ReflectionPrompt.user_id == user_id,
                        ReflectionPrompt.source_event_id == recorded.id,
                        ReflectionPrompt.message_id.is_(None),
                    )
                ):
                    prompt.message_id = assistant_message.id
        elif event.kind == "autonomy_wake" and event.scheduled_action_id:
            from .autonomy import perform

            autonomy_result = perform(
                db,
                user_id,
                recorded.id,
                event.scheduled_action_id,
                self.clock.now(),
                self.morning,
                self.evening,
            )
        elif event.kind in {"morning_briefing", "morning_trigger"}:
            logical_idempotent = not self.morning.run(
                db,
                user_id,
                recorded.id,
                self.clock.now(),
                intended_local_date=event.intended_local_date,
            )
        elif event.kind == "evening_reflection":
            logical_idempotent = not self.evening.start(
                db,
                user_id,
                recorded.id,
                self.clock.now(),
                intended_local_date=event.intended_local_date,
            )
        elif event.kind == "commitment_check":
            surfaced = self.prospective.run(db, user_id, recorded.id, self.clock.now())
        elif event.kind == "vocabulary_review":
            logical_idempotent = (
                self.vocabulary.prompt(db, user_id, recorded.id, self.clock.now()) is None
            )
            if logical_idempotent:
                vocabulary_reason = "no vocabulary review is due"
        db.add(
            SkillRun(
                user_id=user_id,
                event_id=recorded.id,
                skill_name=(
                    "autonomy-dispatch"
                    if event.kind == "autonomy_wake"
                    else self.vocabulary.name
                    if event.kind == "vocabulary_review" or vocabulary_handled
                    else self.evening.name
                    if event.kind == "evening_reflection" or reflection_handled
                    else self.morning.name
                    if event.kind in {"morning_briefing", "morning_trigger"}
                    else self.prospective.name
                    if event.kind == "commitment_check"
                    else "contextual-assistance"
                    if event.kind == "user_message" and self.gateway is not None
                    else "shared-history-recall"
                    if event.kind == "user_message"
                    and event.content
                    and self.history.matches(event.content)
                    else "state-extraction"
                    if event.kind == "user_message"
                    else "scheduled-event"
                ),
                outcome=autonomy_result.get("state")
                or ("duplicate" if logical_idempotent else "handled"),
                created_at=self.clock.now(),
            )
        )
        if commit:
            db.commit()
        else:
            db.flush()
        return {
            "idempotent": logical_idempotent,
            "handled_by": "AgentRuntime.handle",
            **autonomy_result,
            **({"surfaced": surfaced} if event.kind == "commitment_check" else {}),
            **({"reason": vocabulary_reason} if vocabulary_reason else {}),
        }
