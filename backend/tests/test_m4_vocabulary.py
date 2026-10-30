from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.agent import AgentRuntime, RuntimeEvent
from app.clock import FakeClock
from app.models import (
    AgentEvent,
    Base,
    DailyBriefing,
    Message,
    ProvenanceLink,
    User,
    VocabularyAttempt,
    VocabularyItem,
)


def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def owner(db, *, timezone_name="UTC"):
    user = User(username="vocab-owner", timezone=timezone_name)
    db.add(user)
    db.flush()
    return user.id


def test_day_one_morning_introduces_versioned_inchoate_in_existing_briefing():
    factory = sessions()
    clock = FakeClock(datetime(2026, 10, 1, 8, tzinfo=timezone.utc))
    with factory.begin() as db:
        user_id = owner(db)

    with factory() as db:
        AgentRuntime(clock=clock).handle(db, user_id, RuntimeEvent("m1", "morning_briefing"))

    with factory() as db:
        item = db.scalar(select(VocabularyItem))
        briefing = db.scalar(select(DailyBriefing))
        messages = db.scalars(select(Message)).all()
        assert item.normalized_word == "inchoate"
        assert item.display_word == "inchoate"
        assert item.definition == "only partly in existence; imperfectly formed"
        assert item.source == "continuity-curated-lexicon"
        assert item.policy_version == "vocab-v1"
        assert item.mastery_state == "new"
        assert item.next_review_at.replace(tzinfo=timezone.utc) == clock.now() + timedelta(days=1)
        assert briefing.sections["vocabulary"]["mode"] == "new"
        assert briefing.sections["vocabulary"]["word"] == "inchoate"
        assert "Word of the Day: inchoate" in messages[0].content
        assert len(messages) == 1


def test_ordinary_review_before_due_is_explained_without_any_mutation():
    factory = sessions()
    clock = FakeClock(datetime(2026, 10, 1, 8, tzinfo=timezone.utc))
    with factory.begin() as db:
        user_id = owner(db)
    with factory() as db:
        runtime = AgentRuntime(clock=clock)
        runtime.handle(db, user_id, RuntimeEvent("morning", "morning_briefing"))

    clock.set(datetime(2026, 10, 1, 20, tzinfo=timezone.utc))
    with factory() as db:
        before = db.scalar(select(VocabularyItem))
        snapshot = (
            before.recall_attempts,
            before.correct_recalls,
            before.mastery_state,
            before.next_review_at,
            before.version,
        )
        result = AgentRuntime(clock=clock).handle(
            db, user_id, RuntimeEvent("early-review", "vocabulary_review")
        )

    assert result["reason"] == "no vocabulary review is due"
    with factory() as db:
        after = db.scalar(select(VocabularyItem))
        assert (
            after.recall_attempts,
            after.correct_recalls,
            after.mastery_state,
            after.next_review_at,
            after.version,
        ) == snapshot
        assert db.scalars(select(VocabularyAttempt)).all() == []
        assert len(db.scalars(select(Message)).all()) == 1


def test_weak_due_item_is_reviewed_before_introducing_another_word():
    factory = sessions()
    clock = FakeClock(datetime(2026, 10, 1, 8, tzinfo=timezone.utc))
    with factory.begin() as db:
        user_id = owner(db)
    with factory() as db:
        runtime = AgentRuntime(clock=clock)
        runtime.handle(db, user_id, RuntimeEvent("day1", "morning_briefing"))
    clock.set(datetime(2026, 10, 2, 8, tzinfo=timezone.utc))
    with factory() as db:
        AgentRuntime(clock=clock).handle(db, user_id, RuntimeEvent("day2", "morning_briefing"))
    with factory() as db:
        items = db.scalars(select(VocabularyItem)).all()
        latest = db.scalars(select(DailyBriefing).order_by(DailyBriefing.local_date.desc())).first()
        assert len(items) == 1
        assert latest.sections["vocabulary"]["mode"] == "review"
        assert latest.sections["vocabulary"]["reason"] == "weak word due for review"


