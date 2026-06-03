"""Alembic environment for aileadz (Futurematch).

Design goals (see migrations/README.md):

* STANDALONE: this module must NOT import the Flask app (run.py / create_app)
  or any application package. Alembic has to keep working even if the app's
  runtime dependencies are broken or unavailable, so we only depend on the
  standard library + Alembic + SQLAlchemy here.

* DB URL FROM ENV: the database URL is built from the same MYSQL_* environment
  variables the app uses in run.py:
      MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB
  Defaults mirror run.py's production fallbacks so behaviour is consistent.
  An explicit ALEMBIC_DATABASE_URL / DATABASE_URL env var, if set, wins over
  everything (handy for tunnels / CI / a fully-formed SQLAlchemy URL).

* NO MODELS: the app has no SQLAlchemy ORM models — the schema is managed by
  hand-written SQL (today via run.py's before_request bootstrap, going forward
  via the migration files in versions/). So ``target_metadata`` is None and
  autogenerate is not used; migrations are authored explicitly.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Alembic Config object — provides access to values in alembic.ini.
config = context.config

# Configure Python logging from the alembic.ini stanza, if present.
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        # Logging config is best-effort; never let it block a migration.
        pass


def _quote(value: str) -> str:
    """Percent-encode a single URL component (user / password / db name).

    The production DB name contains a ``$`` (``TobiasMastek$AiLead``) and
    passwords can contain reserved characters, so encode each piece safely.
    """
    from urllib.parse import quote_plus

    return quote_plus(str(value))


def get_database_url() -> str:
    """Build the SQLAlchemy URL from env, matching run.py's config.

    Precedence:
      1. ALEMBIC_DATABASE_URL / DATABASE_URL  (full SQLAlchemy URL, used as-is)
      2. MYSQL_* environment variables        (assembled into a pymysql URL)

    The non-secret MYSQL_* defaults mirror run.py for convenience, but the
    PASSWORD is intentionally NOT defaulted here — a migration/ops tool must
    never embed the live credential in source (that is the very leak being
    rotated; see docs/runbooks/SECRET_ROTATION.md). Set MYSQL_PASSWORD (or
    ALEMBIC_DATABASE_URL) in the ops/CI environment before running Alembic.
    """
    explicit = os.environ.get("ALEMBIC_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if explicit:
        return explicit

    host = os.environ.get("MYSQL_HOST", "TobiasMastek.mysql.pythonanywhere-services.com")
    port = os.environ.get("MYSQL_PORT", "3306")
    user = os.environ.get("MYSQL_USER", "TobiasMastek")
    password = os.environ.get("MYSQL_PASSWORD")
    if not password:
        raise RuntimeError(
            "MYSQL_PASSWORD (or ALEMBIC_DATABASE_URL) must be set to run Alembic — "
            "the DB password is never hardcoded in this tool. See "
            "docs/runbooks/SECRET_ROTATION.md."
        )
    database = os.environ.get("MYSQL_DB", "TobiasMastek$AiLead")
    charset = os.environ.get("MYSQL_CHARSET", "utf8mb4")

    return (
        "mysql+pymysql://"
        f"{_quote(user)}:{_quote(password)}@{host}:{port}/{_quote(database)}"
        f"?charset={charset}"
    )


# The app has no SQLAlchemy models; this is a baseline/stamp setup for forward
# migrations authored by hand. Leave as None (disables autogenerate).
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout, no DB connection).

    Useful for generating a SQL script to hand to a DBA:
        alembic upgrade head --sql
    """
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (against a live DB connection)."""
    # Inject the env-derived URL into the config dict Alembic hands to
    # engine_from_config, so alembic.ini's placeholder url is never used.
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_database_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
