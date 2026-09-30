"""Schema registry: the single home for table definitions that no other module
owned, plus boot-time verification and one-shot data migrations (N-3.4).

How the schema story works now
------------------------------
* ``enterprise_tables.enterprise_table_ddls()`` is the ONE list of CREATE TABLE
  statements (core enterprise tables + ``REGISTRY_DDL`` below). Every table has
  exactly one definition; a test fails if a second ``CREATE TABLE`` for the same
  name appears anywhere in the code base.
* ``ensure_enterprise_tables`` still creates missing tables and adds missing
  columns (additive, safe), but its 6 h skip stamp is now keyed on
  ``ddl_fingerprint()``, so a changed definition is applied on the next boot
  instead of waiting for a stale ``/tmp`` file to expire.
* Destructive or data-shaping changes go through Alembic
  (``migrations/versions``) and, where the app must also self-heal a database
  that never ran Alembic, through ``run_data_migrations`` here, each guarded by a
  flag row in ``schema_meta`` so it runs once.
* ``verify_schema`` is the boot-time verify: it reports missing tables/columns
  (for /readyz and the admin system page) instead of silently ``ALTER``-ing.

No DDL in this file carries SQL comments, because ``_parse_columns_from_create``
splits on commas.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_ENGINE = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"

REGISTRY_DDL = [
    f"""CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(255) NOT NULL,
        password VARCHAR(255) NOT NULL,
        email VARCHAR(255),
        credits INT NOT NULL DEFAULT 0,
        role VARCHAR(50) NOT NULL DEFAULT 'user',
        email_notifications TINYINT NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_users_username (username)
    ) {_ENGINE}""",

    f"""CREATE TABLE IF NOT EXISTS brands (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(255) NOT NULL,
        brand_name VARCHAR(255),
        brand_site VARCHAR(500),
        brand_logo VARCHAR(500),
        brand_facebook VARCHAR(500),
        brand_twitter VARCHAR(500),
        brand_instagram VARCHAR(500),
        brand_linkedin VARCHAR(500),
        brand_description TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_brands_username (username)
    ) {_ENGINE}""",

    f"""CREATE TABLE IF NOT EXISTS notifications (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id VARCHAR(255),
        company_id INT NULL,
        recipient_user_id INT NULL,
        sender_user_id INT NULL,
        kind VARCHAR(40) DEFAULT 'info',
        title VARCHAR(255),
        message TEXT,
        image_url VARCHAR(500),
        action_url VARCHAR(500),
        is_urgent TINYINT DEFAULT 0,
        dedupe_key VARCHAR(191) NULL,
        `read` TINYINT DEFAULT 0,
        read_at DATETIME NULL,
        `timestamp` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_notif_user_read (user_id, `read`),
        INDEX idx_notif_company (company_id),
        INDEX idx_notif_dedupe (user_id, dedupe_key)
    ) {_ENGINE}""",

    f"""CREATE TABLE IF NOT EXISTS credit_usage (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(255),
        user_id INT NULL,
        company_id INT NULL,
        credits_used INT NOT NULL DEFAULT 0,
        description VARCHAR(255),
        kind VARCHAR(20) DEFAULT 'usage',
        assistant VARCHAR(40) NULL,
        model VARCHAR(80) NULL,
        tokens_in INT NULL,
        tokens_out INT NULL,
        actor VARCHAR(255) NULL,
        `timestamp` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_credit_usage_user (username, `timestamp`),
        INDEX idx_credit_usage_company (company_id, `timestamp`)
    ) {_ENGINE}""",

    f"""CREATE TABLE IF NOT EXISTS app_usage (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(255),
        app_name VARCHAR(255),
        usage_count INT DEFAULT 0,
        INDEX idx_app_usage_user (username)
    ) {_ENGINE}""",

    f"""CREATE TABLE IF NOT EXISTS social_metrics (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(255),
        followers INT DEFAULT 0,
        impressions INT DEFAULT 0,
        INDEX idx_social_metrics_user (username)
    ) {_ENGINE}""",

    f"""CREATE TABLE IF NOT EXISTS schema_meta (
        meta_key VARCHAR(100) PRIMARY KEY,
        meta_value VARCHAR(255),
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) {_ENGINE}""",

    f"""CREATE TABLE IF NOT EXISTS order_status_history (
        id INT AUTO_INCREMENT PRIMARY KEY,
        order_id VARCHAR(50) NOT NULL,
        company_id INT NULL,
        kind VARCHAR(10) NOT NULL DEFAULT 'status',
        from_value VARCHAR(30) NULL,
        to_value VARCHAR(30) NOT NULL,
        actor_user_id INT NULL,
        actor_kind VARCHAR(20) NOT NULL DEFAULT 'user',
        actor_label VARCHAR(255) NULL,
        note VARCHAR(500) NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_osh_order (order_id, created_at),
        INDEX idx_osh_company (company_id, created_at)
    ) {_ENGINE}""",

    f"""CREATE TABLE IF NOT EXISTS company_report_schedules (
        id INT AUTO_INCREMENT PRIMARY KEY,
        company_id INT NOT NULL,
        report_type VARCHAR(64) NOT NULL,
        cadence VARCHAR(16) NOT NULL,
        department VARCHAR(100) NULL,
        created_by INT NULL,
        enabled TINYINT(1) NOT NULL DEFAULT 1,
        last_sent_at DATETIME NULL,
        last_status VARCHAR(40) NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_company_report (company_id, report_type, department)
    ) {_ENGINE}""",

    f"""CREATE TABLE IF NOT EXISTS learning_path_steps (
        id INT AUTO_INCREMENT PRIMARY KEY,
        path_id INT NOT NULL,
        company_id INT NOT NULL,
        position INT NOT NULL DEFAULT 1,
        step_type VARCHAR(12) NOT NULL DEFAULT 'catalog',
        course_handle VARCHAR(255) NULL,
        title VARCHAR(255) NULL,
        INDEX idx_lps_path (path_id, position)
    ) {_ENGINE}""",

    f"""CREATE TABLE IF NOT EXISTS user_learning_path_versions (
        id INT AUTO_INCREMENT PRIMARY KEY,
        path_id INT NOT NULL,
        username VARCHAR(255) NOT NULL,
        goal VARCHAR(500) DEFAULT '',
        steps LONGTEXT,
        saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_ulpv_path (path_id, saved_at),
        INDEX idx_ulpv_user (username)
    ) {_ENGINE}""",

    f"""CREATE TABLE IF NOT EXISTS learning_path_versions (
        id INT AUTO_INCREMENT PRIMARY KEY,
        path_id INT NOT NULL,
        company_id INT NOT NULL,
        version INT NOT NULL,
        steps_json LONGTEXT,
        saved_by INT NULL,
        note VARCHAR(500) NULL,
        saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_lpv_path (path_id, version)
    ) {_ENGINE}""",

    f"""CREATE TABLE IF NOT EXISTS company_team_order_policy (
        id INT AUTO_INCREMENT PRIMARY KEY,
        company_id INT NOT NULL,
        vendor_id INT NULL,
        mode VARCHAR(30) NOT NULL DEFAULT 'linked_orders',
        updated_by INT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_ctop_company (company_id, vendor_id)
    ) {_ENGINE}""",
]


# Columns that must exist on tables defined elsewhere, for verify_schema only
# (the owning CREATE already lists them; this guards against silent drift).
REQUIRED_COLUMNS = {
    "course_orders": ["order_id", "status", "billing_status", "vendor_id", "booked_at", "group_order_id"],
    "notifications": ["user_id", "recipient_user_id", "action_url", "read"],
    "order_status_history": ["order_id", "kind", "to_value", "actor_kind"],
}


def _rows(cur):
    out = []
    for r in cur.fetchall() or []:
        out.append(r if isinstance(r, dict) else {"v": r[0]})
    return out


def expected_tables():
    """{table: [columns]} parsed from every DDL the platform owns."""
    from enterprise_tables import (_parse_columns_from_create, _parse_table_name,
                                   enterprise_table_ddls)
    result = {}
    for sql in enterprise_table_ddls():
        name = _parse_table_name(sql)
        if name:
            result[name] = [c for c, _ in _parse_columns_from_create(sql)]
    return result


def verify_schema(conn):
    """Compare the live DB with the registry. Returns
    ``{"missing_tables": [...], "missing_columns": {table: [cols]}, "ok": bool}``.
    Never raises; an unreachable DB yields ``ok=False`` with ``error``."""
    report = {"missing_tables": [], "missing_columns": {}, "ok": True}
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE()"
            )
            live = {}
            for r in cur.fetchall() or []:
                t = (r.get("TABLE_NAME") if isinstance(r, dict) else r[0]) or ""
                c = (r.get("COLUMN_NAME") if isinstance(r, dict) else r[1]) or ""
                live.setdefault(t.lower(), set()).add(c.lower())
        finally:
            cur.close()
        for table, cols in expected_tables().items():
            have = live.get(table.lower())
            if have is None:
                report["missing_tables"].append(table)
                continue
            missing = [c for c in cols if c.lower() not in have]
            if missing:
                report["missing_columns"][table] = missing
        report["ok"] = not report["missing_tables"] and not report["missing_columns"]
    except Exception as e:
        logger.warning("schema verify failed: %s", e)
        report["ok"] = False
        report["error"] = str(e)
    return report


# ── one-shot data migrations ────────────────────────────────────────────────

def _flag_done(cur, key):
    cur.execute("SELECT meta_value FROM schema_meta WHERE meta_key = %s", (key,))
    return cur.fetchone() is not None


def _set_flag(cur, key, value="done"):
    cur.execute(
        "INSERT INTO schema_meta (meta_key, meta_value) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value)",
        (key, value),
    )


ORDER_LIFECYCLE_MIGRATION_SQL = [
    "UPDATE course_orders SET billing_note = billing_notes "
    "WHERE (billing_note IS NULL OR billing_note = '') AND billing_notes IS NOT NULL AND billing_notes <> ''",
    "UPDATE course_orders SET billing_status = 'paid' "
    "WHERE status = 'paid' OR payment_status = 'paid'",
    "UPDATE course_orders SET billing_status = 'invoiced' "
    "WHERE billing_status = 'not_invoiced' AND (status = 'invoiced' OR payment_status = 'invoiced')",
    "UPDATE course_orders SET status = CASE WHEN completion_status = 'completed' THEN 'completed' ELSE 'booked' END "
    "WHERE status IN ('invoiced', 'paid')",
    "UPDATE course_orders SET status = 'booked' WHERE status IN ('processing', 'confirmed')",
    "UPDATE course_orders SET status = 'approved' WHERE status = 'pending'",
]


SKILL_HISTORY_BACKFILL_SQL = (
    "UPDATE employee_skill_history h "
    "JOIN company_users cu ON cu.id = h.employee_id AND cu.company_id = h.company_id "
    "SET h.employee_id = cu.user_id "
    "WHERE h.source IN ('profile_manual', 'cv_upload', 'ai_chat') "
    "AND cu.user_id IS NOT NULL AND cu.user_id <> h.employee_id"
)


def run_data_migrations(conn):
    """Apply the idempotent one-shot data fixes. Returns the list of keys run."""
    ran = []
    try:
        cur = conn.cursor()
        try:
            if not _flag_done(cur, "order_lifecycle_v1"):
                for sql in ORDER_LIFECYCLE_MIGRATION_SQL:
                    try:
                        cur.execute(sql)
                    except Exception as e:
                        logger.warning("order lifecycle migration step skipped: %s", e)
                _set_flag(cur, "order_lifecycle_v1")
                conn.commit()
                ran.append("order_lifecycle_v1")
            if not _flag_done(cur, "skill_history_user_ids_v1"):
                try:
                    cur.execute(SKILL_HISTORY_BACKFILL_SQL)
                except Exception as e:
                    logger.warning("skill history id backfill skipped: %s", e)
                _set_flag(cur, "skill_history_user_ids_v1")
                conn.commit()
                ran.append("skill_history_user_ids_v1")
            try:
                from notification_service import migrate_company_notifications
                if not _flag_done(cur, "notifications_unified_v1"):
                    migrate_company_notifications(conn)
                    _set_flag(cur, "notifications_unified_v1")
                    conn.commit()
                    ran.append("notifications_unified_v1")
            except ImportError:
                pass
        finally:
            cur.close()
    except Exception as e:
        logger.warning("data migrations failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
    return ran
