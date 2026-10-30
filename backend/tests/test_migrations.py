import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker


def run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ | {"DATABASE_URL": database_url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def table_names(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    return {row[0] for row in rows}


def column_names(database_path: Path, table: str) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def test_database_url_overrides_ini_for_upgrade_and_downgrade(tmp_path: Path):
    database_path = tmp_path / "migration.db"
    database_url = f"sqlite:///{database_path}"

    upgraded = run_alembic(database_url, "upgrade", "head")

    assert upgraded.returncode == 0, upgraded.stderr
    assert {"users", "messages", "alembic_version"} <= table_names(database_path)
    assert database_url not in upgraded.stdout + upgraded.stderr

    downgraded = run_alembic(database_url, "downgrade", "base")

    assert downgraded.returncode == 0, downgraded.stderr
    assert "users" not in table_names(database_path)
    assert database_url not in downgraded.stdout + downgraded.stderr


def test_database_url_is_converted_to_a_sync_driver(tmp_path: Path):
    database_path = tmp_path / "async-configured.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"

    upgraded = run_alembic(database_url, "upgrade", "head")

    assert upgraded.returncode == 0, upgraded.stderr
    assert "users" in table_names(database_path)
    assert database_url not in upgraded.stdout + upgraded.stderr


def test_initial_migration_uses_explicit_alembic_operations():
    migration = Path("alembic/versions/0001_persistent_continuity.py").read_text()

    assert "op.create_table" in migration
    assert "op.drop_table" in migration
    assert "metadata.create_all" not in migration
    assert "metadata.drop_all" not in migration


def test_daily_loop_migration_upgrades_and_downgrades_explicitly(tmp_path: Path):
    database_path = tmp_path / "daily-loop.db"
    database_url = f"sqlite:///{database_path}"

    upgraded = run_alembic(database_url, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    assert {"daily_briefings", "journal_entries", "reflection_prompts"} <= table_names(
        database_path
    )
    assert "timezone" in column_names(database_path, "users")
    assert "priority" in column_names(database_path, "commitments")

    downgraded = run_alembic(database_url, "downgrade", "0001")
    assert downgraded.returncode == 0, downgraded.stderr
    assert "users" in table_names(database_path)
    assert "journal_entries" not in table_names(database_path)
    assert "timezone" not in column_names(database_path, "users")
    migration = Path("alembic/versions/0002_daily_continuity_loop.py").read_text()
    assert "op.create_table" in migration
    assert "op.drop_table" in migration


def test_vocabulary_migration_is_explicit_and_reversible(tmp_path: Path):
    database_path = tmp_path / "vocabulary.db"
    database_url = f"sqlite:///{database_path}"
    upgraded = run_alembic(database_url, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    assert {"vocabulary_items", "vocabulary_attempts"} <= table_names(database_path)

    downgraded = run_alembic(database_url, "downgrade", "0005")
    assert downgraded.returncode == 0, downgraded.stderr
    assert "vocabulary_items" not in table_names(database_path)
    assert "vocabulary_attempts" not in table_names(database_path)

    reupgraded = run_alembic(database_url, "upgrade", "head")
    assert reupgraded.returncode == 0, reupgraded.stderr
    assert {"vocabulary_items", "vocabulary_attempts"} <= table_names(database_path)


def test_m5_thread_states_are_loss_aware_on_real_postgres_downgrade():
    database_url = os.getenv("POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("POSTGRES_TEST_URL is required for PostgreSQL migration tests")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    assert run_alembic(database_url, "upgrade", "0007").returncode == 0
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id,username,created_at,timezone) VALUES "
                "('state-user','state-owner',CURRENT_TIMESTAMP,'UTC')"
            )
        )
        for state in ("exploring", "waiting", "completed"):
            connection.execute(
                text(
                    "INSERT INTO threads (id,user_id,created_at,title,state,confidence,"
                    "derivation,version,priority,deleted,description,importance,next_action,watch) "
                    "VALUES (:id,'state-user',CURRENT_TIMESTAMP,:title,:state,1,'test',"
                    "1,0,false,'',0,'',false)"
                ),
                {"id": f"thread-{state}", "title": state, "state": state},
            )
    engine.dispose()
    downgraded = run_alembic(database_url, "downgrade", "0006")
    assert downgraded.returncode == 0, downgraded.stderr
    engine = create_engine(database_url)
    with engine.connect() as connection:
        states = {
            row.id: row.state
            for row in connection.execute(text("SELECT id,state FROM threads"))
        }
    engine.dispose()
    assert states == {
        "thread-exploring": "active",
        "thread-waiting": "active",
        "thread-completed": "dormant",
    }
    assert run_alembic(database_url, "upgrade", "head").returncode == 0


def seed_duplicate_conversations(database_url: str) -> None:
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, created_at, timezone) "
                "VALUES ('user-1', 'owner', CURRENT_TIMESTAMP, 'UTC')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO conversation_sessions "
                "(id, user_id, created_at, last_activity_at) VALUES "
                "('conversation-1', 'user-1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                "('conversation-2', 'user-1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO messages "
                "(id, user_id, created_at, conversation_id, role, content, actor, ordinal) VALUES "
                "('message-1', 'user-1', CURRENT_TIMESTAMP, 'conversation-1', "
                "'user', 'first', 'user', 1), "
                "('message-2', 'user-1', CURRENT_TIMESTAMP, 'conversation-2', "
                "'assistant', 'second', 'assistant', 1)"
            )
        )
    engine.dispose()


