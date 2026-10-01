"""N-3.4: one schema story. Static checks that run everywhere, plus a fresh-DB
bootstrap that runs only when a real MySQL is reachable (CI)."""

import os
import re
import socket
import unittest

os.environ.setdefault("SANDBOX", "1")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SKIP_DIRS = {".git", "node_modules", "tests", "sandbox", "migrations", "__pycache__", ".claude", "static", "docs", "scripts"}
# Tables that are deliberately created by more than one module because each one
# owns a private shadow (SQLite AI store or per-feature lazily-created tables).
ALLOWED_DUPLICATES = set()


def _py_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


class SingleDefinitionTests(unittest.TestCase):
    def test_no_table_is_created_twice_with_different_shapes(self):
        """course_orders/order_approvals used to be defined in enterprise_tables AND
        futurematch_ui with different columns. Now each table has one definition."""
        seen = {}
        pat = re.compile(r"CREATE TABLE IF NOT EXISTS\s+`?(\w+)`?", re.I)
        for path in _py_files():
            try:
                text = open(path, encoding="utf-8").read()
            except Exception:
                continue
            for m in pat.finditer(text):
                seen.setdefault(m.group(1).lower(), set()).add(os.path.relpath(path, ROOT))
        dupes = {t: sorted(p) for t, p in seen.items() if len(p) > 1 and t not in ALLOWED_DUPLICATES}
        # event_outbox is intentionally ensured lazily by event_bus AND listed in
        # enterprise_tables (comment there says event_bus holds the authoritative copy).
        dupes.pop("event_outbox", None)
        self.assertEqual(dupes, {}, "tables defined in more than one module: %s" % dupes)

    def test_core_order_tables_live_only_in_enterprise_tables(self):
        text = open(os.path.join(ROOT, "futurematch_ui.py"), encoding="utf-8").read()
        self.assertNotIn("CREATE TABLE IF NOT EXISTS course_orders", text)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS order_approvals", text)

    def test_previously_uncreated_tables_are_defined(self):
        from enterprise_tables import _parse_table_name, enterprise_table_ddls
        names = {_parse_table_name(s) for s in enterprise_table_ddls()}
        for t in ("users", "brands", "notifications", "credit_usage", "app_usage", "social_metrics",
                  "order_status_history", "company_team_order_policy", "schema_meta"):
            self.assertIn(t, names)

    def test_every_ddl_parses_into_columns(self):
        from enterprise_tables import _parse_columns_from_create, _parse_table_name, enterprise_table_ddls
        for sql in enterprise_table_ddls():
            name = _parse_table_name(sql)
            self.assertTrue(name, sql[:60])
            cols = [c for c, _ in _parse_columns_from_create(sql)]
            self.assertTrue(cols, "no columns parsed for " + name)

    def test_order_lifecycle_columns_are_in_the_single_definition(self):
        from schema_registry import expected_tables
        cols = set(expected_tables()["course_orders"])
        for c in ("billing_status", "vendor_id", "invoice_due_date", "payment_reference", "billing_note",
                  "booked_at", "booked_by", "group_order_id", "request_notes", "payment_method"):
            self.assertIn(c, cols)

    def test_fingerprint_changes_with_the_definitions(self):
        import enterprise_tables as et
        before = et.ddl_fingerprint()
        self.assertEqual(before, et.ddl_fingerprint())   # stable
        orig = et.enterprise_table_ddls
        try:
            et.enterprise_table_ddls = lambda: orig() + ["CREATE TABLE IF NOT EXISTS zz (id INT PRIMARY KEY)"]
            self.assertNotEqual(before, et.ddl_fingerprint())
        finally:
            et.enterprise_table_ddls = orig

    def test_alembic_revision_chains_off_the_baseline(self):
        text = open(os.path.join(ROOT, "migrations", "versions", "b001_order_lifecycle_and_notifications.py"),
                    encoding="utf-8").read()
        self.assertIn('down_revision: Union[str, Sequence[str], None] = "0002_performance_indexes"', text)

    def test_legacy_status_migration_covers_every_old_value(self):
        from schema_registry import ORDER_LIFECYCLE_MIGRATION_SQL
        joined = " ".join(ORDER_LIFECYCLE_MIGRATION_SQL)
        for legacy in ("pending", "processing", "confirmed", "invoiced", "paid"):
            self.assertIn("'%s'" % legacy, joined)


def _mysql_reachable():
    try:
        with socket.create_connection((os.environ.get("MYSQL_HOST", "127.0.0.1"),
                                       int(os.environ.get("MYSQL_PORT", "3306"))), timeout=1):
            return True
    except OSError:
        return False


@unittest.skipUnless(_mysql_reachable() and os.environ.get("MYSQL_USER") not in (None, "none"),
                     "needs a real MySQL (CI service container)")
class FreshDatabaseBootstrapTests(unittest.TestCase):
    def test_fresh_db_bootstraps_and_verifies_clean(self):
        os.environ["ENTERPRISE_TABLE_SYNC_FORCE"] = "1"
        import run
        from enterprise_tables import ensure_enterprise_tables
        from schema_registry import verify_schema
        app = run.create_app()
        ensure_enterprise_tables(app)
        with app.app_context():
            report = verify_schema(app.mysql.connection)
        self.assertEqual(report.get("missing_tables"), [])
        self.assertEqual(report.get("missing_columns"), {})


if __name__ == "__main__":
    unittest.main()
