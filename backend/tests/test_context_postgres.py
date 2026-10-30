"""Destructive integration regressions; POSTGRES_TEST_URL must be disposable."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from queue import Queue
from threading import Event

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, event, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from test_migrations import run_alembic
from test_postgres_quality import csrf, login
from test_postgres_quality import pg_client as pg_client
from test_postgres_quality import postgres_url as postgres_url

from app.context import build_context
from app.gateway import ModelReply
from app.models import ConversationSession, Message, Thread, ThreadRevision, User


def test_0009_preserves_legacy_data_defaults_and_tenant_constraints(postgres_url):
    engine = create_engine(postgres_url)
    old = datetime(2020, 1, 2, 12, tzinfo=timezone.utc)
    new_tables = {"thread_revisions", "assistance_runs", "assistance_feedback", "growth_events"}
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        result = run_alembic(postgres_url, "upgrade", "0008")
        assert result.returncode == 0, result.stderr
        legacy = MetaData()
        legacy.reflect(engine)
        seeded = {}
        with engine.begin() as connection:
            for tenant in ("owner", "other"):
                rows = {
                    "users": {"username": tenant, "timezone": "UTC"},
                    "conversation_sessions": {"last_activity_at": old},
                    "messages": {
                        "conversation_id": f"{tenant}-conversation_sessions",
                        "role": "assistant", "actor": "assistant", "ordinal": 1,
                        "content": "Obsolete venue: the lighthouse.",
                    },
                    "threads": {
                        "title": "Audio walk", "description": "Recorded interviews",
                        "state": "active", "confidence": 1, "derivation": "test",
                        "version": 3, "priority": 0, "deleted": False,
                        "importance": 1, "next_action": "Interview residents", "watch": True,
                    },
                    "commitments": {
                        "title": "Prepare interviews", "state": "open", "confidence": 1,
                        "derivation": "test", "version": 2, "priority": 0, "deleted": False,
                        "importance": 1, "ambiguity": "none", "dependencies": [],
                        "last_touched_at": old, "related_thread_id": f"{tenant}-threads",
                    },
                    "agent_events": {
                        "external_id": "legacy-event", "kind": "user_message",
                        "request_fingerprint": "legacy", "status": "handled",
                    },
                    "journal_entries": {
                        "local_date": old.date(), "original_content": "Original journal",
                        "corrected_content": "Corrected journal", "summary": "Interview day",
                        "structured_state": {"completed": ["interview"]}, "updated_at": old,
                        "source_message_id": f"{tenant}-messages",
                        "source_event_id": f"{tenant}-agent_events", "version": 2,
                        "deleted": False,
                    },
                    "episodes": {
                        "summary": "Interviewed residents", "actor": "user", "confidence": 1,
                        "derivation": "test", "source": "explicit episode", "version": 1,
                        "deleted": False,
                    },
                }
                for name, values in rows.items():
                    values.update(id=f"{tenant}-{name}", created_at=old)
                    if name != "users":
                        values["user_id"] = f"{tenant}-users"
                    connection.execute(legacy.tables[name].insert().values(**values))
            for name in rows:
                table = legacy.tables[name]
                seeded[name] = connection.execute(select(table).order_by(table.c.id)).all()

        for cycle in range(2):
            result = run_alembic(postgres_url, "upgrade", "head")
            assert result.returncode == 0, result.stderr
            inspector = inspect(engine)
            assert new_tables <= set(inspector.get_table_names())
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0014"
                for name, expected in seeded.items():
                    table = legacy.tables[name]
                    actual = connection.execute(select(table).where(
                        table.c.id.in_([row.id for row in expected])
                    ).order_by(table.c.id)).all()
                    assert actual == expected, name
                users = connection.execute(text(
                    "SELECT context_reset_at, pet_name, temperament FROM users "
                    "WHERE id IN ('owner-users', 'other-users')"
                )).all()
                assert len(users) == 2
                assert all(cutoff > old and (name, mood) == ("Mica", "calm")
                           for cutoff, name, mood in users)
                assert connection.execute(text(
                    "SELECT kind, why, epistemic_status, parent_thread_id FROM threads"
                )).all() == [("thread", "", "fact", None)] * 2

            sessions = sessionmaker(engine)
            with sessions() as db:
                context = build_context(db, "owner-users", "Audio walk", "owner-threads")
                assert context.payload["recent_conversation"] == []
                assert any(record["id"] == "owner-threads"
                           for record in context.payload["records"])
                assert db.get(Message, "owner-messages").content == (
                    "Obsolete venue: the lighthouse."
                )

            current = MetaData()
            current.reflect(engine)
            with engine.begin() as connection:
                # Reflected inserts exercise server defaults, not ORM Python defaults.
                connection.execute(current.tables["users"].insert().values(
                    id=f"new-{cycle}", username=f"new-{cycle}", created_at=old, timezone="UTC",
                ))
                assert connection.scalar(text(
                    "SELECT context_reset_at FROM users WHERE id = :id"
                ), {"id": f"new-{cycle}"}) is None
                for tenant in ("owner", "other"):
                    rows = {
                        "thread_revisions": {
                            "thread_id": f"{tenant}-threads", "version": 3,
                            "snapshot": {"title": "Audio walk"}, "source_type": "test",
                        },
                        "assistance_runs": {
                            "event_id": f"{tenant}-agent_events",
                            "message_id": f"{tenant}-messages",
                            "related_thread_id": f"{tenant}-threads",
                            "related_commitment_id": f"{tenant}-commitments",
                            "intervention_type": "contextual_help",
                        },
                        "assistance_feedback": {
                            "run_id": f"{tenant}-assistance_runs", "external_id": "feedback",
                            "verdict": "helpful",
                        },
                        "growth_events": {
                            "evidence_id": f"{tenant}-assistance_feedback",
                            "dimension": "judgement", "event_type": "helpfulness_reported",
                        },
                    }
                    for name, values in rows.items():
                        connection.execute(current.tables[name].insert().values(
                            id=f"{tenant}-{name}", user_id=f"{tenant}-users", created_at=old,
                            **values,
                        ))
                relationships = [
                    ("threads", "parent_thread_id", "threads", "fk_threads_user_parent_thread"),
                    ("thread_revisions", "thread_id", "threads", "fk_thread_revisions_user_thread"),
                    ("assistance_runs", "event_id", "agent_events",
                     "fk_assistance_runs_user_event"),
                    ("assistance_runs", "message_id", "messages",
                     "fk_assistance_runs_user_message"),
                    ("assistance_runs", "related_thread_id", "threads",
                     "fk_assistance_runs_user_thread"),
                    ("assistance_runs", "related_commitment_id", "commitments",
                     "fk_assistance_runs_user_commitment"),
                    ("assistance_feedback", "run_id", "assistance_runs",
                     "fk_assistance_feedback_user_run"),
                    ("growth_events", "evidence_id", "assistance_feedback",
                     "fk_growth_events_user_evidence"),
                ]
                for name, column, target, constraint in relationships:
                    fks = {fk["name"]: fk for fk in inspector.get_foreign_keys(name)}
                    assert fks[constraint]["constrained_columns"] == ["user_id", column]
                    assert fks[constraint]["referred_table"] == target
                    assert fks[constraint]["referred_columns"] == ["user_id", "id"]
                    table = current.tables[name]
                    with pytest.raises(IntegrityError) as rejected, connection.begin_nested():
                        connection.execute(table.update().where(
                            table.c.id == f"owner-{name}"
                        ).values({column: f"other-{target}"}))
                    assert rejected.value.orig.sqlstate == "23503"
                    assert rejected.value.orig.diag.constraint_name == constraint
                for name, column, value, constraint in [
                    ("threads", "kind", "invalid", "ck_threads_kind"),
                    ("threads", "epistemic_status", "invalid", "ck_threads_epistemic_status"),
                    ("assistance_feedback", "verdict", "invalid", "ck_assistance_feedback_verdict"),
                    ("growth_events", "dimension", "invalid", "ck_growth_events_dimension"),
                    ("growth_events", "assessment", "invalid", "ck_growth_events_assessment"),
                ]:
                    table = current.tables[name]
                    with pytest.raises(IntegrityError) as rejected, connection.begin_nested():
                        connection.execute(table.update().where(
                            table.c.id == f"owner-{name}"
                        ).values({column: value}))
                    assert rejected.value.orig.sqlstate == "23514"
                    assert rejected.value.orig.diag.constraint_name == constraint

            if cycle == 0:
                result = run_alembic(postgres_url, "downgrade", "0008")
                assert result.returncode == 0, result.stderr
                inspector = inspect(engine)
                assert not new_tables.intersection(inspector.get_table_names())
                assert "context_reset_at" not in {
                    column["name"] for column in inspector.get_columns("users")
                }
                assert not {"kind", "why", "epistemic_status", "parent_thread_id"}.intersection(
                    column["name"] for column in inspector.get_columns("threads")
                )
                with engine.connect() as connection:
                    for name, expected in seeded.items():
                        table = legacy.tables[name]
                        assert connection.execute(select(table).where(
                            table.c.id.in_([row.id for row in expected])
                        ).order_by(table.c.id)).all() == expected, name
    finally:
        engine.dispose()


@pytest.fixture()
def deterministic_model(monkeypatch):
    monkeypatch.setenv("MODEL_MODE", "deterministic")


@pytest.mark.usefixtures("deterministic_model")
@pytest.mark.parametrize("existing_conversation", [True, False], ids=["existing", "get-or-create"])
def test_api_correction_waits_for_generation_before_reading_memory(
    pg_client, monkeypatch, existing_conversation,
):
    import app.main as main

    assert login(pg_client).status_code == 200
    user_id = pg_client.get("/api/auth/me").json()["id"]
    obsolete = "Obsolete venue: the lighthouse."
    corrected = "Current venue: the riverside."
    with main.SessionLocal.begin() as db:
        project = Thread(user_id=user_id, title="Audio walk", description=obsolete,
                         kind="project", derivation="test")
        db.add(project)
        db.flush()
        project_id = project.id
        if existing_conversation:
            conversation = ConversationSession(user_id=user_id)
            db.add(conversation)
            db.flush()
            db.add(Message(user_id=user_id, conversation_id=conversation.id, ordinal=1,
                           role="assistant", actor="assistant", content=obsolete))

    entered_model, release_model, correction_read = Event(), Event(), Event()
    pids = Queue()
    calls = []
    native_call = {
        "id": "read-context-1", "type": "function",
        "function": {"name": "retrieve_context", "arguments": "{}"},
    }

    class WaitingGateway:
        def complete(self, messages, tools):
            calls.append(deepcopy(messages))
            if len(calls) == 1:
                entered_model.set()
                assert release_model.wait(15), "test did not release the model"
                content, tool_calls = "", [native_call]
            elif len(calls) == 2:
                assert tools == []
                receipts = json.loads(messages[-1]["content"].split("\n", 1)[1])
                context = json.loads(messages[1]["content"].split("\n", 1)[1])
                assert receipts == [
                    {"tool": "retrieve_context", "status": "read", "context": context},
                ]
                content, tool_calls = obsolete, []
            else:
                content, tool_calls = corrected, []
            return ModelReply(content=content, tool_calls=tool_calls, model="controlled-test",
                              input_tokens=10, output_tokens=10, latency_ms=1, cost_usd=None)

    def track_thread_read(execution):
        if execution.is_select and any(
            item.get("entity") is Thread for item in execution.statement.column_descriptions
        ):
            correction_read.set()

    def request_session(request: Request):
        with main.SessionLocal() as db:
            # Bound failures locally so executor cleanup cannot hang on a broken lock order.
            db.execute(text("SET LOCAL statement_timeout = '20s'"))
            pid = db.scalar(text("SELECT pg_backend_pid()"))
            if request.method == "PATCH":
                event.listen(db, "do_orm_execute", track_thread_read)
            if request.method in {"POST", "PATCH"}:
                pids.put(pid)
            yield db

    monkeypatch.setattr(main.runtime, "gateway", WaitingGateway())
    monkeypatch.setitem(main.app.dependency_overrides, main.db_session, request_session)
    headers = csrf(pg_client)
    with TestClient(main.app) as correcting_client, ThreadPoolExecutor(max_workers=2) as pool:
        correcting_client.cookies.update(pg_client.cookies)
        try:
            generation = pool.submit(
                pg_client.post, "/api/chat/messages", headers=headers,
                json={"event_id": "waiting-generation", "content": "Help develop this further.",
                      "active_thread_id": project_id},
            )
            assert entered_model.wait(10), "generation did not reach the fake gateway"
            generation_pid = pids.get(timeout=10)
            correction = pool.submit(
                correcting_client.patch, f"/api/threads/{project_id}", headers=headers,
                json={"title": "Audio walk", "description": corrected, "version": 1},
            )
            correction_pid = pids.get(timeout=10)
            assert correction_pid != generation_pid
            deadline = time.monotonic() + 10
            blocked = False
            while time.monotonic() < deadline:
                with main.SessionLocal() as observer:
                    blocked = observer.scalar(text(
                        "SELECT :blocker = ANY(pg_blocking_pids(pid)) "
                        "AND wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"
                    ), {"blocker": generation_pid, "pid": correction_pid})
                if blocked or correction.done():
                    break
                release_model.wait(0.01)
            assert blocked, "correction did not block on the generation transaction"
            assert not correction.done()
            assert not correction_read.is_set(), "correction read memory before the tenant lock"
        finally:
            # Release before ThreadPoolExecutor.__exit__ waits for either HTTP request.
            release_model.set()
        generated = generation.result(timeout=25)
        response = correction.result(timeout=25)

    assert generated.status_code == 201, generated.text
    assert response.status_code == 200, response.text
    assert correction_read.is_set()
    assert (response.json()["description"], response.json()["version"]) == (corrected, 2)
    with main.SessionLocal() as db:
        assert len(db.scalars(select(ConversationSession).where(
            ConversationSession.user_id == user_id
        )).all()) == 1
        stored = db.get(Thread, project_id)
        assert (stored.description, stored.version) == (corrected, 2)
        cutoff = db.get(User, user_id).context_reset_at
        assert cutoff is not None
        messages = db.scalars(select(Message).where(
            Message.user_id == user_id
        ).order_by(Message.ordinal)).all()
        assert messages[-1].role == "assistant"
        assert obsolete in messages[-1].content
        assert all(message.created_at < cutoff for message in messages)
        revisions = db.scalars(select(ThreadRevision).where(
            ThreadRevision.thread_id == project_id
        ).order_by(ThreadRevision.version)).all()
        assert [(revision.version, revision.snapshot["description"]) for revision in revisions] == [
            (1, obsolete), (2, corrected),
        ]
        context = build_context(db, user_id, "Continue this", project_id)
        assert context.payload["recent_conversation"] == []
        assert obsolete not in json.dumps(context.payload)
        assert corrected in json.dumps(context.payload)

    followup = pg_client.post(
        "/api/chat/messages", headers=headers,
        json={"event_id": "after-correction", "content": "Continue this",
              "active_thread_id": project_id},
    )
    assert followup.status_code == 201, followup.text
    assert len(calls) == 3
    assert obsolete in json.dumps(calls[0])
    assert obsolete not in json.dumps(calls[-1])
    assert corrected in json.dumps(calls[-1])
