# Database migrations (Alembic)

Alembic is the *forward* migration tool, added ALONGSIDE the runtime schema bootstrap, not
replacing it. For operators and CI only: `create_app()` never imports Alembic or reads
`alembic.ini` / this package, so a missing install cannot affect the running app.

## Who owns the schema today

The runtime bootstrap does: `before_request` hooks in `run.py` call
`enterprise_tables.ensure_enterprise_tables` (single table definitions; `schema_registry.py` holds
`expected_tables()` / `verify_schema()` and the one-time data fixes flagged in `schema_meta`),
`branding_service.ensure_branding_schema`, `performance_indexes.ensure_performance_indexes`, and
the self-ensuring modules (`scheduler.py`, `two_factor.py`, ...). New tables/columns normally go
there. A revision here is for changes the bootstrap cannot do, or to record them for DBs managed
with `alembic upgrade`. Every revision must tolerate a database the bootstrap already touched.

Revision chain (checked by `tests/test_schema_baseline.py`):
`0001_baseline` (no-op) -> `0002_performance_indexes` (same set as `performance_indexes.PERFORMANCE_INDEXES`;
edit both) -> `b001_part_b_lifecycle` (head: order-lifecycle / billing columns, `order_status_history`,
`company_team_order_policy`, `schema_meta`, unified `notifications` columns, one-way legacy order-status
mapping flagged `order_lifecycle_v1`; its `downgrade()` keeps the additive objects).

## Install

Alembic is not in `requirements.txt` (the app does not need it). In an ops/CI environment:

```bash
pip install "alembic>=1.13,<2"      # pulls SQLAlchemy; PyMySQL is already a runtime dependency
```

## Point Alembic at the database

`migrations/env.py` is standalone (stdlib + Alembic + SQLAlchemy; never imports the app) and builds
the URL as follows:

1. `ALEMBIC_DATABASE_URL`, else `DATABASE_URL`: used as-is. It must be a SQLAlchemy URL with the
   PyMySQL driver, e.g. `mysql+pymysql://user:pass@host:3306/db?charset=utf8mb4`. A bare
   `mysql://...` (the form ServerHoster injects as `DATABASE_URL`) selects the `mysqldb` driver,
   which is not installed: rewrite the scheme or use the next option. URL-encode special
   characters in the password and database name.
2. Else the same `MYSQL_*` variables as the app: `MYSQL_HOST` (default `localhost`), `MYSQL_PORT`
   (`3306`), `MYSQL_USER` (`root`), `MYSQL_DB` (`aileadz`), `MYSQL_CHARSET` (`utf8mb4`), and
   `MYSQL_PASSWORD`, which has NO default: Alembic aborts with a RuntimeError when it is unset.

`sqlalchemy.url` in `alembic.ini` is only a placeholder; `env.py` always overrides it. Check the
target first:

```bash
alembic current        # connects; prints the DB's current revision (empty if never stamped)
```

## Adopting Alembic on an existing database (once per database)

The schema already exists, so tell Alembic where the database is instead of creating anything.
The recommended path:

```bash
python scripts/dump_schema_baseline.py > schema_baseline.sql   # read-only SHOW CREATE TABLE + drift report
alembic stamp b001_part_b_lifecycle                            # after reviewing the drift report
alembic current                                                # -> b001_part_b_lifecycle (head)
```

`scripts/dump_schema_baseline.py` needs `MYSQL_USER` and `MYSQL_DB` (plus `MYSQL_HOST`/`MYSQL_PORT`/
`MYSQL_PASSWORD`) and compares production against `schema_registry.expected_tables()`. Stamping
`0001_baseline` and then `alembic upgrade head` is also safe: `0002` and `b001` check
`INFORMATION_SCHEMA` and skip what exists. Whether a given production DB has been stamped is not
recorded in this repo: run `alembic current` against it.

## Author and run revisions

There are no SQLAlchemy models, so `--autogenerate` is not used; revisions are hand-written from
`script.py.mako`:

```bash
alembic revision -m "add foo column to companies"   # down_revision = current head
alembic upgrade head          # apply
alembic downgrade -1          # roll back one revision
alembic history --verbose
alembic upgrade head --sql    # offline: print SQL instead of executing it
```

Rules for a revision:
- Keep it standalone (no app imports; `env.py` must keep working when the app's dependencies
  are broken) and idempotent. MySQL has no `ADD COLUMN IF NOT EXISTS` / `CREATE INDEX IF NOT
  EXISTS`: check `INFORMATION_SCHEMA` first, as `b001_order_lifecycle_and_notifications.py`
  (`_has_table`, `_has_column`) and `0002_performance_indexes.py` do; `CREATE TABLE IF NOT EXISTS`
  is fine.
- If the change is also in the runtime definitions, keep both in sync (the tests in
  `tests/test_schema_baseline.py` verify a fresh DB bootstraps to the expected tables when a
  MySQL service is reachable, as in CI).
- Two revisions branching off the same parent: `alembic merge heads`.

## Retiring the bootstrap

Only once every schema change lives in revisions and all databases are stamped may the
`CREATE TABLE IF NOT EXISTS` / column-sync bootstrap be retired. Do not remove it before then:
it is the current source of truth and the CI fresh-DB test depends on it.
