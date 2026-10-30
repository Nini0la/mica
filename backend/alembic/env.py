import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

from alembic import context
from app.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

# The checked-in URL is the local Compose default. Deployments select their
# migration target through DATABASE_URL. Alembic uses a synchronous engine, so
# translate common deployment/async forms to the installed sync drivers.
database_url = os.getenv("DATABASE_URL")
if database_url:
    url = make_url(database_url)
    sync_drivers = {
        "postgres": "postgresql+psycopg",
        "postgresql": "postgresql+psycopg",
        "postgresql+asyncpg": "postgresql+psycopg",
        "sqlite+aiosqlite": "sqlite",
    }
    url = url.set(drivername=sync_drivers.get(url.drivername, url.drivername))
    # Preserve credentials while escaping ConfigParser interpolation. Do not
    # log this value: deployment URLs may contain database secrets.
    rendered_url = url.render_as_string(hide_password=False).replace("%", "%%")
    config.set_main_option("sqlalchemy.url", rendered_url)

target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_offline() if context.is_offline_mode() else run_migrations_online()
