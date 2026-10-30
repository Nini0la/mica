from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.agent import AgentRuntime, RuntimeEvent
from app.clock import FakeClock
from app.models import (
    AgentEvent,
    Base,
    Commitment,
    ConversationSession,
    Episode,
    Message,
    ProvenanceLink,
    Reminder,
    SkillRun,
    User,
)
from app.prospective import _explanation, parse_commitment_timing, urgency_for
from app.runtime import StateExtractor


def make_sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_commitment_temporal_fields_and_reminder_relationships_exist():
    now = datetime(2026, 9, 28, 9, tzinfo=timezone.utc)
    commitment = Commitment(
        user_id="owner",
        title="evaluation",
        confidence=0.8,
        derivation="test",
        due_at=now,
        due_granularity="date",
        due_source_text="Friday",
        estimated_effort_minutes=180,
        importance=2,
        next_relevant_moment=now - timedelta(hours=1),
        ambiguity="none",
        dependencies=[{"kind": "blocked", "label": "API access"}],
    )
    reminder = Reminder(
        user_id="owner",
        commitment_id="commitment",
        policy_version="m3-v1",
        relevance_window="2026-09-28",
        reason_code="high_urgency",
        reason_metadata={"score": 80, "bucket": "high"},
        state="pending",
    )

    assert commitment.estimated_effort_minutes == 180
    assert commitment.completed_at is None
    assert reminder.surfaced_message_id is None
    assert reminder.attempt_count == 0


def test_parse_explicit_dates_effort_and_ambiguity_in_user_timezone():
    now = datetime(2026, 9, 28, 14, tzinfo=timezone.utc)  # Monday, 07:00 PDT
    friday = parse_commitment_timing(
        "Send Sarah the document before Friday", now, "America/Los_Angeles"
    )
    next_friday = parse_commitment_timing(
        "I need to submit the evaluation next Friday", now, "America/Los_Angeles"
    )
    effort = parse_commitment_timing(
        "The evaluation should take about three hours", now, "America/Los_Angeles"
    )
    iso = parse_commitment_timing(
        "Submit report by 2026-10-02 15:30", now, "America/Los_Angeles"
    )
    ambiguous = parse_commitment_timing(
        "Submit report by 10/11", now, "America/Los_Angeles"
    )

    assert friday.due_at == datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc)
    assert friday.bound == "exclusive"
    assert friday.due_granularity == "date"
    assert friday.source_phrase == "before Friday"
    assert next_friday.due_at is None  # "next" may mean this Friday or the following one
    assert next_friday.ambiguity != "none"
    assert effort.estimated_effort_minutes == 180
    assert iso.due_at == datetime(2026, 10, 2, 22, 30, tzinfo=timezone.utc)
    assert iso.due_granularity == "minute"
    assert ambiguous.due_at is None
    assert ambiguous.ambiguity == "ambiguous_numeric_date"


def test_invalid_recognized_iso_date_or_clock_is_retained_for_clarification():
    now = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)
    for text, source in [
        ("Submit report by 2026-02-30", "by 2026-02-30"),
        ("Submit report by 2026-10-02 24:00", "by 2026-10-02 24:00"),
        ("Submit report by 2026-10-02 23:60", "by 2026-10-02 23:60"),
    ]:
        parsed = parse_commitment_timing(text, now, "UTC")
        assert parsed.due_at is None
        assert parsed.due_granularity is None
        assert parsed.source_phrase == source
        assert parsed.ambiguity == "invalid_explicit_datetime"


def test_dst_gap_and_fold_wall_times_require_clarification_but_offsets_are_explicit():
    now = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)

    gap = parse_commitment_timing(
        "Submit report by 2026-03-08 02:30", now, "America/New_York"
    )
    fold = parse_commitment_timing(
        "Submit report by 2026-11-01 01:30", now, "America/New_York"
    )
    daylight = parse_commitment_timing(
        "Submit report by 2026-11-01 01:30-04:00", now, "America/New_York"
    )
    standard = parse_commitment_timing(
        "Submit report by 2026-11-01 01:30-05:00", now, "America/New_York"
    )

    assert gap.due_at is None
    assert gap.ambiguity == "nonexistent_local_time"
    assert fold.due_at is None
    assert fold.ambiguity == "ambiguous_local_time"
    assert daylight.due_at == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    assert standard.due_at == datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)