def test_review_answer_is_graded_and_sentence_retained_with_interpretable_schedule():
    factory = sessions()
    clock = FakeClock(datetime(2026, 10, 1, 8, tzinfo=timezone.utc))
    with factory.begin() as db:
        user_id = owner(db)
    with factory() as db:
        runtime = AgentRuntime(clock=clock)
        runtime.handle(db, user_id, RuntimeEvent("morning", "morning_briefing"))
    clock.set(datetime(2026, 10, 1, 20, tzinfo=timezone.utc))
    with factory() as db:
        runtime = AgentRuntime(clock=clock)
        conversation_id = db.scalar(select(Message.conversation_id))
        runtime.handle(
            db,
            user_id,
            RuntimeEvent("review", "user_message", "review inchoate", conversation_id),
        )
        runtime.handle(
            db,
            user_id,
            RuntimeEvent(
                "answer",
                "user_message",
                "only partly in existence; imperfectly formed. "
                "Sentence: The plan remained inchoate.",
                conversation_id,
            ),
        )
    with factory() as db:
        item = db.scalar(select(VocabularyItem))
        attempt = db.scalar(select(VocabularyAttempt))
        assert attempt.correct is True
        assert attempt.user_sentence == "The plan remained inchoate."
        assert item.recall_attempts == 1
        assert item.correct_recalls == 1
        assert item.user_sentences == ["The plan remained inchoate."]
        assert item.mastery_state == "developing"
        assert item.next_review_at.replace(tzinfo=timezone.utc) == clock.now() + timedelta(days=2)


def test_ambiguous_answer_is_ungraded_and_requests_clarification():
    factory = sessions()
    clock = FakeClock(datetime(2026, 10, 1, 8, tzinfo=timezone.utc))
    with factory.begin() as db:
        user_id = owner(db)
    with factory() as db:
        runtime = AgentRuntime(clock=clock)
        runtime.handle(db, user_id, RuntimeEvent("morning", "morning_briefing"))
        conversation_id = db.scalar(select(Message.conversation_id))
        runtime.handle(
            db,
            user_id,
            RuntimeEvent("review", "user_message", "review inchoate", conversation_id),
        )
        runtime.handle(
            db,
            user_id,
            RuntimeEvent(
                "ambiguous", "user_message", "It is sort of beginning-ish", conversation_id
            ),
        )
    with factory() as db:
        attempt = db.scalar(select(VocabularyAttempt))
        assert attempt.correct is None
        assert db.scalars(select(Message).order_by(Message.ordinal)).all()[-1].content.startswith(
            "I’m not certain"
        )


def test_ambiguous_answer_can_be_explicitly_self_corrected():
    factory = sessions()
    clock = FakeClock(datetime(2026, 10, 1, 8, tzinfo=timezone.utc))
    with factory.begin() as db:
        user_id = owner(db)
    with factory() as db:
        runtime = AgentRuntime(clock=clock)
        runtime.handle(db, user_id, RuntimeEvent("morning", "morning_briefing"))
        conversation_id = db.scalar(select(Message.conversation_id))
        runtime.handle(
            db,
            user_id,
            RuntimeEvent("review", "user_message", "review inchoate", conversation_id),
        )
        runtime.handle(
            db, user_id, RuntimeEvent("unclear", "user_message", "beginning-ish", conversation_id)
        )
        runtime.handle(
            db,
            user_id,
            RuntimeEvent(
                "correction",
                "user_message",
                "self-correction: only partly in existence; imperfectly formed",
                conversation_id,
            ),
        )
    with factory() as db:
        item = db.scalar(select(VocabularyItem))
        attempts = db.scalars(select(VocabularyAttempt)).all()
        assert len(attempts) == 1
        assert attempts[0].correct is True
        assert item.correct_recalls == 1


def test_explicit_self_correction_repairs_latest_incorrect_attempt_once_with_provenance():
    factory = sessions()
    clock = FakeClock(datetime(2026, 10, 1, 8, tzinfo=timezone.utc))
    with factory.begin() as db:
        user_id = owner(db)
    runtime = AgentRuntime(clock=clock)
    with factory() as db:
        runtime.handle(db, user_id, RuntimeEvent("morning", "morning_briefing"))
        conversation_id = db.scalar(select(Message.conversation_id))
        runtime.handle(
            db,
            user_id,
            RuntimeEvent("revisit", "user_message", "review inchoate", conversation_id),
        )
        runtime.handle(
            db,
            user_id,
            RuntimeEvent("wrong", "user_message", "I don't know", conversation_id),
        )

    clock.set(datetime(2026, 10, 1, 21, tzinfo=timezone.utc))
    correction = "self-correction: only partly in existence; imperfectly formed"
    with factory() as db:
        runtime.handle(
            db,
            user_id,
            RuntimeEvent("graded-correction", "user_message", correction, conversation_id),
        )
        runtime.handle(
            db,
            user_id,
            RuntimeEvent("graded-correction-repeat", "user_message", correction, conversation_id),
        )

    with factory() as db:
        item = db.scalar(select(VocabularyItem))
        attempt = db.scalar(select(VocabularyAttempt))
        correction_event = db.scalar(
            select(AgentEvent).where(AgentEvent.external_id == "graded-correction")
        )
        provenance = db.scalar(
            select(ProvenanceLink).where(
                ProvenanceLink.target_id == attempt.id,
                ProvenanceLink.source_id == correction_event.id,
                ProvenanceLink.derivation == "explicit-vocabulary-correction-v1",
            )
        )
        assert attempt.user_answer == "I don't know"
        assert attempt.correct is True
        assert item.recall_attempts == 1
        assert item.correct_recalls == 1
        assert item.mastery_state == "developing"
        assert item.next_review_at.replace(tzinfo=timezone.utc) == clock.now() + timedelta(days=2)
        assert item.version == 3
        assert provenance is not None and provenance.metadata_only is True


