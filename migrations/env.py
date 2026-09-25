"""
Alembic environment configuration.

Reads DATABASE_URL from the .env file (same source of truth as the application)
and runs migrations against it. This file is called by `alembic upgrade head`.
"""

import os
import sys
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

# Add the project root to sys.path so we can import our models.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

load_dotenv()

from database import Base
from models import Finding, PullRequest, Repository, Review, ReviewConsensus  # noqa: F401

# Alembic Config object.
config = context.config

# Override sqlalchemy.url from environment if available.
# set_main_option applies ConfigParser %-interpolation, which mangles
# URL-encoded passwords (e.g. p%40ss → crash). Escaping % as %% avoids this.
database_url = os.getenv("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

# Set up Python logging from the alembic.ini [loggers] section.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The MetaData object for autogenerate support.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connect to the database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