def test_urgency_increases_as_slack_shrinks_and_reasons_are_exact():
    monday = datetime(2026, 9, 28, 9, tzinfo=timezone.utc)
    thursday = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    commitment = Commitment(
        user_id="owner",
        title="evaluation",
        confidence=1,
        derivation="test",
        due_at=datetime(2026, 10, 2, 23, 59, 59, tzinfo=timezone.utc),
        due_granularity="date",
        estimated_effort_minutes=180,
        importance=2,
        dependencies=[{"kind": "blocked", "label": "API access"}],
        created_at=monday - timedelta(days=4),
    )

    early = urgency_for(commitment, monday)
    late = urgency_for(commitment, thursday)

    assert late.score > early.score
    assert early.factors == {
        "deadline_hours": 110,
        "effort_minutes": 180,
        "slack_hours": 107,
        "importance": 2,
        "inactivity_days": 4,
        "blocked_dependencies": 1,
        "availability_minutes": None,
    }
    assert late.bucket == "high"
    assert late.factors["slack_hours"] == 35


def test_unknown_effort_has_no_slack_factor_bonus_or_explanation():
    now = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    commitment = Commitment(
        user_id="owner",
        title="unknown effort task",
        confidence=1,
        derivation="test",
        due_at=now + timedelta(hours=24),
        importance=0,
        created_at=now,
    )

    urgency = urgency_for(commitment, now)

    assert urgency.score == 40
    assert urgency.factors["effort_minutes"] is None
    assert urgency.factors["slack_hours"] is None
    explanation = _explanation(commitment.title, urgency)
    assert "estimated effort" not in explanation
    assert "slack" not in explanation


def test_recent_touch_resets_inactivity_without_changing_creation_time():
    now = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    commitment = Commitment(
        user_id="owner",
        title="evaluation",
        confidence=1,
        derivation="test",
        importance=0,
        created_at=now - timedelta(days=7),
        last_touched_at=now,
    )

    urgency = urgency_for(commitment, now)

    assert urgency.factors["inactivity_days"] == 0


def test_unknown_temporal_values_are_not_fabricated():
    parsed = parse_commitment_timing(
        "I need to write the evaluation", datetime(2026, 9, 28, tzinfo=timezone.utc), "UTC"
    )
    assert parsed.due_at is None
    assert parsed.estimated_effort_minutes is None
    assert parsed.ambiguity == "none"


def test_effort_outside_documented_bounds_is_not_persisted_and_requests_clarification():
    sessions = make_sessions()
    now = datetime(2026, 9, 28, 9, tzinfo=timezone.utc)
    parsed = parse_commitment_timing(
        "I need to archive records; it should take about 999999999999 hours",
        now,
        "UTC",
    )
    assert parsed.estimated_effort_minutes is None
    assert parsed.ambiguity == "effort_out_of_range"

    with sessions.begin() as db:
        user = User(username="effort-owner")
        db.add(user)
        db.flush()
        conversation = ConversationSession(user_id=user.id)
        db.add(conversation)
        db.flush()
        user_id, conversation_id = user.id, conversation.id

    with sessions() as db:
        AgentRuntime(clock=FakeClock(now)).handle(
            db,
            user_id,
            RuntimeEvent(
                "huge-effort",
                "user_message",
                "I need to archive records; it should take about 999999999999 hours",
                conversation_id,
            ),
        )

    with sessions() as db:
        commitment = db.scalar(select(Commitment))
        assert commitment.estimated_effort_minutes is None
        assert commitment.effort_minutes is None
        assert commitment.ambiguity == "effort_out_of_range"
        assert "between 1 minute and 7 days" in db.scalars(
            select(Message).order_by(Message.ordinal)
        ).all()[-1].content


