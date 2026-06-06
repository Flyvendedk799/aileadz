"""
Idempotent performance indexes for the aileadz / Futurematch database.

Why this module exists
----------------------
The dashboards (admin, HR, chatbot/AI reports) filter and group on a handful of
hot columns — ``created_at``, ``company_id``, ``status``, ``username``,
``query_text`` — that the runtime-bootstrapped schema never indexed. On MySQL
those queries fall back to full-table scans, which is the dominant cost of the
slow dashboard/report pages.

``ensure_performance_indexes(app)`` adds those indexes. It mirrors the idempotent
"reflect-and-ALTER" style already used by ``enterprise_tables.ensure_enterprise_tables``:

  * It is **safe to run repeatedly** — each ``ALTER TABLE … ADD INDEX`` ignores
    MySQL error 1061 (duplicate key name), so a re-run on an already-indexed DB
    is a no-op.
  * It is **safe on a partial schema** — before touching a table it checks that
    the table and every column the index needs actually exist (via
    ``INFORMATION_SCHEMA``). A missing table/column is skipped silently rather
    than raising, so this can never crash ``create_app()`` / the first request.
  * It never raises out of ``ensure_performance_indexes`` — the whole body is
    guarded, matching the other startup migrations.

Deployment: it is wired into a ``before_request`` hook in ``run.py`` that runs
once per worker process (NOT gated by the 6-hour enterprise-sync TTL stamp), so
the indexes are applied on the next ``git pull`` + web-app reload without any
manual MySQL console step. The same DDL is recorded as the Alembic revision
``migrations/versions/0002_performance_indexes.py`` for the migration history.

The canonical index list lives in :data:`PERFORMANCE_INDEXES` so the Alembic
revision and this runtime ensurer stay in sync.
"""
import logging

# (table, index_name, columns_sql, required_columns)
#   columns_sql  -> goes verbatim inside ADD INDEX <name> ( ... ); already
#                   backticked where a column name is a reserved word (`read`).
#                   Prefix lengths (e.g. query_text(191)) are for TEXT/long
#                   VARCHAR columns that cannot be indexed in full.
#   required_columns -> every column that must exist for the index to be valid;
#                   if any is missing the index is skipped.
PERFORMANCE_INDEXES = [
    # ── chatbot_interactions: the biggest table; every report scans it by date,
    #    user, or query text ──────────────────────────────────────────────────
    ("chatbot_interactions", "idx_ci_created",
     "created_at", ["created_at"]),
    ("chatbot_interactions", "idx_ci_user_created",
     "username, created_at", ["username", "created_at"]),
    ("chatbot_interactions", "idx_ci_query",
     "query_text(191)", ["query_text"]),
    ("chatbot_interactions", "idx_ci_company_created",
     "company_id, created_at", ["company_id", "created_at"]),
    ("chatbot_interactions", "idx_ci_session",
     "session_id", ["session_id"]),

    # ── course_orders: revenue/order aggregations per company + recency ───────
    ("course_orders", "idx_co_company_created",
     "company_id, created_at", ["company_id", "created_at"]),
    ("course_orders", "idx_co_company_status",
     "company_id, status", ["company_id", "status"]),
    ("course_orders", "idx_co_status",
     "status", ["status"]),
    ("course_orders", "idx_co_user_company",
     "user_id, company_id", ["user_id", "company_id"]),

    # ── company_users: HR metrics + the admin/user joins ──────────────────────
    ("company_users", "idx_cu_company_status",
     "company_id, status", ["company_id", "status"]),
    ("company_users", "idx_cu_company_status_login",
     "company_id, status, last_login", ["company_id", "status", "last_login"]),
    ("company_users", "idx_cu_user_company",
     "user_id, company_id", ["user_id", "company_id"]),

    # ── employee_learning_progress: joined per company in the HR dashboard ─────
    ("employee_learning_progress", "idx_elp_user_company",
     "user_id, company_id", ["user_id", "company_id"]),

    # ── ai_agent_runs: AI-cost dashboard (latency / cost over a date window) ───
    ("ai_agent_runs", "idx_aar_created",
     "created_at", ["created_at"]),

    # ── users: admin filters by role, orders by created_at ────────────────────
    ("users", "idx_users_role",
     "role", ["role"]),
    ("users", "idx_users_created",
     "created_at", ["created_at"]),

    # ── notifications: unread-per-user lookups (`read` is a reserved word) ─────
    ("notifications", "idx_notif_user_read",
     "user_id, `read`", ["user_id", "read"]),

    # ── per-user reports (reports.py / pages.py analytics) ────────────────────
    ("credit_usage", "idx_credit_usage_user",
     "username, `timestamp`", ["username", "timestamp"]),
    ("app_usage", "idx_app_usage_user",
     "username", ["username"]),
    ("social_metrics", "idx_social_metrics_user",
     "username", ["username"]),
]


