"""baseline — the schema already exists (no-op).

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-03

PURPOSE
-------
This is the BASELINE revision for the aileadz / Futurematch database. The live
production schema was NOT created by Alembic — it is bootstrapped at runtime by
the homegrown "reflect-and-ALTER" logic in run.py's before_request hooks
(CREATE TABLE IF NOT EXISTS + _auto_sync_columns in enterprise_tables.py,
branding_service.py, etc.). Alembic is being added ALONGSIDE that, as the
forward-migration runner — it does not replace the bootstrap.

Because the tables already exist on every live database, this revision is a
deliberate NO-OP. Its only job is to be a fixed starting point in Alembic's
revision graph so that:

  1. We can ``alembic stamp 0001_baseline`` the existing prod DB ONCE, recording
     in the alembic_version table that the database is "at" this baseline —
     WITHOUT trying to (re)create anything. See migrations/README.md.

  2. All future, real schema changes are authored as new revisions whose
     ``down_revision`` chains back to this one (down_revision = "0001_baseline").

SAFETY
------
upgrade() and downgrade() are both no-ops, so this revision is safe to run
(``alembic upgrade``) against a live, already-populated database — it will not
create, alter, or drop anything. In normal operation you should STAMP this
revision rather than upgrade to it; the no-op upgrade is belt-and-suspenders so
that an accidental ``alembic upgrade head`` against a populated DB cannot cause
harm.
"""
from __future__ import annotations

from typing import Sequence, Union

# Imported for symmetry / so authors copying this file have them at hand. The
# baseline itself does not use op / sa because it is intentionally a no-op.
from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op.

    The schema already exists (created by run.py's before_request bootstrap),
    so there is nothing to apply. This is intentionally empty so that running
    it against the live DB is a guaranteed-safe operation. Real forward
    migrations are authored as separate revisions after this baseline.
    """
    # Intentionally empty — see module docstring.
    pass


def downgrade() -> None:
    """No-op.

    You cannot meaningfully "un-baseline" a database whose schema predates
    Alembic, so downgrade does nothing rather than dropping live tables.
    """
    # Intentionally empty — never drop the pre-existing live schema.
    pass