def assert_conversation_constraints(database_url: str) -> None:
    engine = create_engine(database_url)
    inspector = inspect(engine)
    conversation_uniques = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("conversation_sessions")
    }
    message_uniques = {
        tuple(item["column_names"]) for item in inspector.get_unique_constraints("messages")
    }
    with engine.connect() as connection:
        conversations = connection.execute(
            text("SELECT id FROM conversation_sessions WHERE user_id = 'user-1'")
        ).all()
        messages = connection.execute(
            text(
                "SELECT conversation_id, ordinal FROM messages "
                "WHERE user_id = 'user-1' ORDER BY ordinal"
            )
        ).all()
    engine.dispose()
    assert len(conversations) == 1
    assert ("user_id",) in conversation_uniques
    assert ("conversation_id", "ordinal") in message_uniques
    assert messages == [(conversations[0][0], 1), (conversations[0][0], 2)]


def test_conversation_invariants_migrate_existing_rows_and_downgrade(tmp_path: Path):
    database_path = tmp_path / "conversation-invariants.db"
    database_url = f"sqlite:///{database_path}"
    assert run_alembic(database_url, "upgrade", "0002").returncode == 0
    seed_duplicate_conversations(database_url)

    upgraded = run_alembic(database_url, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    assert_conversation_constraints(database_url)

    downgraded = run_alembic(database_url, "downgrade", "0002")
    assert downgraded.returncode == 0, downgraded.stderr
    migration = Path("alembic/versions/0003_conversation_concurrency.py").read_text()
    assert "create_unique_constraint" in migration
    assert "drop_constraint" in migration


def test_conversation_invariant_migration_cycle_on_real_postgres():
    database_url = os.getenv("POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("POSTGRES_TEST_URL is required for PostgreSQL migration tests")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()

    assert run_alembic(database_url, "upgrade", "0002").returncode == 0
    seed_duplicate_conversations(database_url)
    upgraded = run_alembic(database_url, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    assert_conversation_constraints(database_url)
    assert run_alembic(database_url, "downgrade", "0002").returncode == 0
    assert run_alembic(database_url, "upgrade", "head").returncode == 0


@pytest.mark.parametrize(
    "legacy_fields",
    [
        pytest.param(("content", "kind", "user_id"), id="original-three-field"),
        pytest.param(
            ("content", "intended_local_date", "kind", "user_id"),
            id="later-four-field",
        ),
    ],
)
def test_legacy_0002_http_message_replays_and_upgrades_safely_on_postgres(
    monkeypatch, legacy_fields: tuple[str, ...]
):
    database_url = os.getenv("POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("POSTGRES_TEST_URL is required for PostgreSQL migration tests")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()

    assert run_alembic(database_url, "upgrade", "0002").returncode == 0
    legacy_request = {
        "content": "Hello from before 0003",
        "intended_local_date": None,
        "kind": "user_message",
        "user_id": "legacy-user",
    }
    canonical_legacy_request = {field: legacy_request[field] for field in legacy_fields}
    legacy_fingerprint = hashlib.sha256(
        json.dumps(
            canonical_legacy_request, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, created_at, timezone) "
                "VALUES ('legacy-user', 'owner', CURRENT_TIMESTAMP, 'UTC')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO conversation_sessions "
                "(id, user_id, created_at, last_activity_at) VALUES "
                "('legacy-conversation', 'legacy-user', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_events "
                "(id, user_id, created_at, external_id, kind, request_fingerprint, status) "
                "VALUES ('legacy-agent-event', 'legacy-user', CURRENT_TIMESTAMP, "
                "'legacy-http-event', 'user_message', :fingerprint, 'handled')"
            ),
            {"fingerprint": legacy_fingerprint},
        )
        connection.execute(
            text(
                "INSERT INTO messages "
                "(id, user_id, created_at, conversation_id, role, content, actor, ordinal) VALUES "
                "('legacy-user-message', 'legacy-user', CURRENT_TIMESTAMP, "
                "'legacy-conversation', 'user', 'Hello from before 0003', 'user', 1), "
                "('legacy-assistant-message', 'legacy-user', CURRENT_TIMESTAMP, "
                "'legacy-conversation', 'assistant', 'Hello!', 'assistant', 2)"
            )
        )
    engine.dispose()

    upgraded = run_alembic(database_url, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr

    from app.agent import AgentRuntime, IdempotencyConflict, RuntimeEvent

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, created_at, timezone) "
                "VALUES ('other-user', 'other', CURRENT_TIMESTAMP, 'UTC')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO conversation_sessions "
                "(id, user_id, created_at, last_activity_at) VALUES "
                "('other-conversation', 'other-user', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    sessions = sessionmaker(engine)
    legacy_mismatches = [
        RuntimeEvent(
            "legacy-http-event",
            "user_message",
            "Altered content",
            "legacy-conversation",
        ),
        RuntimeEvent("legacy-http-event", "dormancy_scan"),
        RuntimeEvent(
            "legacy-http-event",
            "user_message",
            "Hello from before 0003",
            "other-conversation",
        ),
        RuntimeEvent(
            "legacy-http-event",
            "user_message",
            "Hello from before 0003",
            "legacy-conversation",
            date(2026, 10, 1),
        ),
    ]
    for mismatch in legacy_mismatches:
        with sessions() as db, pytest.raises(IdempotencyConflict):
            AgentRuntime().handle(db, "legacy-user", mismatch)
    engine.dispose()

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("ACCESS_CODE", "correct horse battery staple")
    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://testserver")
    import app.main

    app.main.engine.dispose()
    app.main = importlib.reload(app.main)
    with TestClient(app.main.app) as client:
        login = client.post(
            "/api/auth/login",
            json={"access_code": "correct horse battery staple", "username": "owner"},
            headers={"origin": "http://testserver"},
        )
        assert login.status_code == 200
        headers = {
            "origin": "http://testserver",
            "x-csrf-token": client.cookies.get("csrf_token"),
        }
        replay = client.post(
            "/api/chat/messages",
            json={"event_id": "legacy-http-event", "content": "Hello from before 0003"},
            headers=headers,
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent"] is True
        assert client.post(
            "/api/chat/messages",
            json={"event_id": "legacy-http-event", "content": "Altered content"},
            headers=headers,
        ).status_code == 409
        assert client.post(
            "/api/events/scheduled",
            json={"event_id": "legacy-http-event", "kind": "dormancy_scan"},
            headers=headers,
        ).status_code == 409
    app.main.engine.dispose()

    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM agent_events")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM messages")).scalar_one() == 2
        upgraded_fingerprint: str = connection.execute(
            text("SELECT request_fingerprint FROM agent_events WHERE id = 'legacy-agent-event'")
        ).scalar_one()
    engine.dispose()
    assert upgraded_fingerprint.startswith("v2:")

    assert run_alembic(database_url, "downgrade", "0002").returncode == 0
    assert run_alembic(database_url, "upgrade", "head").returncode == 0

    engine = create_engine(database_url)
    sessions = sessionmaker(engine)
    with sessions() as db, pytest.raises(IdempotencyConflict):
        AgentRuntime().handle(
            db,
            "legacy-user",
            RuntimeEvent(
                "legacy-http-event",
                "user_message",
                "Hello from before 0003",
                "other-conversation",
            ),
        )
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM agent_events")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM messages")).scalar_one() == 2
        assert connection.execute(
            text("SELECT request_fingerprint FROM agent_events WHERE id = 'legacy-agent-event'")
        ).scalar_one() == upgraded_fingerprint
    engine.dispose()


