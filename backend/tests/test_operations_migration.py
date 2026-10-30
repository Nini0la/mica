from datetime import datetime, timezone

from sqlalchemy import MetaData, create_engine, inspect, text
from test_migrations import run_alembic


def test_operations_migration_is_reversible_and_preserves_existing_commitments(tmp_path):
    url = f"sqlite:///{tmp_path}/operations.db"
    assert run_alembic(url, "upgrade", "0010").returncode == 0
    engine = create_engine(url)
    metadata = MetaData()
    metadata.reflect(engine)
    now = datetime.now(timezone.utc)
    with engine.begin() as db:
        db.execute(
            metadata.tables["users"].insert().values(id="user", username="owner", created_at=now)
        )
        db.execute(
            metadata.tables["commitments"]
            .insert()
            .values(
                id="commitment",
                user_id="user",
                created_at=now,
                last_touched_at=now,
                title="Keep existing work",
                state="open",
                confidence=1,
                derivation="user-stated",
                version=1,
                priority=0,
                deleted=False,
            )
        )
    result = run_alembic(url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    names = set(inspect(engine).get_table_names())
    assert {
        "responsibilities",
        "channel_accounts",
        "channel_messages",
        "responsibility_events",
    } <= names
    with engine.connect() as db:
        assert db.scalar(text("SELECT title FROM commitments")) == "Keep existing work"
    result = run_alembic(url, "downgrade", "0010")
    assert result.returncode == 0, result.stderr
    assert "responsibilities" not in inspect(engine).get_table_names()
    assert run_alembic(url, "upgrade", "head").returncode == 0
    engine.dispose()
