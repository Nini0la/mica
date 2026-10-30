from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.agent import AgentRuntime, RuntimeEvent
from app.clock import FakeClock
from app.daily import Candidate, local_date_for, rank_power_list
from app.models import (
    AgentEvent,
    Base,
    Commitment,
    ConversationSession,
    DailyBriefing,
    Episode,
    JournalEntry,
    Message,
    ProvenanceLink,
    SkillRun,
    Thread,
    User,
)


def test_local_date_uses_zoneinfo_across_dst_boundary():
    before_midnight = datetime(2026, 3, 8, 4, 30, tzinfo=timezone.utc)
    after_midnight = datetime(2026, 3, 8, 5, 30, tzinfo=timezone.utc)

    assert local_date_for(before_midnight, "America/New_York") == date(2026, 3, 7)
    assert local_date_for(after_midnight, "America/New_York") == date(2026, 3, 8)


def test_power_list_ranking_is_deterministic_interpretable_and_bounded():
    now = datetime(2026, 9, 30, 8, tzinfo=timezone.utc)
    candidates = [
        Candidate(
            "thread-old",
            "thread",
            "Important dormant design",
            None,
            0,
            datetime(2026, 9, 1, tzinfo=timezone.utc),
        ),
        Candidate(
            "promise",
            "commitment",
            "Send launch plan",
            None,
            0,
            datetime(2026, 9, 29, tzinfo=timezone.utc),
        ),
        Candidate(
            "deadline",
            "commitment",
            "Submit demo",
            "Friday",
            0,
            datetime(2026, 9, 20, tzinfo=timezone.utc),
        ),
        Candidate(
            "priority",
            "thread",
            "Critical launch architecture",
            None,
            3,
            datetime(2026, 9, 30, tzinfo=timezone.utc),
        ),
    ]

    ranked = rank_power_list(candidates, now, unfinished_ids={"deadline"})

    assert [item.id for item in ranked] == ["deadline", "priority", "thread-old"]
    assert len(ranked) == 3
    assert "deadline" in ranked[0].reason
    assert "unfinished yesterday" in ranked[0].reason
    assert all(item.reason for item in ranked)


def make_sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_morning_event_persists_one_structured_briefing_in_the_same_chat():
    sessions = make_sessions()
    clock = FakeClock(datetime(2026, 9, 30, 15, tzinfo=timezone.utc))
    with sessions.begin() as db:
        user = User(username="owner", timezone="America/Los_Angeles")
        db.add(user)
        db.flush()
        conversation = ConversationSession(user_id=user.id)
        db.add_all(
            [
                conversation,
                Commitment(
                    user_id=user.id,
                    title="Submit demo",
                    deadline_hint="Friday",
                    confidence=1,
                    derivation="test",
                ),
                Thread(
                    user_id=user.id,
                    title="Explore local inference",
                    confidence=1,
                    derivation="test",
                ),
            ]
        )
        db.flush()
        user_id, conversation_id = user.id, conversation.id

    runtime = AgentRuntime(clock=clock)
    with sessions() as db:
        first = runtime.handle(db, user_id, RuntimeEvent("morning-1", "morning_briefing"))
    with sessions() as db:
        repeated = runtime.handle(db, user_id, RuntimeEvent("morning-2", "morning_briefing"))

    assert first["idempotent"] is False
    assert repeated["idempotent"] is True
    with sessions() as db:
        briefing = db.scalar(select(DailyBriefing))
        assert briefing.local_date == date(2026, 9, 30)
        assert len(briefing.power_list) == 2
        assert briefing.power_list[0]["title"] == "Submit demo"
        # Legacy free-text hints are not authoritative normalized deadlines.
        assert "current urgency" in briefing.power_list[0]["reason"]
        assert "deadline" not in briefing.power_list[0]["reason"]
        assert briefing.sections["vocabulary"]["word"] == "inchoate"
        messages = db.scalars(select(Message).order_by(Message.created_at, Message.id)).all()
        assert len(messages) == 1
        assert messages[0].conversation_id == conversation_id
        assert messages[0].role == "assistant"
        assert "Power List" in messages[0].content
        assert db.scalar(select(func.count()).select_from(AgentEvent)) == 2
        assert db.scalar(select(func.count()).select_from(SkillRun)) == 2
        assert db.scalar(select(func.count()).select_from(Episode)) == 1
        assert db.scalar(select(func.count()).select_from(ProvenanceLink)) >= 2