def test_runtime_persists_normalized_date_effort_and_source_phrase():
    sessions = make_sessions()
    clock = FakeClock(datetime(2026, 9, 28, 14, tzinfo=timezone.utc))
    with sessions.begin() as db:
        user = User(username="owner", timezone="America/Los_Angeles")
        db.add(user)
        db.flush()
        conversation = ConversationSession(user_id=user.id)
        db.add(conversation)
        db.flush()
        user_id, conversation_id = user.id, conversation.id

    with sessions() as db:
        AgentRuntime(clock=clock).handle(
            db,
            user_id,
            RuntimeEvent(
                "timed",
                "user_message",
                "I need to submit the evaluation by Friday; it should take about three hours",
                conversation_id,
            ),
        )

    with sessions() as db:
        commitment = db.scalar(select(Commitment))
        assert commitment.title == "submit the evaluation"
        assert commitment.due_at.replace(tzinfo=timezone.utc) == datetime(
            2026, 10, 3, 6, 59, 59, tzinfo=timezone.utc
        )
        assert commitment.due_granularity == "date"
        assert commitment.due_source_text == "by Friday"
        assert commitment.estimated_effort_minutes == 180
        assert commitment.effort_minutes == 180  # compatibility during M3 migration


def test_ambiguous_date_is_retained_without_guess_and_requests_clarification():
    sessions = make_sessions()
    clock = FakeClock(datetime(2026, 9, 28, 14, tzinfo=timezone.utc))
    with sessions.begin() as db:
        user = User(username="owner", timezone="UTC")
        db.add(user)
        db.flush()
        conversation = ConversationSession(user_id=user.id)
        db.add(conversation)
        db.flush()
        user_id, conversation_id = user.id, conversation.id

    with sessions() as db:
        AgentRuntime(clock=clock).handle(
            db,
            user_id,
            RuntimeEvent("ambiguous", "user_message", "I need to submit by 10/11", conversation_id),
        )

    with sessions() as db:
        commitment = db.scalar(select(Commitment))
        assert commitment.due_at is None
        assert commitment.ambiguity == "ambiguous_numeric_date"
        assert db.scalars(select(Message).order_by(Message.ordinal)).all()[-1].content == (
            "I saved that commitment, but is 10/11 October 11 or November 10?"
        )


def test_state_extractor_recognizes_documented_commitment_examples():
    extractor = StateExtractor()
    assert extractor.extract("Send Sarah the document before Friday").kind == "commitment"
    assert extractor.extract("I need to submit the evaluation next Friday").kind == "commitment"


def test_followup_effort_phrase_updates_one_explicitly_named_commitment():
    sessions = make_sessions()
    now = datetime(2026, 9, 28, 9, tzinfo=timezone.utc)
    with sessions.begin() as db:
        user = User(username="owner")
        db.add(user)
        db.flush()
        conversation = ConversationSession(user_id=user.id)
        commitment = Commitment(
            user_id=user.id,
            title="submit the evaluation",
            confidence=1,
            derivation="test",
            created_at=now,
        )
        db.add_all([conversation, commitment])
        db.flush()
        user_id, conversation_id, commitment_id = user.id, conversation.id, commitment.id

    with sessions() as db:
        AgentRuntime(clock=FakeClock(now)).handle(
            db,
            user_id,
            RuntimeEvent(
                "effort-update",
                "user_message",
                "The evaluation should take about three hours",
                conversation_id,
            ),
        )

    with sessions() as db:
        commitment = db.get(Commitment, commitment_id)
        assert commitment.estimated_effort_minutes == 180
        assert commitment.effort_minutes == 180
        assert commitment.last_touched_at.replace(tzinfo=timezone.utc) == now