def test_explicit_self_correction_does_not_rewrite_attempt_outside_bounded_window():
    factory = sessions()
    clock = FakeClock(datetime(2026, 10, 1, 8, tzinfo=timezone.utc))
    with factory.begin() as db:
        user_id = owner(db)
    runtime = AgentRuntime(clock=clock)
    with factory() as db:
        runtime.handle(db, user_id, RuntimeEvent("morning", "morning_briefing"))
        conversation_id = db.scalar(select(Message.conversation_id))
        runtime.handle(
            db,
            user_id,
            RuntimeEvent("revisit", "user_message", "review inchoate", conversation_id),
        )
        runtime.handle(
            db, user_id, RuntimeEvent("wrong", "user_message", "I don't know", conversation_id)
        )

    clock.set(datetime(2026, 10, 2, 9, tzinfo=timezone.utc))
    with factory() as db:
        runtime.handle(
            db,
            user_id,
            RuntimeEvent(
                "late-correction",
                "user_message",
                "self-correction: only partly in existence; imperfectly formed",
                conversation_id,
            ),
        )

    with factory() as db:
        item = db.scalar(select(VocabularyItem))
        attempt = db.scalar(select(VocabularyAttempt))
        messages = db.scalars(select(Message).order_by(Message.ordinal)).all()
        assert attempt.user_answer == "I don't know"
        assert attempt.correct is False
        assert (item.recall_attempts, item.correct_recalls, item.version) == (1, 0, 2)
        assert messages[-1].content == "I couldn’t find a recent vocabulary attempt to correct."


@pytest.mark.parametrize(
    "content",
    [
        "It is not a rough idea anymore.",
        "Unlike the early draft, it is now fully formed.",
        "The design is not fully formed anymore.",
    ],
)
def test_negated_or_contrast_context_does_not_force_inchoate_reuse(content):
    factory = sessions()
    clock = FakeClock(datetime(2026, 10, 8, 12, tzinfo=timezone.utc))
    with factory.begin() as db:
        user_id = owner(db)
    with factory() as db:
        runtime = AgentRuntime(clock=clock)
        runtime.handle(db, user_id, RuntimeEvent("morning", "morning_briefing"))
        item = db.scalar(select(VocabularyItem))
        item.mastery_state = "familiar"
        item.last_reviewed_at = clock.now() - timedelta(days=1)
        conversation_id = db.scalar(select(Message.conversation_id))
        runtime.handle(
            db,
            user_id,
            RuntimeEvent("context", "user_message", content, conversation_id),
        )

    with factory() as db:
        reply = db.scalars(select(Message).order_by(Message.ordinal)).all()[-1].content
        assert "inchoate" not in reply.casefold()


