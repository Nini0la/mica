from datetime import datetime, timezone

from sqlalchemy import MetaData, create_engine, inspect, select, text
from test_migrations import run_alembic


def test_response_trace_migration_preserves_old_runs_without_fabricating_inputs(tmp_path):
    url = f"sqlite:///{tmp_path}/trace-migration.db"
    result = run_alembic(url, "upgrade", "0009")
    assert result.returncode == 0, result.stderr
    engine = create_engine(url)
    metadata = MetaData()
    metadata.reflect(engine)
    now = datetime.now(timezone.utc)
    with engine.begin() as db:
        db.execute(metadata.tables["users"].insert().values(
            id="user", username="synthetic", created_at=now,
        ))
        db.execute(metadata.tables["agent_events"].insert().values(
            id="event", user_id="user", created_at=now, external_id="external",
            kind="user_message", request_fingerprint="fingerprint", status="handled",
        ))
        db.execute(metadata.tables["assistance_runs"].insert().values(
            id="run", user_id="user", created_at=now, event_id="event",
            intervention_type="contextual_help", execution_status="delivered",
        ))
    result = run_alembic(url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    with engine.connect() as db:
        assert db.execute(text(
            "SELECT context_trace, trace_status FROM assistance_runs WHERE id='run'"
        )).one() == (None, "not_recorded")
    assert "ix_assistance_runs_user_message" in {
        entry["name"] for entry in inspect(engine).get_indexes("assistance_runs")
    }
    result = run_alembic(url, "downgrade", "0009")
    assert result.returncode == 0, result.stderr
    assert "context_trace" not in {
        column["name"] for column in inspect(engine).get_columns("assistance_runs")
    }
    with engine.connect() as db:
        assert db.scalar(select(metadata.tables["assistance_runs"].c.id)) == "run"
    result = run_alembic(url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    with engine.connect() as db:
        assert db.scalar(text("SELECT trace_status FROM assistance_runs")) == "not_recorded"
    engine.dispose()
