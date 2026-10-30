"""Semantic decisions scripted; PostgreSQL/runtime/scheduler/delivery effects are real."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from test_assistance import ScriptedGateway, reply, send, tool
from test_postgres_quality import csrf, login
from test_postgres_quality import pg_client as pg_client
from test_postgres_quality import postgres_url as postgres_url

from app.agent import AgentRuntime
from app.clock import FakeClock
from app.commitments import utc
from app.models import (
    Commitment,
    ConversationProposal,
    JournalEntry,
    Message,
    ReflectionPrompt,
    ReminderPlan,
    ScheduledAction,
    TelegramBot,
    TelegramConnection,
    TelegramDelivery,
    User,
    VocabularyAttempt,
)
from app.scheduler import tick
from app.telegram import cycle
from app.temporal import When, resolve_when

NOW = datetime(2026, 10, 29, 9, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def deterministic(monkeypatch):
    monkeypatch.setenv("MODEL_MODE", "deterministic")


@pytest.fixture()
def scene(pg_client, monkeypatch):
    import app.main as main

    assert login(pg_client).status_code == 200
    gateway = ScriptedGateway()
    monkeypatch.setattr(main.runtime, "gateway", gateway)
    monkeypatch.setattr(main.runtime, "clock", FakeClock(NOW))
    with main.SessionLocal.begin() as db:
        user = db.scalar(select(User))
        user.timezone = "Africa/Lagos"
        user.checkins_enabled = True
    return main, gateway


def act(client, gateway, event, content, name, args, *, allowed=True):
    gateway.scripts.append(
        reply("", tool(name, {**args, "response_text": "I've updated that as requested."}))
    )
    return send(client, event, content, allow_memory_write=allowed)


@pytest.mark.parametrize(
    "utterance",
    [
        "Not so great",
        "You’re not gonna ask how my day went",
        "You're not gonna ask how my day went?",
    ],
)
def test_pending_activities_do_not_arbitrate_live_conversation(pg_client, scene, utterance):
    main, gateway = scene
    for kind in ("morning_briefing", "evening_reflection"):
        assert (
            pg_client.post(
                "/api/events/scheduled",
                headers=csrf(pg_client),
                json={"event_id": kind, "kind": kind},
            ).status_code
            == 202
        )
    act(
        pg_client,
        gateway,
        "start-word",
        "Let's review inchoate",
        "manage_workflow",
        {"kind": "vocabulary", "action": "start", "word": "inchoate"},
    )
    count = len(gateway.calls)
    gateway.scripts.append(reply("I'm sorry it felt rough. What happened with your day?"))
    answer = send(pg_client, "day-disclosure", utterance, allow_memory_write=True)
    assert len(gateway.calls) == count + 1
    assert "What happened" in answer["content"] and "grade" not in answer["content"]
    context = json.loads(gateway.calls[-1]["messages"][1]["content"].split("\n", 1)[1])
    assert {w["kind"] for w in context["conversation_state"]["workflows"]} == {
        "reflection",
        "vocabulary",
    }
    with main.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(JournalEntry)) == 0
        assert db.scalar(select(ReflectionPrompt)).status == "awaiting"
        assert db.scalar(select(VocabularyAttempt)).user_answer is None


def test_three_pm_is_a_one_time_reminder_without_date_clarification(pg_client, scene):
    main, gateway = scene
    act(
        pg_client,
        gateway,
        "pelumi",
        "Remind me to look for Pelumi Buari at 3pm.",
        "schedule_reminder",
        {"topic": "Look for Pelumi Buari", "when": {"kind": "local", "at": "15:00"}},
    )
    with main.SessionLocal() as db:
        row = db.scalar(select(ReminderPlan))
        assert row.mode == "once" and row.max_occurrences == 1
        assert utc(row.next_due_at) == NOW.replace(hour=14)
        assert db.scalar(select(func.count()).select_from(ConversationProposal)) == 0
        source = db.get(Message, row.source_message_id)
        assert "Pelumi Buari" in source.content
    assert len(gateway.calls) == 1


def test_eight_minute_focus_checkin_recurrence_and_completion(pg_client, scene):
    main, gateway = scene
    act(
        pg_client,
        gateway,
        "focus",
        "Could you pls remnd me to focus every 8min ?",
        "schedule_reminder",
        {"topic": "Focus", "mode": "recurring", "interval_seconds": 480, "max_occurrences": 2},
    )
    with main.SessionLocal() as db:
        plan = db.scalar(select(ReminderPlan))
        identifier = plan.id
        assert utc(plan.next_due_at) == NOW + timedelta(minutes=8)
    clock = FakeClock(NOW + timedelta(minutes=8))
    worker = AgentRuntime(clock=clock)
    assert tick(main.SessionLocal, worker)["executed"] == 1
    assert tick(main.SessionLocal, worker)["executed"] == 0
    clock.set(NOW + timedelta(minutes=16))
    assert tick(main.SessionLocal, worker)["executed"] == 1
    with main.SessionLocal() as db:
        plan = db.get(ReminderPlan, identifier)
        assert plan.state == "completed" and plan.next_due_at is None and plan.occurrence_count == 2
        assert db.scalar(select(func.count()).select_from(ScheduledAction)) == 2
    clock.set(NOW + timedelta(minutes=24))
    assert tick(main.SessionLocal, worker)["executed"] == 0


def test_proposal_survives_interruption_and_fresh_session_then_executes_once(pg_client, scene):
    main, gateway = scene
    act(
        pg_client,
        gateway,
        "offer",
        "Suggest a focus check-in, but ask before setting it.",
        "propose_action",
        {
            "tool_name": "schedule_reminder",
            "title": "Focus check-in offer",
            "arguments": {
                "topic": "Focus",
                "mode": "recurring",
                "interval_seconds": 900,
                "when": {"kind": "after", "seconds": 900, "anchor": "acceptance"},
            },
        },
    )
    with main.SessionLocal() as db:
        proposal = db.scalar(select(ConversationProposal))
        identifier = proposal.id
        assert proposal.state == "proposed"
        assert db.scalar(select(func.count()).select_from(ReminderPlan)) == 0
    for index in range(3):
        gateway.scripts.append(reply("We can talk about that without changing your pending plan."))
        send(
            pg_client,
            f"interruption-{index}",
            "Let's talk about something else.",
            allow_memory_write=True,
        )
    assert pg_client.post("/api/auth/logout", headers=csrf(pg_client)).status_code == 204
    assert login(pg_client).status_code == 200
    main.runtime.clock.set(NOW + timedelta(minutes=10))
    act(
        pg_client,
        gateway,
        "accept",
        "OK do that thanks",
        "resolve_proposal",
        {"id": identifier, "version": 1, "action": "execute"},
    )
    with main.SessionLocal() as db:
        plan = db.scalar(select(ReminderPlan))
        assert plan.topic == "Focus" and plan.interval_seconds == 900
        assert utc(plan.next_due_at) == NOW + timedelta(minutes=25)
        assert db.get(ConversationProposal, identifier).state == "executed"
    before = len(gateway.calls)
    response = pg_client.post(
        "/api/chat/messages",
        headers=csrf(pg_client),
        json={"event_id": "accept", "content": "OK do that thanks", "allow_memory_write": True},
    )
    assert response.status_code == 200
    assert len(gateway.calls) == before
    with main.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ReminderPlan)) == 1
    latest = pg_client.get("/api/chat/messages").json()[-1]
    trace = pg_client.get(f"/api/chat/messages/{latest['id']}/context").json()["trace"]
    assert any(t["to"] == "executed" for t in trace["transitions"])
    assert any(e["kind"] == "reminder" for e in trace["effects"])


def test_fifteen_minutes_followup_supplies_only_missing_time(pg_client, scene):
    main, gateway = scene
    act(
        pg_client,
        gateway,
        "missing",
        "Remind me to focus.",
        "schedule_reminder",
        {"topic": "Focus"},
    )
    with main.SessionLocal() as db:
        proposal = db.scalar(select(ConversationProposal))
        identifier = proposal.id
        assert proposal.state == "needs_clarification"
        assert db.scalar(select(func.count()).select_from(ReminderPlan)) == 0
    main.runtime.clock.set(NOW + timedelta(minutes=2))
    act(
        pg_client,
        gateway,
        "clarify",
        "15min from now",
        "resolve_proposal",
        {
            "id": identifier,
            "version": 1,
            "action": "execute",
            "changes": {"when": {"kind": "after", "seconds": 900}},
        },
    )
    with main.SessionLocal() as db:
        assert utc(db.scalar(select(ReminderPlan)).next_due_at) == NOW + timedelta(minutes=17)
        assert db.scalar(select(func.count()).select_from(ConversationProposal)) == 1


def test_acceptance_still_needs_current_write_permission_and_ownership(pg_client, scene):
    main, gateway = scene
    act(
        pg_client,
        gateway,
        "offer",
        "Offer me a reminder",
        "propose_action",
        {
            "tool_name": "schedule_reminder",
            "title": "Offer",
            "arguments": {
                "topic": "Focus",
                "when": {"kind": "after", "seconds": 30, "anchor": "acceptance"},
            },
        },
        allowed=False,
    )
    with main.SessionLocal() as db:
        identifier = db.scalar(select(ConversationProposal.id))
    answer = act(
        pg_client,
        gateway,
        "no-grant",
        "OK do that thanks",
        "resolve_proposal",
        {"id": identifier, "version": 1, "action": "execute"},
        allowed=False,
    )
    assert "write_permission_required" in answer["content"]
    with main.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ReminderPlan)) == 0
        assert db.get(ConversationProposal, identifier).state == "proposed"


class ProcessCrash(BaseException):
    pass


@pytest.mark.parametrize(
    "stage", ["after_prepare", "after_claim", "before_effect_commit", "after_effect_commit"]
)
def test_short_one_time_reminder_survives_crash_and_duplicate_sweeps(pg_client, scene, stage):
    main, gateway = scene
    act(
        pg_client,
        gateway,
        "short",
        "Remind me in ten seconds",
        "schedule_reminder",
        {"topic": "Short trial", "when": {"kind": "after", "seconds": 10}},
    )
    worker = AgentRuntime(clock=FakeClock(NOW + timedelta(seconds=10)))

    def crash(point, _):
        if point == stage:
            raise ProcessCrash()

    with pytest.raises(ProcessCrash):
        tick(main.SessionLocal, worker, fault=crash)
    worker = AgentRuntime(clock=FakeClock(NOW + timedelta(seconds=80)))
    tick(main.SessionLocal, worker)
    tick(main.SessionLocal, worker)
    with main.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ScheduledAction)) == 1
        assert db.scalar(select(ReminderPlan)).state == "completed"
        assert (
            db.scalar(
                select(func.count()).select_from(Message).where(Message.content.like("Reminder:%"))
            )
            == 1
        )


def test_one_time_telegram_delivery_is_valid_after_plan_completed(pg_client, scene):
    main, gateway = scene
    with main.SessionLocal.begin() as db:
        owner = db.scalar(select(User))
        owner.quiet_start = 0
        owner.quiet_end = 23
        db.add(TelegramBot(id="123", username="TestBot"))
        db.flush()
        db.add(
            TelegramConnection(
                user_id=owner.id,
                bot_id="123",
                peer_id="456",
                linked=True,
                allow_memory_write=True,
                proactive_enabled=True,
                proactive_since=NOW,
            )
        )
    act(
        pg_client,
        gateway,
        "short",
        "Remind me in one minute",
        "schedule_reminder",
        {"topic": "One-time", "when": {"kind": "after", "seconds": 60}},
    )
    main.runtime.clock.set(NOW + timedelta(seconds=60))
    tick(main.SessionLocal, AgentRuntime(clock=main.runtime.clock))

    class Transport:
        def __init__(self):
            self.sent = []

        def send(self, peer, content):
            self.sent.append((peer, content))
            return "22"

    transport = Transport()
    cycle(main.SessionLocal, main.runtime, transport, "123", [])
    cycle(main.SessionLocal, main.runtime, transport, "123", [])
    assert transport.sent == [("456", "Reminder: One-time.")]
    with main.SessionLocal() as db:
        assert db.scalar(select(TelegramDelivery)).state == "sent"


def test_structured_temporal_resolution_uses_timestamp_timezone_and_dst():
    assert resolve_when(When(kind="local", at="15:00"), NOW, "Africa/Lagos") == NOW.replace(hour=14)
    assert resolve_when(When(kind="after", seconds=480), NOW, "Africa/Lagos") == NOW + timedelta(
        minutes=8
    )
    later = NOW.replace(hour=16)
    assert resolve_when(When(kind="local", at="15:00"), later, "Africa/Lagos") == NOW.replace(
        hour=14
    ) + timedelta(days=1)
    with pytest.raises(ValueError):
        resolve_when(When(kind="local", date="2026-11-01", at="01:30"), NOW, "America/New_York")


def test_proposal_pause_resume_partial_correction_and_cancel(pg_client, scene):
    main, gateway = scene
    act(
        pg_client,
        gateway,
        "offer",
        "Offer a reminder for 3pm tomorrow",
        "propose_action",
        {
            "tool_name": "schedule_reminder",
            "title": "Draft reminder",
            "arguments": {
                "topic": "Draft",
                "when": {"kind": "local", "date": "2026-10-30", "at": "15:00"},
            },
        },
    )
    with main.SessionLocal() as db:
        p = db.scalar(select(ConversationProposal))
        identifier = p.id
    for event, action, version in (("pause", "pause", 1), ("resume", "resume", 2)):
        act(
            pg_client,
            gateway,
            event,
            event,
            "resolve_proposal",
            {"id": identifier, "version": version, "action": action},
        )
    act(
        pg_client,
        gateway,
        "amend",
        "Make that Saturday instead",
        "resolve_proposal",
        {
            "id": identifier,
            "version": 3,
            "action": "amend",
            "changes": {"when": {"date": "2026-10-31"}},
        },
    )
    with main.SessionLocal() as db:
        p = db.get(ConversationProposal, identifier)
        assert p.arguments["when"]["at"] == "15:00" and p.arguments["when"]["date"] == "2026-10-31"
        assert p.arguments["when"]["timezone"] == "Africa/Lagos"
    act(
        pg_client,
        gateway,
        "cancel",
        "Never mind",
        "resolve_proposal",
        {"id": identifier, "version": 4, "action": "cancel"},
    )
    answer = act(
        pg_client,
        gateway,
        "closed",
        "Do it",
        "resolve_proposal",
        {"id": identifier, "version": 5, "action": "execute"},
    )
    assert "closed_proposal" in answer["content"]
    with main.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ReminderPlan)) == 0


def test_model_can_pause_resume_and_semantically_grade_vocabulary(pg_client, scene):
    main, gateway = scene
    pg_client.post(
        "/api/events/scheduled",
        headers=csrf(pg_client),
        json={"event_id": "morning", "kind": "morning_briefing"},
    )
    act(
        pg_client,
        gateway,
        "start",
        "Practice inchoate",
        "manage_workflow",
        {"kind": "vocabulary", "action": "start", "word": "inchoate"},
    )
    with main.SessionLocal() as db:
        identifier = db.scalar(select(VocabularyAttempt.id))
    act(
        pg_client,
        gateway,
        "pause",
        "Let's leave this for a moment",
        "manage_workflow",
        {"kind": "vocabulary", "id": identifier, "version": 1, "action": "pause"},
    )
    gateway.scripts.append(reply("We can talk about your day."))
    send(pg_client, "other", "My day was difficult", allow_memory_write=True)
    with main.SessionLocal() as db:
        assert db.get(VocabularyAttempt, identifier).workflow_state == "paused"
    act(
        pg_client,
        gateway,
        "resume",
        "Back to the word",
        "manage_workflow",
        {"kind": "vocabulary", "id": identifier, "version": 2, "action": "resume"},
    )
    act(
        pg_client,
        gateway,
        "answer",
        "It means only starting to take shape",
        "manage_workflow",
        {
            "kind": "vocabulary",
            "id": identifier,
            "version": 3,
            "action": "respond",
            "grade": "correct",
        },
    )
    with main.SessionLocal() as db:
        attempt = db.get(VocabularyAttempt, identifier)
        assert attempt.correct is True and attempt.workflow_state == "completed"
        assert attempt.user_answer == "It means only starting to take shape"


def test_stale_and_foreign_proposals_cannot_execute(pg_client, scene):
    main, gateway = scene
    act(
        pg_client,
        gateway,
        "offer",
        "Offer a short reminder",
        "propose_action",
        {
            "tool_name": "schedule_reminder",
            "title": "Offer",
            "arguments": {"topic": "Read", "when": {"kind": "after", "seconds": 60}},
        },
    )
    with main.SessionLocal() as db:
        identifier = db.scalar(select(ConversationProposal.id))
    answer = act(
        pg_client,
        gateway,
        "stale",
        "OK",
        "resolve_proposal",
        {"id": identifier, "version": 99, "action": "execute"},
    )
    assert "version_conflict" in answer["content"]
    assert (
        pg_client.post(
            "/api/auth/login",
            headers={"origin": "http://testserver"},
            json={"username": "other", "access_code": "correct horse battery staple"},
        ).status_code
        == 200
    )
    answer = act(
        pg_client,
        gateway,
        "foreign",
        "OK",
        "resolve_proposal",
        {"id": identifier, "version": 1, "action": "execute"},
    )
    assert "unknown_proposal" in answer["content"]
    with main.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ReminderPlan)) == 0


def test_telegram_relative_time_uses_sent_timestamp_not_processing_time(pg_client, scene):
    main, gateway = scene
    with main.SessionLocal.begin() as db:
        owner = db.scalar(select(User))
        db.add(TelegramBot(id="123", username="TestBot"))
        db.flush()
        db.add(
            TelegramConnection(
                user_id=owner.id,
                bot_id="123",
                peer_id="456",
                linked=True,
                allow_memory_write=True,
                proactive_enabled=True,
                proactive_since=NOW - timedelta(minutes=1),
            )
        )
    main.runtime.clock.set(NOW + timedelta(minutes=2))
    gateway.scripts.append(
        reply(
            "",
            tool(
                "schedule_reminder",
                {
                    "topic": "Read",
                    "when": {"kind": "after", "seconds": 900},
                    "response_text": "Scheduled.",
                },
            ),
        )
    )

    class Transport:
        def send(self, peer, content):
            return "42"

    cycle(
        main.SessionLocal,
        main.runtime,
        Transport(),
        "123",
        [
            {
                "update_id": 1,
                "message": {
                    "date": int(NOW.timestamp()),
                    "chat": {"id": 456, "type": "private"},
                    "from": {"id": 456, "is_bot": False},
                    "text": "Remind me to read in 15 minutes",
                },
            }
        ],
    )
    with main.SessionLocal() as db:
        assert utc(db.scalar(select(ReminderPlan)).due_at) == NOW + timedelta(minutes=15)
    context = json.loads(gateway.calls[0]["messages"][1]["content"].split("\n", 1)[1])
    assert context["conversation_state"]["message_at"] == NOW.isoformat()


def test_unrelated_preposition_does_not_invent_a_deadline_and_date_bounds_are_preserved(
    pg_client, scene
):
    main, gateway = scene
    act(
        pg_client,
        gateway,
        "goal",
        "I want to learn by practicing",
        "create_commitment",
        {"title": "Learn through practice", "estimated_effort_minutes": 180},
    )
    with main.SessionLocal() as db:
        row = db.scalar(select(Commitment))
        identifier = row.id
        assert row.due_at is None and row.estimated_effort_minutes == 180
    act(
        pg_client,
        gateway,
        "deadline",
        "Finish by Friday",
        "update_commitment",
        {"id": identifier, "version": 1, "deadline": {"kind": "local", "date": "2026-10-30"}},
    )
    with main.SessionLocal() as db:
        row = db.get(Commitment, identifier)
        assert row.due_granularity == "date"
        assert utc(row.due_at) == datetime(2026, 10, 30, 22, 59, 59, tzinfo=timezone.utc)


def test_claimed_reminder_is_cancelled_and_resume_creates_fresh_work(pg_client, scene):
    main, gateway = scene
    act(
        pg_client,
        gateway,
        "create",
        "Remind me in a minute",
        "schedule_reminder",
        {"topic": "Claim test", "when": {"kind": "after", "seconds": 60}},
    )
    with main.SessionLocal() as db:
        identifier = db.scalar(select(ReminderPlan.id))
    main.runtime.clock.set(NOW + timedelta(seconds=60))
    worker = AgentRuntime(clock=main.runtime.clock)

    def pause(stage, _):
        if stage == "after_claim":
            response = pg_client.post(
                f"/api/reminder-plans/{identifier}/control",
                headers=csrf(pg_client),
                json={"id": identifier, "version": 1, "event_id": "pause", "action": "pause"},
            )
            assert response.status_code == 200, response.text

    assert tick(main.SessionLocal, worker, fault=pause)["executed"] == 0
    with main.SessionLocal() as db:
        assert db.scalar(select(ScheduledAction)).state == "cancelled"
        assert db.get(ReminderPlan, identifier).state == "paused"
    response = pg_client.post(
        f"/api/reminder-plans/{identifier}/control",
        headers=csrf(pg_client),
        json={
            "id": identifier,
            "version": 2,
            "event_id": "resume",
            "action": "resume",
            "when": {"kind": "after", "seconds": 60},
        },
    )
    assert response.status_code == 200, response.text
    main.runtime.clock.set(NOW + timedelta(seconds=120))
    assert tick(main.SessionLocal, worker)["executed"] == 1
    assert tick(main.SessionLocal, worker)["executed"] == 0


def test_reminder_frequency_guard_is_configurable_and_never_bursts(pg_client, scene, monkeypatch):
    main, gateway = scene
    monkeypatch.setenv("MICA_MIN_CHECKIN_SECONDS", "10")
    monkeypatch.setenv("MICA_REMINDERS_PER_HOUR", "1")
    for index in range(2):
        act(
            pg_client,
            gateway,
            f"plan-{index}",
            "Check in every ten seconds",
            "schedule_reminder",
            {
                "topic": f"Focus {index}",
                "mode": "recurring",
                "interval_seconds": 10,
                "max_occurrences": 2,
            },
        )
    main.runtime.clock.set(NOW + timedelta(seconds=10))
    tick(main.SessionLocal, AgentRuntime(clock=main.runtime.clock))
    with main.SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(ScheduledAction)
                .where(ScheduledAction.state == "succeeded")
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(ScheduledAction)
                .where(ScheduledAction.reason == "reminder_rate_limit")
            )
            == 1
        )


def test_older_proposal_can_resume_by_unique_reference_after_context_eviction(pg_client, scene):
    main, gateway = scene
    for index in range(5):
        main.runtime.clock.set(NOW + timedelta(seconds=index))
        act(
            pg_client,
            gateway,
            f"offer-{index}",
            f"Offer reminder {index}",
            "propose_action",
            {
                "tool_name": "schedule_reminder",
                "title": f"Offer {index}",
                "arguments": {
                    "topic": f"Task {index}",
                    "when": {"kind": "after", "seconds": 60, "anchor": "acceptance"},
                },
            },
        )
    visible = pg_client.get("/api/continuations").json()["proposals"]
    assert "Offer 0" not in {item["title"] for item in visible}
    act(
        pg_client,
        gateway,
        "accept-old",
        "Let's do Offer 0 now",
        "resolve_proposal",
        {"reference": "Offer 0", "action": "execute"},
    )
    with main.SessionLocal() as db:
        assert db.scalar(select(ReminderPlan)).topic == "Task 0"
        assert db.scalar(select(func.count()).select_from(ReminderPlan)) == 1