def test_persisted_briefing_sections_are_stably_bounded_with_counts():
    sessions = make_sessions()
    now = datetime(2026, 9, 30, 8, tzinfo=timezone.utc)
    clock = FakeClock(now)
    with sessions.begin() as db:
        user = User(username="owner", timezone="UTC")
        db.add(user)
        db.flush()
        db.add_all(
            [
                Commitment(
                    user_id=user.id,
                    title=f"commitment {index:03d}",
                    confidence=1,
                    derivation="test",
                    created_at=now + timedelta(seconds=index),
                )
                for index in range(150)
            ]
            + [
                Thread(
                    user_id=user.id,
                    title=f"thread {index:03d}",
                    confidence=1,
                    derivation="test",
                    created_at=now + timedelta(seconds=index),
                )
                for index in range(150)
            ]
        )
        user_id = user.id

    with sessions() as db:
        AgentRuntime(clock=clock).handle(
            db, user_id, RuntimeEvent("bounded-morning", "morning_briefing")
        )

    with sessions() as db:
        briefing = db.scalar(select(DailyBriefing))
        assert [item["title"] for item in briefing.sections["open_commitments"]] == [
            f"commitment {index:03d}" for index in range(50)
        ]
        assert [item["title"] for item in briefing.sections["active_threads"]] == [
            f"thread {index:03d}" for index in range(50)
        ]
        assert briefing.sections["collection_meta"] == {
            "open_commitments": {
                "limit": 50,
                "returned_count": 50,
                "total_count": 150,
                "has_more": True,
            },
            "active_threads": {
                "limit": 50,
                "returned_count": 50,
                "total_count": 150,
                "has_more": True,
            },
        }


def test_evening_checkin_and_free_form_answer_close_the_loop_verbatim():
    sessions = make_sessions()
    clock = FakeClock(datetime(2026, 9, 30, 23, tzinfo=timezone.utc))
    with sessions.begin() as db:
        user = User(username="owner", timezone="UTC")
        db.add(user)
        db.flush()
        conversation = ConversationSession(user_id=user.id)
        complete = Commitment(
            user_id=user.id,
            title="submit demo",
            confidence=1,
            derivation="test",
        )
        unfinished = Commitment(
            user_id=user.id,
            title="write docs",
            confidence=1,
            derivation="test",
        )
        priority = Thread(
            user_id=user.id,
            title="launch polish",
            confidence=1,
            derivation="test",
        )
        db.add_all([conversation, complete, unfinished, priority])
        db.flush()
        user_id, conversation_id = user.id, conversation.id
        complete_id, unfinished_id, priority_id = complete.id, unfinished.id, priority.id

    runtime = AgentRuntime(clock=clock)
    with sessions() as db:
        started = runtime.handle(db, user_id, RuntimeEvent("evening", "evening_reflection"))
    clock.set(datetime(2026, 9, 30, 23, 1, tzinfo=timezone.utc))
    reflection = (
        "I finished submit demo.\n"
        "I didn't finish write docs. Launch polish is now top priority. "
        "I need to email Sam before Friday. I'm considering observability. "
        "Blocked by API access."
    )
    with sessions() as db:
        answered = runtime.handle(
            db,
            user_id,
            RuntimeEvent("answer", "user_message", reflection, conversation_id),
        )

    assert started["idempotent"] is False
    assert answered["idempotent"] is False
    with sessions() as db:
        messages = db.scalars(select(Message).order_by(Message.created_at, Message.id)).all()
        assert messages[0].content == "How did things go today?"
        journal = db.scalar(select(JournalEntry))
        assert journal.original_content == reflection
        assert journal.local_date == date(2026, 9, 30)
        source_message = next(item for item in messages if item.content == reflection)
        assert journal.source_message_id == source_message.id
        assert complete_id in journal.structured_state["completed_ids"]
        assert unfinished_id in journal.structured_state["unfinished_ids"]
        assert db.get(Commitment, complete_id).state == "complete"
        assert db.get(Commitment, complete_id).completed_at.replace(
            tzinfo=timezone.utc
        ) == clock.now()
        assert db.get(Commitment, unfinished_id).state == "open"
        assert db.get(Thread, priority_id).priority == 3
        assert any(item.title == "email Sam" for item in db.scalars(select(Commitment)))
        assert any(item.title == "observability" for item in db.scalars(select(Thread)))
        reflection_episodes = db.scalars(
            select(Episode).where(Episode.derivation == "evening-reflection-v1")
        ).all()
        assert reflection_episodes
        assert all(
            item.source == f"message:{journal.source_message_id}"
            for item in reflection_episodes
        )