def test_temporal_update_refreshes_last_touched_at():
    sessions = make_sessions()
    created = datetime(2026, 9, 21, 9, tzinfo=timezone.utc)
    now = datetime(2026, 9, 28, 9, tzinfo=timezone.utc)
    with sessions.begin() as db:
        user = User(username="touch-owner")
        db.add(user)
        db.flush()
        conversation = ConversationSession(user_id=user.id)
        commitment = Commitment(
            user_id=user.id,
            title="submit the evaluation",
            confidence=1,
            derivation="test",
            created_at=created,
        )
        db.add_all([conversation, commitment])
        db.flush()
        user_id, conversation_id, commitment_id = user.id, conversation.id, commitment.id

    with sessions() as db:
        AgentRuntime(clock=FakeClock(now)).handle(
            db,
            user_id,
            RuntimeEvent(
                "touch-effort-update",
                "user_message",
                "The evaluation should take about three hours",
                conversation_id,
            ),
        )

    with sessions() as db:
        commitment = db.get(Commitment, commitment_id)
        assert commitment.last_touched_at.replace(tzinfo=timezone.utc) == now
        assert urgency_for(commitment, now).factors["inactivity_days"] == 0


def seed_due_commitment(sessions, now, *, state="open", deleted=False):
    with sessions.begin() as db:
        user = User(username="owner", timezone="UTC")
        db.add(user)
        db.flush()
        conversation = ConversationSession(user_id=user.id)
        commitment = Commitment(
            user_id=user.id,
            title="finish evaluation",
            state=state,
            deleted=deleted,
            confidence=1,
            derivation="test",
            due_at=datetime(2026, 10, 2, 23, 59, 59, tzinfo=timezone.utc),
            due_granularity="date",
            due_source_text="Friday",
            estimated_effort_minutes=180,
            importance=2,
            dependencies=[{"kind": "blocked", "label": "not audited"}],
            created_at=datetime(2026, 9, 24, 9, tzinfo=timezone.utc),
        )
        db.add_all([conversation, commitment])
        db.flush()
        return user.id, conversation.id, commitment.id


def test_periodic_check_is_noop_then_surfaces_complete_audit_bundle():
    sessions = make_sessions()
    monday = datetime(2026, 9, 28, 9, tzinfo=timezone.utc)
    clock = FakeClock(monday)
    user_id, conversation_id, commitment_id = seed_due_commitment(sessions, monday)
    runtime = AgentRuntime(clock=clock)

    with sessions() as db:
        first = runtime.handle(db, user_id, RuntimeEvent("check-mon", "commitment_check"))
    clock.set(datetime(2026, 10, 1, 9, tzinfo=timezone.utc))
    with sessions() as db:
        second = runtime.handle(db, user_id, RuntimeEvent("check-thu", "commitment_check"))

    assert first["surfaced"] == 0
    assert second["surfaced"] == 1
    with sessions() as db:
        reminder = db.scalar(select(Reminder))
        assert reminder.state == "surfaced"
        assert reminder.reason_code == "high_urgency"
        assert reminder.reason_metadata == {
            "score": 87,
            "bucket": "high",
            "factors": {
                "deadline_hours": 38,
                "effort_minutes": 180,
                "slack_hours": 35,
                "importance": 2,
                "inactivity_days": 7,
                "blocked_dependencies": 1,
                "availability_minutes": None,
            },
        }
        message = db.get(Message, reminder.surfaced_message_id)
        assert message.conversation_id == conversation_id
        assert message.content == (
            "I’m bringing up “finish evaluation” because the deadline is in 38 hours, "
            "estimated effort is 180 minutes, schedule slack is 35 hours, importance is 2/3, "
            "it has been inactive for 7 days, and 1 dependency is blocked."
        )
        assert db.get(Commitment, commitment_id).last_surfaced_at.replace(
            tzinfo=timezone.utc
        ) == clock.now()
        assert db.get(Commitment, commitment_id).last_touched_at.replace(
            tzinfo=timezone.utc
        ) == datetime(2026, 9, 24, 9, tzinfo=timezone.utc)
        assert db.scalar(select(func.count()).select_from(Episode)) == 1
        assert db.scalar(select(func.count()).select_from(ProvenanceLink)) >= 2
        assert db.scalar(select(func.count()).select_from(SkillRun)) == 2
        assert db.scalar(select(func.count()).select_from(AgentEvent)) == 2


