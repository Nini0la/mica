"""Milestone scenarios: real PostgreSQL/API/worker; model interpretation is scripted."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from test_assistance import ScriptedGateway, reply, send, tool
from test_postgres_quality import csrf, login
from test_postgres_quality import pg_client as pg_client
from test_postgres_quality import pg_sessions as pg_sessions
from test_postgres_quality import postgres_url as postgres_url

from app.agent import AgentRuntime
from app.clock import FakeClock
from app.commitments import ChangeCommitment, change, utc
from app.models import (
    AgentEvent,
    Commitment,
    CommitmentRevision,
    DailyBriefing,
    Message,
    ReflectionPrompt,
    ScheduledAction,
    User,
)
from app.prospective import parse_commitment_timing
from app.responsibilities import lock_user
from app.scheduler import prepare, tick

NOW = datetime(2026, 10, 26, 9, tzinfo=timezone.utc)
INTENTION = "I need to finish my presentation before 9pm tomorrow. Help me stay on track."


@pytest.fixture(autouse=True)
def deterministic(monkeypatch):
    monkeypatch.setenv("MODEL_MODE", "deterministic")


def preferences(client, **updates):
    existing = client.get("/api/autonomy").json()["preferences"]
    keys = (
        "version",
        "checkins_enabled",
        "morning_enabled",
        "evening_enabled",
        "morning_time",
        "evening_time",
        "quiet_start",
        "quiet_end",
    )
    response = client.patch(
        "/api/autonomy/preferences",
        headers=csrf(client),
        json={key: existing[key] for key in keys} | updates,
    )
    assert response.status_code == 200, response.text
    return response.json()


def capture(client, monkeypatch):
    import app.main as main

    assert login(client).status_code == 200
    monkeypatch.setattr(main.runtime, "clock", FakeClock(NOW))
    profile = client.get("/api/preferences").json()
    response = client.patch(
        "/api/preferences",
        headers=csrf(client),
        json={
            "version": profile["version"],
            "timezone": "Africa/Lagos",
            "morning_time": "08:00",
            "evening_time": "20:00",
            "onboarding_complete": True,
        },
    )
    assert response.status_code == 200, response.text
    preferences(client, checkins_enabled=True)
    gateway = ScriptedGateway()
    monkeypatch.setattr(main.runtime, "gateway", gateway)
    gateway.scripts.extend(
        [
            reply(
                "",
                tool(
                    "create_commitment",
                    {
                        "title": "Finish my presentation",
                        "deadline_text": "before 9pm tomorrow",
                        "purpose": "Explain my research",
                        "next_action": "Prepare the outline",
                        "followup_enabled": True,
                    },
                ),
            ),
            reply("The presentation is saved."),
        ]
    )
    send(client, "intention", INTENTION, allow_memory_write=True)
    item = client.get("/api/autonomy").json()["commitments"][0]
    assert not gateway.scripts
    return main, gateway, item


def control(client, item, action, **fields):
    response = client.post(
        f"/api/commitments/{item['id']}/lifecycle",
        headers=csrf(client),
        json={
            "event_id": f"{action}:{item['version']}",
            "version": item["version"],
            "action": action,
            **fields,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_s1_s2_s3_s9_conversation_fresh_session_unattended_correction_completion(
    pg_client, monkeypatch
):
    main, gateway, item = capture(pg_client, monkeypatch)
    assert item["due_at"] == "2026-10-27T20:00:00+00:00"
    assert item["due_bound"] == "exclusive"
    assert item["next_check_at"]
    assert pg_client.post("/api/auth/logout", headers=csrf(pg_client)).status_code == 204
    assert login(pg_client).status_code == 200
    assert pg_client.get("/api/autonomy").json()["commitments"] == [item]
    revisions = pg_client.get(f"/api/commitments/{item['id']}/revisions").json()["items"]
    assert revisions[0]["source_type"] == "message"
    assert revisions[0]["source_id"] in {
        row["id"]
        for row in pg_client.get("/api/chat/messages").json()
        if row["content"] == INTENTION
    }
    due = datetime.fromisoformat(item["next_check_at"])
    worker = AgentRuntime(clock=FakeClock(due))
    assert tick(main.SessionLocal, worker)["executed"] == 1
    assert tick(main.SessionLocal, worker)["executed"] == 0
    actions = pg_client.get("/api/autonomy/actions").json()["items"]
    assert len(actions) == 1
    assert actions[0]["state"] == "succeeded"
    assert actions[0]["decision"]["delivery"] == "in_app"
    assert actions[0]["source_event_id"] and actions[0]["message_id"]
    assert "lease_token" not in actions[0]
    message = next(
        row
        for row in pg_client.get("/api/chat/messages").json()
        if row["id"] == actions[0]["message_id"]
    )
    assert "check-in you enabled" in message["content"]
    # Persist an old pending plan, then correct it conversationally.
    with main.SessionLocal.begin() as db:
        user = db.scalar(select(User))
        old = db.get(Commitment, item["id"])
        prepare(db, user, utc(old.next_relevant_moment))
    main.runtime.clock = FakeClock(due + timedelta(minutes=1))
    gateway.scripts.extend(
        [
            reply(
                "",
                tool(
                    "update_commitment",
                    {
                        "id": item["id"],
                        "deadline_text": "Thursday at 6pm",
                    },
                ),
            ),
            reply("The deadline has moved."),
        ]
    )
    send(
        pg_client,
        "correction",
        "Actually, the deadline moved to Thursday at 6pm.",
        allow_memory_write=True,
    )
    updated = pg_client.get("/api/autonomy").json()["commitments"][0]
    assert updated["due_local"] == "2026-10-29T18:00:00+01:00"
    assert updated["purpose"] == item["purpose"]
    assert any(
        row["state"] == "cancelled"
        for row in pg_client.get("/api/autonomy/actions").json()["items"]
    )
    revisions = pg_client.get(f"/api/commitments/{item['id']}/revisions").json()["items"]
    assert len(revisions) == 2 and all(row["source_id"] for row in revisions)
    gateway.scripts.extend(
        [
            reply(
                "",
                tool(
                    "update_commitment",
                    {
                        "id": item["id"],
                        "reference": "presentation",
                        "action": "complete",
                    },
                ),
            ),
            reply("Marked complete."),
        ]
    )
    send(pg_client, "completion", "I've finished the presentation.", allow_memory_write=True)
    completed = pg_client.get("/api/autonomy").json()["commitments"][0]
    assert completed["state"] == "completed" and completed["next_check_at"] is None
    worker.clock = FakeClock(NOW + timedelta(days=4))
    tick(main.SessionLocal, worker)
    assert (
        sum(
            row["state"] == "succeeded"
            for row in pg_client.get("/api/autonomy/actions").json()["items"]
        )
        == 1
    )
    reopened = control(pg_client, completed, "reopen")
    paused = control(pg_client, reopened, "pause")
    assert paused["next_check_at"] is None
    resumed = control(pg_client, paused, "resume")
    assert resumed["state"] == "open" and resumed["next_check_at"]
    cancelled = control(pg_client, resumed, "cancel")
    assert cancelled["state"] == "cancelled" and cancelled["next_check_at"] is None


def test_availability_correction_bypasses_pending_evening_reflection(pg_client, monkeypatch):
    main, gateway, item = capture(pg_client, monkeypatch)
    response = pg_client.post(
        "/api/events/scheduled",
        headers=csrf(pg_client),
        json={
            "event_id": "pending-evening",
            "kind": "evening_reflection",
        },
    )
    assert response.status_code == 202
    gateway.scripts.extend(
        [
            reply("", tool("update_commitment", {"id": item["id"], "blocked_periods": []})),
            reply("The availability restriction is cleared."),
        ]
    )
    send(
        pg_client,
        "availability-restored",
        f"For {item['title']}, evenings are available again. Clear the blocked periods.",
        allow_memory_write=True,
    )
    updated = pg_client.get("/api/autonomy").json()["commitments"][0]
    assert updated["version"] == item["version"] + 1
    assert updated["availability"]["blocked_periods"] == []
    with main.SessionLocal() as db:
        prompt = db.scalar(select(ReflectionPrompt))
        assert prompt.status == "awaiting" and prompt.journal_entry_id is None
    assert not gateway.scripts


def seed(sessions, **settings):
    with sessions.begin() as db:
        user = User(username="owner", timezone="Africa/Lagos", checkins_enabled=True, **settings)
        db.add(user)
        db.flush()
        item = Commitment(
            user_id=user.id,
            title="Prepare for Friday",
            purpose="Learn geometry",
            confidence=1,
            derivation="test",
            next_action="Study in the evening",
            followup_enabled=True,
            next_relevant_moment=NOW,
            created_at=NOW,
            last_touched_at=NOW,
        )
        db.add(item)
        db.flush()
        return user.id, item.id


class ProcessCrash(BaseException):
    """Bypass normal exception recovery, like process termination."""


@pytest.mark.parametrize(
    "stage", ["after_prepare", "after_claim", "before_effect_commit", "after_effect_commit"]
)
def test_s4_process_crash_restart_and_overlapping_workers(pg_sessions, stage):
    seed(pg_sessions)
    runtime = AgentRuntime(clock=FakeClock(NOW))

    def crash(point, _):
        if point == stage:
            raise ProcessCrash()

    with pytest.raises(ProcessCrash):
        tick(pg_sessions, runtime, fault=crash)
    runtime = AgentRuntime(clock=FakeClock(NOW + timedelta(minutes=2)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: tick(pg_sessions, runtime), range(2)))
    assert all(result["failures"] == 0 for result in outcomes)
    with pg_sessions() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 1
        action = db.scalar(select(ScheduledAction))
        assert action.state == "succeeded" and action.message_id
        assert action.attempt_count <= 2
        assert db.get(AgentEvent, action.source_event_id).status == "handled"
        assert db.scalar(select(func.count()).select_from(ScheduledAction)) == 1


def test_s4_failures_retry_with_backoff_and_stop(pg_sessions):
    seed(pg_sessions)
    runtime = AgentRuntime(clock=FakeClock(NOW))

    def fail(point, _):
        if point == "before_effect_commit":
            raise RuntimeError("simulated database effect failure")

    for attempt in range(1, 4):
        assert tick(pg_sessions, runtime, fault=fail)["failures"] == 1
        with pg_sessions() as db:
            row = db.scalar(select(ScheduledAction))
            assert row.attempt_count == attempt
            assert row.state == ("failed" if attempt == 3 else "retryable")
            assert db.scalar(select(func.count()).select_from(Message)) == 0
            retry = row.retry_at
        assert tick(pg_sessions, runtime)["executed"] == 0
        runtime.clock = FakeClock(retry)
    assert tick(pg_sessions, runtime)["executed"] == 0


def test_s5_changed_availability_preserves_purpose_and_suppresses_old_plan(pg_sessions):
    owner, identifier = seed(pg_sessions)
    now = NOW.replace(hour=17)  # 18:00 local; the newly unavailable evening
    with pg_sessions.begin() as db:
        prepare(db, db.get(User, owner), now)
        result = change(
            db,
            owner,
            ChangeCommitment(
                id=identifier,
                blocked_periods=["evening"],
            ),
            "Evenings are unavailable this week, but I still want to finish preparing for Friday",
            "source-message",
            now,
        )
        assert result["status"] == "updated"
        item = db.get(Commitment, identifier)
        assert item.purpose == "Learn geometry"
        assert "evening" not in item.next_action
        assert item.availability == {"blocked_periods": ["evening"], "until": "2026-11-01"}
        assert utc(item.next_relevant_moment).hour == 7  # next allowed local 08:00
        assert db.scalar(select(ScheduledAction)).state == "cancelled"
        assert db.scalar(select(func.count()).select_from(CommitmentRevision)) == 2
    assert tick(pg_sessions, AgentRuntime(clock=FakeClock(now)))["executed"] == 0


def test_s6_two_local_days_daily_routines_disable_and_no_replay(pg_sessions):
    owner, identifier = seed(
        pg_sessions,
        morning_enabled=True,
        evening_enabled=True,
        morning_time="08:00",
        evening_time="20:00",
    )
    with pg_sessions.begin() as db:
        db.get(Commitment, identifier).followup_enabled = False
    runtime = AgentRuntime()
    for day in range(2):
        for hour in (7, 19):
            runtime.clock = FakeClock(NOW.replace(hour=hour) + timedelta(days=day))
            assert tick(pg_sessions, runtime)["executed"] == 1
            assert tick(pg_sessions, runtime)["executed"] == 0
    with pg_sessions.begin() as db:
        assert db.scalar(select(func.count()).select_from(DailyBriefing)) == 2
        assert db.scalar(select(func.count()).select_from(ReflectionPrompt)) == 2
        assert db.scalar(select(func.count()).select_from(Message)) == 4
        user = db.get(User, owner)
        user.morning_enabled = user.evening_enabled = False
    runtime.clock = FakeClock(NOW + timedelta(days=2))
    assert tick(pg_sessions, runtime)["executed"] == 0


def test_s7_external_action_never_inherits_bridge_authorization(pg_sessions, monkeypatch):
    owner, _ = seed(pg_sessions)
    monkeypatch.setenv("CHANNEL_ACCOUNT_ID", "configured-does-not-authorize")
    with pg_sessions.begin() as db:
        db.get(User, owner).checkins_enabled = False
        db.add(
            ScheduledAction(
                user_id=owner,
                action_key="unapproved-send",
                kind="whatsapp_send",
                due_at=NOW,
                subject_version=0,
                preference_version=1,
            )
        )
    result = tick(pg_sessions, AgentRuntime(clock=FakeClock(NOW)))
    assert result["executed"] == 1
    with pg_sessions() as db:
        row = db.scalar(select(ScheduledAction))
        assert row.state == "skipped" and row.reason == "external_action_not_authorized"
        assert row.decision["delivery"] == "none" and row.message_id is None
        assert db.scalar(select(func.count()).select_from(Message)) == 0


def test_claimed_action_is_cancelled_before_effect_and_resume_never_replays_it(pg_sessions):
    owner, identifier = seed(pg_sessions)
    runtime = AgentRuntime(clock=FakeClock(NOW))

    def pause_after_claim(stage, _):
        if stage == "after_claim":
            with pg_sessions.begin() as db:
                lock_user(db, owner)
                result = change(
                    db,
                    owner,
                    ChangeCommitment(id=identifier, action="pause"),
                    "Pause",
                    "owner-control",
                    NOW,
                    trusted=True,
                )
                assert result["status"] == "updated"

    assert tick(pg_sessions, runtime, fault=pause_after_claim)["executed"] == 0
    with pg_sessions.begin() as db:
        assert db.scalar(select(ScheduledAction)).state == "cancelled"
        assert db.scalar(select(func.count()).select_from(Message)) == 0
        change(
            db,
            owner,
            ChangeCommitment(id=identifier, action="resume"),
            "Resume",
            "owner-control",
            NOW,
            trusted=True,
        )
        due = db.get(Commitment, identifier).next_relevant_moment
        assert utc(due) > NOW
    assert tick(pg_sessions, runtime)["executed"] == 0
    runtime.clock = FakeClock(due)
    assert tick(pg_sessions, runtime)["executed"] == 1
    with pg_sessions() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 1


def test_quiet_hours_daily_attention_and_finite_followups(pg_sessions):
    owner, identifier = seed(pg_sessions)
    with pg_sessions.begin() as db:
        item = db.get(Commitment, identifier)
        item.followup_minutes = 15
        item.max_followups = 5
        item.next_relevant_moment = NOW.replace(hour=21)
    runtime = AgentRuntime(clock=FakeClock(NOW.replace(hour=21)))
    tick(pg_sessions, runtime)
    with pg_sessions() as db:
        assert db.scalar(select(ScheduledAction)).reason == "quiet_hours_or_availability"
        assert db.scalar(select(func.count()).select_from(Message)) == 0
        due = db.get(Commitment, identifier).next_relevant_moment
    runtime.clock = FakeClock(due)
    for _ in range(6):
        tick(pg_sessions, runtime)
        with pg_sessions() as db:
            due = db.get(Commitment, identifier).next_relevant_moment
            if due is None:
                break
        runtime.clock = FakeClock(due)
    with pg_sessions() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 5
        assert db.get(Commitment, identifier).next_relevant_moment is None
        assert "daily_attention_limit" in set(db.scalars(select(ScheduledAction.reason)))


def test_tools_require_write_permission_and_disambiguation(pg_client, monkeypatch):
    main, gateway, item = capture(pg_client, monkeypatch)
    gateway.scripts.append(
        reply(
            "",
            tool(
                "update_commitment",
                {
                    "id": item["id"],
                    "action": "complete",
                    "reference": "presentation",
                },
            ),
        )
    )
    answer = send(pg_client, "no-write", "I've finished the presentation.")
    assert "write_permission_required" in answer["content"]
    assert pg_client.get("/api/autonomy").json()["commitments"][0]["state"] == "open"
    with main.SessionLocal.begin() as db:
        owner = db.scalar(select(User))
        db.add(
            Commitment(
                user_id=owner.id, title="Other presentation", confidence=1, derivation="test"
            )
        )
    gateway.scripts.append(
        reply(
            "",
            tool(
                "update_commitment",
                {
                    "reference": "presentation",
                    "action": "complete",
                },
            ),
        )
    )
    answer = send(
        pg_client, "ambiguous", "I've finished the presentation.", allow_memory_write=True
    )
    assert "ambiguous_reference" in answer["content"]
    assert all(
        row["state"] == "open" for row in pg_client.get("/api/autonomy").json()["commitments"]
    )
    # A model can provide ID/version alone when the owner names the full title.
    # Another open commitment must not make that explicit reference ambiguous.
    gateway.scripts.extend(
        [
            reply(
                "",
                tool(
                    "update_commitment",
                    {
                        "id": item["id"],
                        "version": item["version"],
                        "deadline_text": "Thursday at 6pm",
                    },
                ),
            ),
            reply("Updated the named presentation deadline."),
        ]
    )
    send(
        pg_client,
        "named-correction",
        f"For {item['title']}, the deadline moved to Thursday at 6pm.",
        allow_memory_write=True,
    )
    current = pg_client.get("/api/autonomy").json()["commitments"]
    changed = next(row for row in current if row["id"] == item["id"])
    assert changed["due_local"] == "2026-10-29T18:00:00+01:00"
    assert next(row for row in current if row["id"] != item["id"])["due_at"] is None
    gateway.scripts.append(
        reply(
            "",
            tool(
                "update_commitment",
                {
                    "id": item["id"],
                    "deadline_text": "Friday at 6pm",
                    "response_text": "I've moved the selected deadline to Friday.",
                },
            ),
        )
    )
    answer = send(
        pg_client, "unnamed-correction", "The deadline is Friday at 6pm.", allow_memory_write=True
    )
    assert "Updated commitment" in answer["content"]
    with main.SessionLocal.begin() as db:
        owner = db.scalar(select(User))
        db.add(Commitment(user_id=owner.id, title=item["title"], confidence=1, derivation="test"))
    gateway.scripts.append(
        reply(
            "",
            tool(
                "update_commitment",
                {
                    "reference": "presentation",
                    "deadline_text": "Friday at 6pm",
                },
            ),
        )
    )
    answer = send(
        pg_client,
        "duplicate-title",
        f"For {item['title']}, move it to Friday at 6pm.",
        allow_memory_write=True,
    )
    assert "ambiguous_reference" in answer["content"]
    assert (
        pg_client.post(
            f"/api/commitments/{item['id']}/lifecycle",
            headers=csrf(pg_client),
            json={
                "action": "update",
                "version": 99,
                "event_id": "stale",
                "next_action": "Wrong stale edit",
            },
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    ("phrase", "expected", "bound"),
    [
        ("before 9pm tomorrow", "2026-10-27T20:00:00+00:00", "exclusive"),
        ("Thursday at 6pm", "2026-10-29T17:00:00+00:00", "inclusive"),
        ("next Friday afternoon", None, None),
        ("tomorrow at 6", None, None),
        ("2026-11-01 01:30", None, None),
        ("2026-03-08 02:30", None, None),
    ],
)
def test_deadline_bound_ambiguity_and_dst(phrase, expected, bound):
    zone = "America/New_York" if phrase.startswith("2026-") else "Africa/Lagos"
    parsed = parse_commitment_timing(phrase, NOW, zone)
    assert (parsed.due_at.isoformat() if parsed.due_at else None) == expected
    if expected:
        assert parsed.bound == bound and parsed.ambiguity == "none"
    else:
        assert parsed.ambiguity != "none"