def test_evening_clauses_complete_pronoun_subject_and_retain_contrasted_unfinished_work():
    sessions = make_sessions()
    clock = FakeClock(datetime(2026, 9, 30, 23, tzinfo=timezone.utc))
    with sessions.begin() as db:
        user = User(username="owner", timezone="UTC")
        db.add(user)
        db.flush()
        conversation = ConversationSession(user_id=user.id)
        profiling = Commitment(
            user_id=user.id, title="profiling", confidence=1, derivation="test"
        )
        portfolio = Commitment(
            user_id=user.id, title="portfolio", confidence=1, derivation="test"
        )
        db.add_all([conversation, profiling, portfolio])
        db.flush()
        user_id, conversation_id = user.id, conversation.id
        profiling_id, portfolio_id = profiling.id, portfolio.id

    runtime = AgentRuntime(clock=clock)
    with sessions() as db:
        runtime.handle(db, user_id, RuntimeEvent("evening", "evening_reflection"))
    with sessions() as db:
        runtime.handle(
            db,
            user_id,
            RuntimeEvent(
                "answer",
                "user_message",
                "Profiling took forever. I finished it but didn’t touch the portfolio.",
                conversation_id,
            ),
        )

    with sessions() as db:
        journal = db.scalar(select(JournalEntry))
        assert journal.structured_state["completed_ids"] == [profiling_id]
        assert journal.structured_state["unfinished_ids"] == [portfolio_id]
        assert db.get(Commitment, profiling_id).state == "complete"
        assert db.get(Commitment, portfolio_id).state == "open"


def test_evening_completion_normalizes_action_prefix_and_article():
    sessions = make_sessions()
    clock = FakeClock(datetime(2026, 9, 30, 20, tzinfo=timezone.utc))
    with sessions.begin() as db:
        user = User(username="owner", timezone="UTC")
        db.add(user)
        db.flush()
        conversation = ConversationSession(user_id=user.id)
        db.add(conversation)
        db.flush()
        user_id, conversation_id = user.id, conversation.id

    runtime = AgentRuntime(clock=clock)
    with sessions() as db:
        runtime.handle(
            db,
            user_id,
            RuntimeEvent(
                "commitment",
                "user_message",
                "I need to finish the frontend",
                conversation_id,
            ),
        )
    with sessions() as db:
        commitment_id = db.scalar(select(Commitment.id))
    with sessions() as db:
        runtime.handle(db, user_id, RuntimeEvent("evening", "evening_reflection"))
    with sessions() as db:
        runtime.handle(
            db,
            user_id,
            RuntimeEvent("answer", "user_message", "I finished the frontend.", conversation_id),
        )

    with sessions() as db:
        journal = db.scalar(select(JournalEntry))
        assert journal.structured_state["completed_ids"] == [commitment_id]
        assert db.get(Commitment, commitment_id).state == "complete"


def test_evening_ambiguous_pronoun_makes_no_structured_mutation():
    sessions = make_sessions()
    clock = FakeClock(datetime(2026, 9, 30, 20, tzinfo=timezone.utc))
    with sessions.begin() as db:
        user = User(username="owner", timezone="UTC")
        db.add(user)
        db.flush()
        conversation = ConversationSession(user_id=user.id)
        first = Commitment(user_id=user.id, title="profiling", confidence=1, derivation="test")
        second = Commitment(user_id=user.id, title="portfolio", confidence=1, derivation="test")
        db.add_all([conversation, first, second])
        db.flush()
        user_id, conversation_id = user.id, conversation.id

    runtime = AgentRuntime(clock=clock)
    with sessions() as db:
        runtime.handle(db, user_id, RuntimeEvent("evening", "evening_reflection"))
    with sessions() as db:
        runtime.handle(
            db,
            user_id,
            RuntimeEvent(
                "answer",
                "user_message",
                "Profiling and portfolio took forever. I finished it.",
                conversation_id,
            ),
        )

    with sessions() as db:
        journal = db.scalar(select(JournalEntry))
        assert journal.structured_state["completed_ids"] == []
        assert all(item.state == "open" for item in db.scalars(select(Commitment)).all())


