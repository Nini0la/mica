from datetime import datetime, timezone

from sqlalchemy import MetaData, create_engine, inspect, text
from test_migrations import run_alembic


def test_0012_preserves_legacy_records_and_disables_by_default_with_safe_rollback(tmp_path):
    url = f"sqlite:///{tmp_path}/autonomy.db"
    assert run_alembic(url, "upgrade", "0011").returncode == 0
    engine = create_engine(url)
    metadata = MetaData()
    metadata.reflect(engine)
    now = datetime.now(timezone.utc)
    with engine.begin() as db:
        db.execute(
            metadata.tables["users"]
            .insert()
            .values(
                id="owner",
                username="owner",
                created_at=now,
            )
        )
        db.execute(
            metadata.tables["commitments"]
            .insert()
            .values(
                id="legacy",
                user_id="owner",
                title="Keep original purpose",
                state="open",
                confidence=1,
                derivation="user-stated",
                version=4,
                priority=0,
                deleted=False,
                created_at=now,
                last_touched_at=now,
            )
        )
    for _ in range(2):
        result = run_alembic(url, "upgrade", "0012")
        assert result.returncode == 0, result.stderr
        with engine.begin() as db:
            assert db.execute(
                text("SELECT checkins_enabled, morning_enabled, evening_enabled FROM users")
            ).one() == (0, 0, 0)
            assert db.execute(
                text("SELECT title, version, followup_enabled FROM commitments")
            ).one() == ("Keep original purpose", 4, 0)
            db.execute(text("UPDATE commitments SET state='paused'"))
        result = run_alembic(url, "downgrade", "0011")
        assert result.returncode == 0, result.stderr
        assert "scheduled_actions" not in inspect(engine).get_table_names()
        with engine.connect() as db:
            assert db.scalar(text("SELECT state FROM commitments")) == "waiting"
    engine.dispose()