def _table_exists(cur, table):
    try:
        cur.execute(
            "SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s LIMIT 1",
            (table,),
        )
        return cur.fetchone() is not None
    except Exception as e:
        logging.debug("perf-index: table check failed for %s: %s", table, e)
        return False


def _existing_columns(cur, table):
    try:
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (table,),
        )
        # DictCursor is the connection default, but this module must work with a
        # plain cursor too — handle both tuple and dict rows.
        cols = set()
        for row in cur.fetchall():
            if isinstance(row, dict):
                cols.add(str(next(iter(row.values()))).lower())
            else:
                cols.add(str(row[0]).lower())
        return cols
    except Exception as e:
        logging.debug("perf-index: column check failed for %s: %s", table, e)
        return set()


def ensure_performance_indexes(app):
    """Add the hot-path indexes in :data:`PERFORMANCE_INDEXES` (idempotent).

    Safe to run repeatedly and on a partial schema. Never raises.
    Returns the number of indexes actually created (0 when all already exist).
    """
    created = 0
    try:
        with app.app_context():
            conn = app.mysql.connection
            inspect_cur = conn.cursor()

            # Cache table existence + columns so we issue one metadata query per
            # table rather than one per index.
            columns_by_table = {}
            for table, _name, _cols_sql, _required in PERFORMANCE_INDEXES:
                if table in columns_by_table:
                    continue
                if not _table_exists(inspect_cur, table):
                    columns_by_table[table] = None  # table missing -> skip all
                    continue
                columns_by_table[table] = _existing_columns(inspect_cur, table)
            try:
                inspect_cur.close()
            except Exception:
                pass

            for table, name, cols_sql, required in PERFORMANCE_INDEXES:
                cols = columns_by_table.get(table)
                if not cols:  # None (missing table) or empty set (no columns read)
                    continue
                if not all(c.lower() in cols for c in required):
                    logging.debug(
                        "perf-index: skip %s.%s — missing column(s) %s",
                        table, name, [c for c in required if c.lower() not in cols],
                    )
                    continue
                try:
                    add_cur = conn.cursor()
                    add_cur.execute(
                        "ALTER TABLE `{table}` ADD INDEX `{name}` ({cols})".format(
                            table=table, name=name, cols=cols_sql
                        )
                    )
                    conn.commit()
                    add_cur.close()
                    created += 1
                    logging.info("perf-index: created %s on %s (%s)", name, table, cols_sql)
                except Exception as e:
                    # 1061 = duplicate key name (index already exists) — expected
                    # on every run after the first; anything else is logged but
                    # not fatal.
                    if hasattr(e, "args") and e.args and e.args[0] == 1061:
                        continue
                    logging.warning(
                        "perf-index: could not add %s on %s: %s", name, table, e
                    )
                    try:
                        conn.rollback()
                    except Exception:
                        pass
            if created:
                logging.info("perf-index: %d new index(es) created", created)
            else:
                logging.info("perf-index: all hot-path indexes already present")
    except Exception as e:
        logging.warning("perf-index: ensure_performance_indexes skipped: %s", e)
    return created