def test_evening_answer_after_midnight_uses_outstanding_prompt_local_date():
    sessions = make_sessions()
    clock = FakeClock(datetime(2026, 9, 30, 23, 58, tzinfo=timezone.utc))
    with sessions.begin() as db:
        user = User(username="owner", timezone="UTC")
        db.add(user)
        db.flush()
        conversation = ConversationSession(user_id=user.id)
        db.add(conversation)
        db.flush()
        user_id, conversation_id = user.id, conversation.id

    runtime = AgentRuntime(clock=clock)
    with sessions() as db:
        runtime.handle(db, user_id, RuntimeEvent("evening", "evening_reflection"))
    clock.set(datetime(2026, 10, 1, 0, 3, tzinfo=timezone.utc))
    with sessions() as db:
        runtime.handle(
            db,
            user_id,
            RuntimeEvent("after-midnight", "user_message", "A quiet day.", conversation_id),
        )

    with sessions() as db:
        journal = db.scalar(select(JournalEntry))
        assert journal.local_date == date(2026, 9, 30)


def test_evening_answer_across_dst_uses_most_recent_outstanding_prompt_date():
    sessions = make_sessions()
    clock = FakeClock(datetime(2026, 3, 8, 4, 55, tzinfo=timezone.utc))
    with sessions.begin() as db:
        user = User(username="owner", timezone="America/New_York")
        db.add(user)
        db.flush()
        conversation = ConversationSession(user_id=user.id)
        db.add(conversation)
        db.flush()
        user_id, conversation_id = user.id, conversation.id

    runtime = AgentRuntime(clock=clock)
    with sessions() as db:
        runtime.handle(db, user_id, RuntimeEvent("dst-evening", "evening_reflection"))
    clock.set(datetime(2026, 3, 8, 7, 5, tzinfo=timezone.utc))
    with sessions() as db:
        runtime.handle(
            db,
            user_id,
            RuntimeEvent("dst-answer", "user_message", "Made progress.", conversation_id),
        )

    with sessions() as db:
        journal = db.scalar(select(JournalEntry))
        assert journal.local_date == date(2026, 3, 7)


def test_next_morning_excludes_completed_and_boosts_yesterday_unfinished():
    sessions = make_sessions()
    clock = FakeClock(datetime(2026, 9, 30, 20, tzinfo=timezone.utc))
    with sessions.begin() as db:
        user = User(username="owner", timezone="UTC")
        db.add(user)
        db.flush()
        conversation = ConversationSession(user_id=user.id)
        done = Commitment(user_id=user.id, title="ship demo", confidence=1, derivation="test")
        open_item = Commitment(user_id=user.id, title="write docs", confidence=1, derivation="test")
        db.add_all([conversation, done, open_item])
        db.flush()
        user_id, conversation_id = user.id, conversation.id
        done_id, open_id = done.id, open_item.id

    runtime = AgentRuntime(clock=clock)
    with sessions() as db:
        runtime.handle(db, user_id, RuntimeEvent("evening", "evening_reflection"))
    clock.set(datetime(2026, 9, 30, 20, 5, tzinfo=timezone.utc))
    with sessions() as db:
        runtime.handle(
            db,
            user_id,
            RuntimeEvent(
                "answer",
                "user_message",
                "I finished ship demo. I didn't finish write docs.",
                conversation_id,
            ),
        )
    clock.set(datetime(2026, 10, 1, 8, tzinfo=timezone.utc))
    with sessions() as db:
        runtime.handle(db, user_id, RuntimeEvent("next-morning", "morning_briefing"))

    with sessions() as db:
        briefing = db.scalar(
            select(DailyBriefing).where(DailyBriefing.local_date == date(2026, 10, 1))
        )
        ids = [item["id"] for item in briefing.power_list]
        assert done_id not in ids
        assert open_id in ids
        reason = next(item["reason"] for item in briefing.power_list if item["id"] == open_id)
        assert "unfinished yesterday" in reason
        assert briefing.sections["yesterday_unfinished"][0]["id"] == open_id
