# Database migrations (Alembic)

This directory holds the **Alembic** migration runner for the aileadz /
Futurematch database. Alembic is the *forward* migration tool going forward.

> **Important context.** Today the live schema is still bootstrapped at runtime
> by the homegrown "reflect-and-ALTER" logic in `run.py`'s `before_request`
> hooks (`CREATE TABLE IF NOT EXISTS` + `_auto_sync_columns`, in
> `enterprise_tables.py`, `branding_service.py`, etc.). **Alembic is added
> ALONGSIDE that bootstrap, not as a replacement.** The legacy bootstrap stays
> in place until migrations have fully taken over the schema lifecycle. Until
> then, every change you make via Alembic must remain compatible with the
> idempotent `CREATE TABLE IF NOT EXISTS` bootstrap.

> **Alembic is optional at runtime.** The Flask app (`create_app()` in
> `run.py`) does **not** import Alembic and never reads `alembic.ini` or this
> `migrations/` package. So a missing `alembic` install cannot affect the
> running app — these tools are for operators and CI only.

---

## (a) Install Alembic

Alembic is intentionally **not** in `requirements.txt` (it's not needed by the
running app). Install it in your ops/CI environment when you want to run
migrations:

```bash
pip install "alembic>=1.13,<2"
```

`SQLAlchemy` (pulled in by Alembic) and `PyMySQL` (already a runtime dep) are
all that's needed for the MySQL connection.

---

## (b) Point Alembic at the database via env

`migrations/env.py` builds the SQLAlchemy URL from the **same `MYSQL_*`
environment variables the app uses** in `run.py`:

| Env var          | Default (mirrors `run.py`)                                |
|------------------|-----------------------------------------------------------|
| `MYSQL_HOST`     | `TobiasMastek.mysql.pythonanywhere-services.com`          |
| `MYSQL_PORT`     | `3306`                                                    |
| `MYSQL_USER`     | `TobiasMastek`                                            |
| `MYSQL_PASSWORD` | *(prod fallback)*                                         |
| `MYSQL_DB`       | `TobiasMastek$AiLead`                                     |
| `MYSQL_CHARSET`  | `utf8mb4`                                                 |

Set the ones that differ from the defaults, e.g. when going through the local
SSH tunnel (see `main()` in `run.py`):

```bash
export MYSQL_HOST=127.0.0.1
export MYSQL_PORT=<tunnel-local-port>
export MYSQL_USER=TobiasMastek
export MYSQL_PASSWORD='...'
export MYSQL_DB='TobiasMastek$AiLead'
```

Alternatively, provide a fully-formed SQLAlchemy URL which **overrides** the
`MYSQL_*` assembly entirely:

```bash
export ALEMBIC_DATABASE_URL='mysql+pymysql://user:pass@127.0.0.1:3306/TobiasMastek%24AiLead?charset=utf8mb4'
```

(`ALEMBIC_DATABASE_URL` or `DATABASE_URL` both work; remember to URL-encode the
`$` in the DB name as `%24`.) The `sqlalchemy.url` in `alembic.ini` is only a
placeholder — `env.py` always overrides it from the environment.

Sanity check that you're pointed at the right DB:

```bash
alembic current      # connects; prints the DB's current revision (empty before baseline)
```

---

## (c) ⚠️ Baseline the existing prod DB — ONCE

The production schema already exists (it predates Alembic). So we must tell
Alembic "this database is already at revision `0001_baseline`" **without** trying
to create anything. That is a **STAMP**, not an upgrade:

```bash
# Run this EXACTLY ONCE against each pre-existing database (prod, staging, ...).
alembic stamp 0001_baseline
```

This simply writes `0001_baseline` into the `alembic_version` table (creating
that table if needed). It does **not** touch any application tables.

- `0001_baseline` is a deliberate **no-op** revision (see
  `versions/0001_baseline.py`): both `upgrade()` and `downgrade()` do nothing.
  So even if someone runs `alembic upgrade head` against a populated DB before
  stamping, no tables are created, altered, or dropped — it's safe either way.
- For a brand-new, empty database you'd typically still `stamp 0001_baseline`
  and rely on the app's `CREATE TABLE IF NOT EXISTS` bootstrap to build the
  current schema, then apply forward migrations on top.

After stamping:

```bash
alembic current      # -> 0001_baseline (head)
```

---

## (d) Author and run forward migrations

From now on, every schema change is a new revision chained onto the baseline.

Create a revision (hand-authored — there are no SQLAlchemy models, so
`--autogenerate` is **not** used):

```bash
alembic revision -m "add foo column to companies"
```

This generates a new file under `versions/` from `script.py.mako`. Its
`down_revision` will automatically point at the current head (initially
`0001_baseline`). Fill in `upgrade()` / `downgrade()` with explicit SQL via
`op.execute(...)` or Alembic's `op.*` helpers, e.g.:

```python
def upgrade() -> None:
    op.execute(
        "ALTER TABLE companies "
        "ADD COLUMN IF NOT EXISTS foo VARCHAR(255) NULL"
    )

def downgrade() -> None:
    op.execute("ALTER TABLE companies DROP COLUMN IF EXISTS foo")
```

> **Keep migrations idempotent / bootstrap-compatible** while the legacy
> `before_request` bootstrap is still live: prefer `ADD COLUMN IF NOT EXISTS`,
> `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, etc., so a column
> the bootstrap may also add can't cause a hard failure during the transition.

Apply migrations:

```bash
alembic upgrade head          # apply everything up to the latest revision
alembic downgrade -1          # roll back the most recent revision
alembic history --verbose     # see the revision chain
alembic upgrade head --sql    # offline: print SQL instead of executing it
```

---

## Transition plan / ownership of the schema

1. **Now:** legacy `before_request` bootstrap owns the schema; Alembic is
   present, prod is **stamped** at `0001_baseline`, and new changes go in as
   forward migrations (written defensively so they coexist with the bootstrap).
2. **Later:** once all schema mutations live in Alembic revisions and have been
   deployed everywhere, the homegrown `CREATE TABLE IF NOT EXISTS` /
   `_auto_sync_columns` bootstrap in `run.py` & friends can be retired. **Do not
   remove the bootstrap before then** — it's the current source of truth.