def test_recent_touch_yields_zero_inactivity_and_exact_reminder_explanation():
    sessions = make_sessions()
    now = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    user_id, _, commitment_id = seed_due_commitment(sessions, now)
    with sessions.begin() as db:
        db.get(Commitment, commitment_id).last_touched_at = now

    with sessions() as db:
        result = AgentRuntime(clock=FakeClock(now)).handle(
            db, user_id, RuntimeEvent("touched-check", "commitment_check")
        )

    assert result["surfaced"] == 1
    with sessions() as db:
        reminder = db.scalar(select(Reminder))
        assert reminder.reason_metadata["factors"]["inactivity_days"] == 0
        assert db.get(Message, reminder.surfaced_message_id).content == (
            "I’m bringing up “finish evaluation” because the deadline is in 38 hours, "
            "estimated effort is 180 minutes, schedule slack is 35 hours, importance is 2/3, "
            "and 1 dependency is blocked."
        )


def test_duplicate_checks_create_one_reminder_per_relevance_window():
    sessions = make_sessions()
    now = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    user_id, _, _ = seed_due_commitment(sessions, now)
    runtime = AgentRuntime(clock=FakeClock(now))
    with sessions() as db:
        runtime.handle(db, user_id, RuntimeEvent("check-1", "commitment_check"))
    with sessions() as db:
        repeated = runtime.handle(db, user_id, RuntimeEvent("check-2", "commitment_check"))

    assert repeated["surfaced"] == 0
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Reminder)) == 1
        assert db.scalar(select(func.count()).select_from(Message)) == 1


def test_repeated_bounded_scans_advance_past_cold_rows_to_later_eligible_work():
    sessions = make_sessions()
    now = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    with sessions.begin() as db:
        user = User(username="fair-scan-owner")
        db.add(user)
        db.flush()
        db.add(ConversationSession(user_id=user.id))
        for index in range(100):
            db.add(
                Commitment(
                    user_id=user.id,
                    title=f"cold task {index}",
                    confidence=1,
                    derivation="test",
                    created_at=now - timedelta(minutes=101 - index),
                )
            )
        urgent = Commitment(
            user_id=user.id,
            title="urgent row 101",
            confidence=1,
            derivation="test",
            next_relevant_moment=now,
            created_at=now,
        )
        db.add(urgent)
        db.flush()
        user_id, urgent_id = user.id, urgent.id

    runtime = AgentRuntime(clock=FakeClock(now))
    with sessions() as db:
        first = runtime.handle(db, user_id, RuntimeEvent("fair-scan-1", "commitment_check"))
    with sessions() as db:
        second = runtime.handle(db, user_id, RuntimeEvent("fair-scan-2", "commitment_check"))

    assert first["surfaced"] == 0
    assert second["surfaced"] == 1
    with sessions() as db:
        reminders = db.scalars(select(Reminder)).all()
        assert len(reminders) == 1
        assert reminders[0].commitment_id == urgent_id


def test_explicit_next_relevant_moment_surfaces_without_inventing_factors():
    sessions = make_sessions()
    now = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    with sessions.begin() as db:
        user = User(username="owner")
        db.add(user)
        db.flush()
        db.add(
            Commitment(
                user_id=user.id,
                title="check application",
                confidence=1,
                derivation="test",
                next_relevant_moment=now,
                created_at=now,
            )
        )
        user_id = user.id

    with sessions() as db:
        result = AgentRuntime(clock=FakeClock(now)).handle(
            db, user_id, RuntimeEvent("relevant", "commitment_check")
        )
    assert result["surfaced"] == 1
    with sessions() as db:
        reminder = db.scalar(select(Reminder))
        assert reminder.reason_code == "relevant_moment"
        assert db.get(Message, reminder.surfaced_message_id).content == (
            "I’m bringing up “check application” because its stored next relevant "
            "moment has arrived."
        )