def test_prospective_memory_migration_upgrades_and_downgrades_explicitly(tmp_path: Path):
    database_path = tmp_path / "prospective-memory.db"
    database_url = f"sqlite:///{database_path}"

    upgraded = run_alembic(database_url, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    assert "reminders" in table_names(database_path)
    assert {
        "due_at",
        "due_granularity",
        "due_source_text",
        "estimated_effort_minutes",
        "importance",
        "next_relevant_moment",
        "last_surfaced_at",
        "last_touched_at",
        "ambiguity",
        "dependencies",
        "available_minutes",
        "related_thread_id",
        "completed_at",
    } <= column_names(database_path, "commitments")

    downgraded = run_alembic(database_url, "downgrade", "0003")
    assert downgraded.returncode == 0, downgraded.stderr
    assert "reminders" not in table_names(database_path)
    assert "due_at" not in column_names(database_path, "commitments")
    migration = Path("alembic/versions/0004_prospective_memory.py").read_text()
    assert "op.create_table" in migration
    assert "op.drop_table" in migration


def test_reminder_tenant_boundary_migration_is_explicit_and_reversible(tmp_path: Path):
    database_path = tmp_path / "reminder-tenant-boundary.db"
    database_url = f"sqlite:///{database_path}"

    upgraded = run_alembic(database_url, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    assert {
        "reminder_scan_cursor_created_at",
        "reminder_scan_cursor_id",
    } <= column_names(database_path, "conversation_sessions")
    engine = create_engine(database_url)
    reminder_fks = {
        (
            tuple(item["constrained_columns"]),
            item["referred_table"],
            tuple(item["referred_columns"]),
        )
        for item in inspect(engine).get_foreign_keys("reminders")
    }
    engine.dispose()
    assert (("user_id", "commitment_id"), "commitments", ("user_id", "id")) in reminder_fks
    assert (("user_id", "thread_id"), "threads", ("user_id", "id")) in reminder_fks
    assert (("user_id", "surfaced_message_id"), "messages", ("user_id", "id")) in reminder_fks
    assert (("user_id", "source_event_id"), "agent_events", ("user_id", "id")) in reminder_fks

    downgraded = run_alembic(database_url, "downgrade", "0004")
    assert downgraded.returncode == 0, downgraded.stderr
    assert "reminder_scan_cursor_id" not in column_names(
        database_path, "conversation_sessions"
    )


def test_tenant_safe_reminder_migration_preserves_valid_existing_rows_on_postgres():
    database_url = os.getenv("POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("POSTGRES_TEST_URL is required for PostgreSQL migration tests")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()

    assert run_alembic(database_url, "upgrade", "0004").returncode == 0
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, created_at, timezone) VALUES "
                "('tenant-user', 'tenant-owner', CURRENT_TIMESTAMP, 'UTC')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO conversation_sessions "
                "(id, user_id, created_at, last_activity_at) VALUES "
                "('tenant-conversation', 'tenant-user', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO threads "
                "(id, user_id, created_at, title, state, confidence, derivation, version, "
                "priority, deleted) VALUES "
                "('tenant-thread', 'tenant-user', CURRENT_TIMESTAMP, 'thread', 'active', 1, "
                "'test', 1, 0, false)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO commitments "
                "(id, user_id, created_at, last_touched_at, title, state, confidence, "
                "derivation, version, priority, deleted, importance, ambiguity, dependencies, "
                "related_thread_id) VALUES "
                "('tenant-commitment', 'tenant-user', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
                "'task', 'open', 1, 'test', 1, 0, false, 0, 'none', '[]'::json, "
                "'tenant-thread')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_events "
                "(id, user_id, created_at, external_id, kind, request_fingerprint, status) "
                "VALUES ('tenant-event', 'tenant-user', CURRENT_TIMESTAMP, 'event', "
                "'commitment_check', 'fingerprint', 'handled')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO messages "
                "(id, user_id, created_at, conversation_id, role, content, actor, ordinal) "
                "VALUES ('tenant-message', 'tenant-user', CURRENT_TIMESTAMP, "
                "'tenant-conversation', 'assistant', 'valid', 'assistant', 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO reminders "
                "(id, user_id, created_at, commitment_id, thread_id, policy_version, "
                "relevance_window, reason_code, reason_metadata, state, surfaced_message_id, "
                "source_event_id, attempt_count) VALUES "
                "('tenant-reminder', 'tenant-user', CURRENT_TIMESTAMP, 'tenant-commitment', "
                "'tenant-thread', 'm3-v1', 'window', 'test', '{}'::json, 'surfaced', "
                "'tenant-message', 'tenant-event', 1)"
            )
        )
    engine.dispose()

    upgraded = run_alembic(database_url, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM reminders")).scalar_one() == 1
    engine.dispose()
    assert run_alembic(database_url, "downgrade", "0004").returncode == 0
    assert run_alembic(database_url, "upgrade", "head").returncode == 0


def test_prospective_memory_backfills_last_touched_from_created_at_on_postgres():
    database_url = os.getenv("POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("POSTGRES_TEST_URL is required for PostgreSQL migration tests")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()

    assert run_alembic(database_url, "upgrade", "0003").returncode == 0
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, created_at, timezone) VALUES "
                "('touch-user', 'touch-owner', '2026-09-01T09:00:00Z', 'UTC')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO commitments "
                "(id, user_id, created_at, title, state, confidence, derivation, version, "
                "priority, deleted) VALUES "
                "('touch-commitment', 'touch-user', '2026-09-02T10:30:00Z', "
                "'legacy task', 'open', 1, 'test', 1, 0, false)"
            )
        )
    engine.dispose()

    upgraded = run_alembic(database_url, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    engine = create_engine(database_url)
    with engine.connect() as connection:
        created_at, last_touched_at = connection.execute(
            text(
                "SELECT created_at, last_touched_at FROM commitments "
                "WHERE id = 'touch-commitment'"
            )
        ).one()
        nullable = next(
            column["nullable"]
            for column in inspect(connection).get_columns("commitments")
            if column["name"] == "last_touched_at"
        )
    engine.dispose()
    assert last_touched_at == created_at
    assert nullable is False


def test_prospective_memory_postgres_downgrade_maps_m3_states_and_reupgrades():
    database_url = os.getenv("POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("POSTGRES_TEST_URL is required for PostgreSQL migration tests")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()

    upgraded = run_alembic(database_url, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, created_at, timezone) "
                "VALUES ('state-user', 'state-owner', CURRENT_TIMESTAMP, 'UTC')"
            )
        )
        for index, state in enumerate(
            (
                "open",
                "in_progress",
                "waiting",
                "complete",
                "completed",
                "cancelled",
                "abandoned",
                "dormant",
            )
        ):
            connection.execute(
                text(
                    "INSERT INTO commitments "
                    "(id, user_id, created_at, last_touched_at, title, state, confidence, "
                    "derivation, version, priority, deleted, importance, ambiguity, dependencies) "
                    "VALUES (:id, 'state-user', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :title, "
                    ":state, 1, 'test', 1, 0, false, 0, 'none', '[]'::json)"
                ),
                {"id": f"state-{index}", "title": state, "state": state},
            )
    engine.dispose()

    downgraded = run_alembic(database_url, "downgrade", "0003")
    assert downgraded.returncode == 0, downgraded.stderr
    engine = create_engine(database_url)
    with engine.connect() as connection:
        states = {
            row.title: row.state
            for row in connection.execute(text("SELECT title, state FROM commitments"))
        }
    engine.dispose()
    assert states == {
        "open": "open",
        "in_progress": "open",
        "waiting": "open",
        "complete": "complete",
        "completed": "complete",
        "cancelled": "abandoned",
        "abandoned": "abandoned",
        "dormant": "dormant",
    }

    reupgraded = run_alembic(database_url, "upgrade", "head")
    assert reupgraded.returncode == 0, reupgraded.stderr
    engine = create_engine(database_url)
    with engine.connect() as connection:
        reupgraded_states = {
            row.title: row.state
            for row in connection.execute(text("SELECT title, state FROM commitments"))
        }
    engine.dispose()
    assert reupgraded_states == states