@pytest.mark.parametrize(
    "content",
    [
        "The design is still an early draft.",
        "The design is still a rough idea.",
        "The design is not fully formed.",
    ],
)
def test_positive_context_searches_all_eligible_items_not_only_latest_reviewed(content):
    factory = sessions()
    clock = FakeClock(datetime(2026, 10, 8, 12, tzinfo=timezone.utc))
    with factory.begin() as db:
        user_id = owner(db)
    with factory() as db:
        runtime = AgentRuntime(clock=clock)
        runtime.handle(db, user_id, RuntimeEvent("morning", "morning_briefing"))
        inchoate = db.scalar(select(VocabularyItem))
        inchoate.mastery_state = "familiar"
        inchoate.last_reviewed_at = clock.now() - timedelta(days=1)
        db.add(
            VocabularyItem(
                user_id=user_id,
                normalized_word="lucid",
                display_word="lucid",
                definition="expressed clearly; easy to understand",
                natural_example="Her lucid explanation made the idea approachable.",
                introduced_at=clock.now(),
                introduced_local_date=clock.now().date(),
                recall_attempts=2,
                correct_recalls=2,
                user_sentences=[],
                exposure_count=1,
                last_reviewed_at=clock.now(),
                mastery_state="familiar",
                next_review_at=clock.now() + timedelta(days=7),
                source="continuity-curated-lexicon",
                source_version="2026-10-01",
                policy_version="vocab-v1",
                updated_at=clock.now(),
            )
        )
        conversation_id = db.scalar(select(Message.conversation_id))
        runtime.handle(
            db,
            user_id,
            RuntimeEvent(
                "day-seven-context",
                "user_message",
                content,
                conversation_id,
            ),
        )

    with factory() as db:
        reply = db.scalars(select(Message).order_by(Message.ordinal)).all()[-1].content
        assert reply == "That sounds inchoate — still only partly formed."
        assert reply.casefold().count("inchoate") == 1


def test_evening_reflection_finishes_naturally_then_asks_active_recall_in_same_chat():
    factory = sessions()
    clock = FakeClock(datetime(2026, 10, 1, 8, tzinfo=timezone.utc))
    with factory.begin() as db:
        user_id = owner(db)
    runtime = AgentRuntime(clock=clock)
    with factory() as db:
        runtime.handle(db, user_id, RuntimeEvent("morning", "morning_briefing"))
    clock.set(datetime(2026, 10, 2, 20, tzinfo=timezone.utc))
    with factory() as db:
        runtime.handle(db, user_id, RuntimeEvent("evening", "evening_reflection"))
        conversation_id = db.scalar(select(Message.conversation_id))
        runtime.handle(
            db,
            user_id,
            RuntimeEvent("reflection", "user_message", "A calm day overall.", conversation_id),
        )
    with factory() as db:
        messages = db.scalars(select(Message).order_by(Message.ordinal)).all()
        assert "saved your reflection" in messages[-1].content
        assert "what does “inchoate” mean?" in messages[-1].content
        assert len({message.conversation_id for message in messages}) == 1


def test_mastery_retires_word_and_allows_next_curated_word():
    factory = sessions()
    clock = FakeClock(datetime(2026, 10, 1, 8, tzinfo=timezone.utc))
    with factory.begin() as db:
        user_id = owner(db)
    runtime = AgentRuntime(clock=clock)
    with factory() as db:
        runtime.handle(db, user_id, RuntimeEvent("morning", "morning_briefing"))
        conversation_id = db.scalar(select(Message.conversation_id))
    for index, day in enumerate((2, 4, 11, 18), start=1):
        clock.set(datetime(2026, 10, day, 20, tzinfo=timezone.utc))
        with factory() as db:
            runtime.handle(db, user_id, RuntimeEvent(f"review-{index}", "vocabulary_review"))
            runtime.handle(
                db,
                user_id,
                RuntimeEvent(
                    f"answer-{index}",
                    "user_message",
                    "only partly in existence; imperfectly formed",
                    conversation_id,
                ),
            )
    with factory() as db:
        inchoate = db.scalar(
            select(VocabularyItem).where(VocabularyItem.normalized_word == "inchoate")
        )
        assert inchoate.mastery_state == "mastered"
        assert inchoate.next_review_at is None

    clock.set(datetime(2026, 10, 18, 21, tzinfo=timezone.utc))
    with factory() as db:
        runtime.handle(
            db, user_id, RuntimeEvent("revisit", "user_message", "review inchoate", conversation_id)
        )
        runtime.handle(
            db, user_id, RuntimeEvent("lapse", "user_message", "I don't know", conversation_id)
        )
    with factory() as db:
        inchoate = db.scalar(
            select(VocabularyItem).where(VocabularyItem.normalized_word == "inchoate")
        )
        assert inchoate.mastery_state == "developing"

    clock.set(datetime(2026, 10, 19, 8, tzinfo=timezone.utc))
    with factory() as db:
        runtime.handle(db, user_id, RuntimeEvent("next-word", "morning_briefing"))
    with factory() as db:
        words = set(db.scalars(select(VocabularyItem.normalized_word)))
        assert words == {"inchoate", "lucid"}