@pytest.mark.parametrize("state", ["complete", "completed", "cancelled", "abandoned"])
def test_terminal_commitments_never_surface(state):
    sessions = make_sessions()
    now = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    user_id, _, _ = seed_due_commitment(sessions, now, state=state)
    with sessions() as db:
        result = AgentRuntime(clock=FakeClock(now)).handle(
            db, user_id, RuntimeEvent(f"check-{state}", "commitment_check")
        )
    assert result["surfaced"] == 0


def test_deleted_commitment_never_surfaces():
    sessions = make_sessions()
    now = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    user_id, _, _ = seed_due_commitment(sessions, now, deleted=True)
    with sessions() as db:
        result = AgentRuntime(clock=FakeClock(now)).handle(
            db, user_id, RuntimeEvent("check-deleted", "commitment_check")
        )
    assert result["surfaced"] == 0


@pytest.mark.parametrize(
    "content",
    [
        "I finished that",
        "I finished that already",
        "I completed that",
        "I completed that already",
        "done with that",
        "done with that already",
    ],
)
def test_demonstrative_completion_variants_resolve_only_unique_recent_surface(content):
    sessions = make_sessions()
    now = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    user_id, conversation_id, commitment_id = seed_due_commitment(sessions, now)
    runtime = AgentRuntime(clock=FakeClock(now))
    with sessions() as db:
        runtime.handle(db, user_id, RuntimeEvent("check", "commitment_check"))
    with sessions() as db:
        runtime.handle(
            db,
            user_id,
            RuntimeEvent(
                "finished", "user_message", content, conversation_id
            ),
        )

    with sessions() as db:
        commitment = db.get(Commitment, commitment_id)
        assert commitment.state == "complete"
        assert commitment.completed_at is not None
        assert db.scalars(select(Message).order_by(Message.ordinal)).all()[-1].content == (
            "Got it — I marked “finish evaluation” complete."
        )
    runtime.clock.set(datetime(2026, 10, 1, 12, tzinfo=timezone.utc))
    with sessions() as db:
        result = runtime.handle(db, user_id, RuntimeEvent("later", "commitment_check"))
    assert result["surfaced"] == 0


@pytest.mark.parametrize(
    "content",
    ["I finished that", "I completed that already", "done with that"],
)
def test_demonstrative_completion_without_surfaced_candidate_asks(content):
    sessions = make_sessions()
    now = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    user_id, conversation_id, commitment_id = seed_due_commitment(sessions, now)

    with sessions() as db:
        AgentRuntime(clock=FakeClock(now)).handle(
            db,
            user_id,
            RuntimeEvent(
                "unsurfaced-finished",
                "user_message",
                content,
                conversation_id,
            ),
        )

    with sessions() as db:
        commitment = db.get(Commitment, commitment_id)
        assert commitment.state == "open"
        assert commitment.completed_at is None
        assert db.scalars(select(Message).order_by(Message.ordinal)).all()[-1].content == (
            "Which commitment did you finish? I don’t have a uniquely recent surfaced commitment."
        )


@pytest.mark.parametrize(
    "content",
    ["I finished that already", "I completed that", "done with that already"],
)
def test_demonstrative_completion_asks_when_multiple_were_surfaced_together(content):
    sessions = make_sessions()
    now = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    with sessions.begin() as db:
        user = User(username="owner")
        db.add(user)
        db.flush()
        conversation = ConversationSession(user_id=user.id)
        first = Commitment(
            user_id=user.id,
            title="first task",
            confidence=1,
            derivation="test",
            last_surfaced_at=now,
        )
        second = Commitment(
            user_id=user.id,
            title="second task",
            confidence=1,
            derivation="test",
            last_surfaced_at=now,
        )
        db.add_all([conversation, first, second])
        db.flush()
        user_id, conversation_id = user.id, conversation.id

    with sessions() as db:
        AgentRuntime(clock=FakeClock(now)).handle(
            db,
            user_id,
            RuntimeEvent(
                "ambiguous-finished",
                "user_message",
                content,
                conversation_id,
            ),
        )
    with sessions() as db:
        assert all(item.state == "open" for item in db.scalars(select(Commitment)))
        assert "Which commitment" in db.scalars(
            select(Message).order_by(Message.ordinal)
        ).all()[-1].content
