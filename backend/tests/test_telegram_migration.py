from datetime import datetime, timezone

from sqlalchemy import MetaData, create_engine, inspect, text
from test_migrations import run_alembic


def test_telegram_upgrade_and_downgrade_preserve_existing_records(tmp_path):
    url = f"sqlite:///{tmp_path}/telegram.db"
    assert run_alembic(url, "upgrade", "0012").returncode == 0
    engine = create_engine(url)
    metadata = MetaData()
    metadata.reflect(engine)
    with engine.begin() as db:
        db.execute(
            metadata.tables["users"]
            .insert()
            .values(
                id="owner",
                username="owner",
                pet_name="Pip",
                temperament="curious",
                created_at=datetime.now(timezone.utc),
            )
        )
    result = run_alembic(url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    assert {
        "telegram_bots",
        "telegram_connections",
        "telegram_inputs",
        "telegram_deliveries",
    } <= set(inspect(engine).get_table_names())
    with engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM telegram_connections")) == 0
        assert db.execute(text("SELECT pet_name,temperament FROM users")).one() == (
            "Pip",
            "curious",
        )
    result = run_alembic(url, "downgrade", "0012")
    assert result.returncode == 0, result.stderr
    assert "telegram_connections" not in inspect(engine).get_table_names()
    with engine.connect() as db:
        assert db.execute(text("SELECT pet_name,temperament FROM users")).one() == (
            "Pip",
            "curious",
        )
    engine.dispose()
