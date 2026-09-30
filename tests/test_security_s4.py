"""
Regression tests for Part A tier S-4 (privacy & GDPR).

S-4.1 erasure by identity, S-4.2 self-service data rights + DSR tickets,
S-4.3 retention, S-4.4 per-goal sharing, S-4.5 AI store residency.
(The repo-wide "new table with personal data must be covered" guard lives in
tests/test_gdpr_table_coverage.py.)
"""

import os
import re
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import db_compat  # noqa: F401
from tests import secapp
from tests.secapp import FakeMySQL, client_as, get_app, login, patch_mysql

import gdpr_service


def _n(sql):
    return " ".join(sql.split())


# ---------------------------------------------------------------------------
# S-4.1
# ---------------------------------------------------------------------------
class S41_ErasureByIdentity(unittest.TestCase):
    def _responder(self):
        def r(sql, params):
            s = _n(sql).lower()
            if s.startswith("select id, username, email from users where username"):
                return {"id": 5, "username": "anna", "email": "Anna@Firma.dk"}
            if s.startswith("select id, company_id, email from company_users"):
                # a SCIM row matched by e-mail (no username) + the normal row
                return [{"id": 71, "company_id": 7, "email": "anna@firma.dk"},
                        {"id": 72, "company_id": 8, "email": "anna@other.dk"}]
            if s.startswith("select distinct session_id from"):
                return [{"session_id": "sess-1"}, {"session_id": "sess-2"}] if "conversation_history" in s else []
            if s.startswith("select count(*)"):
                return {"COUNT(*)": 2}
            return None
        return r

    def _run(self, dry_run, **kw):
        app = get_app()
        fake, p = patch_mysql(app, self._responder())
        with p, app.app_context():
            report = gdpr_service.erase_user_data("anna", actor="root", dry_run=dry_run, **kw)
        return report, fake

    def test_subject_is_resolved_across_username_id_email_and_memberships(self):
        app = get_app()
        fake, p = patch_mysql(app, self._responder())
        with p, app.app_context():
            subject = gdpr_service.resolve_subject(app.mysql.connection, "anna")
        self.assertEqual(subject["user_id"], 5)
        self.assertEqual(subject["emails"], ["anna@firma.dk", "anna@other.dk"])
        self.assertEqual(subject["member_ids"], [71, 72])
        self.assertEqual(subject["company_ids"], [7, 8])
        self.assertEqual(subject["session_ids"], ["sess-1", "sess-2"])

    def test_dry_run_changes_nothing_but_lists_every_family(self):
        report, fake = self._run(dry_run=True)
        self.assertTrue(report["ok"])
        writes = [q for q in fake.log if re.match(r"(delete|update|insert)", q[0], re.I)]
        self.assertEqual(writes, [])
        for table in ("employee_goals", "employee_skills_matrix", "employee_skill_history", "email_log",
                      "ai_agent_runs", "hr_chatbot_interactions", "user_2fa", "password_reset_tokens",
                      "ai_cv_parse_jobs", "employee_learning_progress"):
            self.assertIn(table, report["deleted"], table)
        for table in ("audit_log", "order_approvals", "course_reviews", "dsr_requests", "users", "company_users"):
            self.assertIn(table, report["anonymised"], table)

    def test_execute_hits_identity_keyed_tables_with_the_right_keys(self):
        report, fake = self._run(dry_run=False)
        self.assertTrue(report["ok"], report["errors"])
        sql = [(_n(q[0]), q[1]) for q in fake.log]

        def find(prefix, containing=""):
            return [q for q in sql if q[0].startswith(prefix) and containing in q[0]]

        goals = find("DELETE FROM `employee_goals`")
        self.assertEqual(len(goals), 1)
        self.assertIn("`company_id` IN (%s,%s)", goals[0][0])            # tenant-scoped
        self.assertEqual(goals[0][1], (5, 7, 8))

        hist = find("DELETE FROM `employee_skill_history`")
        self.assertEqual(hist[0][1], (5, 71, 72, 7, 8))                  # users.id AND company_users ids

        mail = find("DELETE FROM `email_log`")
        self.assertEqual(mail[0][1], ("anna@firma.dk", "anna@other.dk"))   # keyed by e-mail

        cvjobs = find("DELETE FROM `ai_cv_parse_jobs`")
        self.assertEqual(cvjobs[0][1], ("sess-1", "sess-2"))             # keyed by AI session

        tokens = find("DELETE FROM `password_reset_tokens`")
        self.assertIn("`account_type`='user'", tokens[0][0])

        members = find("UPDATE `company_users` SET status='inactive'")
        self.assertTrue(any(5 in q[1] and "anna@firma.dk" in q[1] for q in members))   # by user_id AND e-mail

        users = find("UPDATE `users` SET password='!erased'")
        self.assertEqual(users[0][1], (5,))                               # login is disabled

    def test_audit_log_is_pseudonymised_never_deleted(self):
        report, fake = self._run(dry_run=False)
        sql = [_n(q[0]) for q in fake.log]
        self.assertFalse(any(s.startswith("DELETE FROM `audit_log`") for s in sql))
        upd = [q for q in fake.log if _n(q[0]).startswith("UPDATE `audit_log`")]
        self.assertEqual(len(upd), 2)                                    # actor rows + "about this user" rows
        actor_sql = _n(upd[0][0])
        self.assertIn("user_id=NULL", actor_sql)
        self.assertIn("ip_address=NULL", actor_sql)
        self.assertIn("REPLACE(", actor_sql)
        pseudo = gdpr_service._pseudonym({"user_id": 5, "username": "anna"})
        self.assertIn(pseudo, upd[0][1])
        self.assertIn("anna", upd[0][1])
        self.assertIn("anna@firma.dk", upd[0][1])
        self.assertTrue(pseudo.startswith("slettet-bruger-"))

    def test_order_approvals_both_roles_are_handled(self):
        report, fake = self._run(dry_run=False)
        upd = [_n(q[0]) for q in fake.log if _n(q[0]).startswith("UPDATE `order_approvals`")]
        self.assertTrue(any("requester_user_id=0" in u for u in upd))
        self.assertTrue(any("approver_user_id=NULL" in u for u in upd))

    def test_course_orders_are_matched_by_user_id_and_email_too(self):
        report, fake = self._run(dry_run=False)
        upd = [q for q in fake.log if _n(q[0]).startswith("UPDATE `course_orders` SET user_email=NULL, user_name=NULL, user_phone=NULL, user_id=NULL")]
        self.assertEqual(len(upd), 1)
        self.assertEqual(upd[0][1], (5, "anna@firma.dk", "anna@other.dk"))

    def test_nothing_runs_for_a_subject_without_any_identifier(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p, app.app_context():
            report = gdpr_service.erase_user_data("ghost", actor="root", dry_run=False)
        for q in fake.log:
            s = _n(q[0])
            # no spec may fall back to an unfiltered statement
            if s.startswith(("DELETE FROM `employee_goals`", "DELETE FROM `email_log`", "DELETE FROM `user_2fa`")) \
                    or (s.startswith("UPDATE `audit_log`") and "`user_id`=" in s):
                self.fail("unexpected statement for an unknown subject: %s" % s)

    def test_sqlite_ai_store_is_erased_through_session_ids(self):
        from app1 import memory_store
        tmp = tempfile.TemporaryDirectory()
        orig = memory_store.DB_PATH
        conn = getattr(memory_store._local, "conn", None)
        if conn is not None:
            conn.close()
        memory_store._local.conn = None
        memory_store.DB_PATH = os.path.join(tmp.name, "t.db")
        try:
            memory_store.init_db()
            memory_store.log_event("sess-1", "feedback", query_text="q", feedback_rating=1)
            memory_store.log_event("sess-other", "feedback", query_text="keep", feedback_rating=1)
            memory_store.log_debug("sess-1", "step", {"a": 1})
            subject = {"session_ids": ["sess-1"], "emails": [], "member_ids": [], "company_ids": []}
            plan = gdpr_service._sqlite_ai_store_plan(subject)
            self.assertEqual(plan["analytics"], 1)
            self.assertEqual(plan["debug_logs"], 1)
            done = gdpr_service._sqlite_ai_store_erase(subject)
            self.assertEqual(done["analytics"], 1)
            c = memory_store._get_conn()
            left = [r[0] for r in c.execute("SELECT session_id FROM analytics").fetchall()]
            self.assertEqual(left, ["sess-other"])                        # only THIS person's sessions
        finally:
            c = getattr(memory_store._local, "conn", None)
            if c is not None:
                c.close()
            memory_store._local.conn = None
            memory_store.DB_PATH = orig
            tmp.cleanup()

    def test_export_contains_the_new_families_and_no_secrets(self):
        app = get_app()
        fake, p = patch_mysql(app, self._responder())
        with p, app.app_context():
            data = gdpr_service.collect_user_data("anna")
        for table in ("employee_goals", "email_log", "ai_agent_runs", "user_2fa", "audit_log"):
            self.assertIn(table, data["tables"], table)
        two_fa = [q for q in fake.log if "FROM user_2fa" in _n(q[0])]
        self.assertTrue(two_fa)
        self.assertNotIn("secret_enc", _n(two_fa[0][0]))                 # never exports the TOTP secret
        self.assertNotIn("backup_codes", _n(two_fa[0][0]))
        tok = [q for q in fake.log if "FROM password_reset_tokens" in _n(q[0])]
        self.assertNotIn("token_hash", _n(tok[0][0]))

    def test_redaction_list_covers_secrets(self):
        for col in ("password", "secret_enc", "backup_codes", "token_hash"):
            self.assertIn(col, gdpr_service._REDACT_COLUMNS)


# ---------------------------------------------------------------------------
# S-4.2
# ---------------------------------------------------------------------------
class FakeDsrDb:
    def __init__(self):
        self.rows = []

    def cursor(self, *a, **k):
        return _DsrCur(self)

    def commit(self):
        pass


class _DsrCur:
    def __init__(self, db):
        self.db = db
        self._res = []
        self.rowcount = 0
        self.lastrowid = 0

    def execute(self, sql, params=None):
        s = _n(sql).lower()
        self._res = []
        self.rowcount = 0
        if s.startswith("create table"):
            return
        if s.startswith("select * from dsr_requests where request_type = %s and user_id <=> %s"):
            self._res = [r for r in self.db.rows if r["request_type"] == params[0] and r["user_id"] == params[1]
                         and r["status"] in ("open", "in_progress")][-1:]
        elif s.startswith("insert into dsr_requests"):
            rid = len(self.db.rows) + 1
            now = params[5]
            self.db.rows.append({"id": rid, "request_type": params[0], "user_id": params[1], "username": params[2],
                                 "email": params[3], "status": "open", "reason": params[4], "requested_at": now,
                                 "due_at": now + timedelta(days=30)})
            self.lastrowid = rid
        elif s.startswith("select * from dsr_requests where id"):
            self._res = [r for r in self.db.rows if r["id"] == params[0]]
        elif s.startswith("update dsr_requests set status = %s"):
            for r in self.db.rows:
                if r["id"] == params[4] and r["status"] in ("open", "in_progress"):
                    r["status"] = params[0]
                    self.rowcount += 1

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return list(self._res)

    def close(self):
        pass


class S42_DataRights(unittest.TestCase):
    def setUp(self):
        import dsr_service
        dsr_service._TABLE_READY = False
        self.dsr = dsr_service

    def test_ticket_has_a_30_day_sla_and_is_deduplicated(self):
        db = FakeDsrDb()
        now = datetime(2026, 10, 1, 9, 0)
        t1, created1 = self.dsr.create_request(db, user_id=5, username="anna", email="a@x.dk", now=now)
        t2, created2 = self.dsr.create_request(db, user_id=5, username="anna", email="a@x.dk", now=now)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(len(db.rows), 1)
        self.assertEqual(t1["due_at"] - t1["requested_at"], timedelta(days=30))

    def test_overdue_flag(self):
        row = {"status": "open", "due_at": datetime(2026, 1, 1)}
        self.assertTrue(self.dsr.is_overdue(row, now=datetime(2026, 2, 1)))
        self.assertFalse(self.dsr.is_overdue(row, now=datetime(2025, 12, 1)))
        self.assertFalse(self.dsr.is_overdue({"status": "completed", "due_at": datetime(2026, 1, 1)},
                                             now=datetime(2026, 2, 1)))

    def test_learner_can_request_erasure_from_settings(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p, mock.patch("dsr_service.create_request",
                           return_value=({"id": 9}, True)) as create, \
                mock.patch("gdpr_routes._notify_admins_of_request") as notify:
            c = client_as(app, "employee")
            r = c.post("/mine-data/anmod-sletning", data={"confirm": "1", "reason": "skifter job"})
            self.assertEqual(r.status_code, 302)
            create.assert_called_once()
            self.assertEqual(create.call_args.kwargs["request_type"], "erasure")
            self.assertEqual(create.call_args.kwargs["user_id"], 11)
            notify.assert_called_once()
            with c.session_transaction() as s:
                flashes = " ".join(m for _, m in s.get("_flashes", []))
            self.assertIn("30 dage", flashes)

    def test_request_needs_explicit_confirmation_and_login(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p, mock.patch("dsr_service.create_request") as create:
            client_as(app, "employee").post("/mine-data/anmod-sletning", data={})
            client_as(app, "anon").post("/mine-data/anmod-sletning", data={"confirm": "1"})
            create.assert_not_called()

    def test_second_request_tells_the_user_one_is_already_open(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p, mock.patch("dsr_service.create_request", return_value=({"id": 9}, False)), \
                mock.patch("gdpr_routes._notify_admins_of_request") as notify:
            c = client_as(app, "employee")
            c.post("/mine-data/anmod-sletning", data={"confirm": "1"})
            notify.assert_not_called()
            with c.session_transaction() as s:
                flashes = " ".join(m for _, m in s.get("_flashes", []))
            self.assertIn("allerede", flashes)

    def test_settings_and_privacy_pages_link_the_rights(self):
        settings = open(secapp._REPO_ROOT + "/templates/fm/settings.html", encoding="utf-8").read()
        self.assertIn("gdpr.my_data", settings)
        self.assertIn("gdpr.request_erasure", settings)
        self.assertIn("Anmod om sletning", settings)
        privacy = open(secapp._REPO_ROOT + "/templates/fm/privacy.html", encoding="utf-8").read()
        self.assertIn("pages.settings", privacy)
        self.assertIn("30 dage", privacy)

    def test_admin_console_shows_queue_with_overdue_and_only_admins_reach_it(self):
        app = get_app()
        overdue = {"id": 3, "username": "anna", "email": "a@x.dk", "request_type": "erasure", "status": "open",
                   "requested_at": datetime(2026, 1, 1), "due_at": datetime(2026, 1, 31), "overdue": True}
        fake, p = patch_mysql(app)
        with p, mock.patch("gdpr_routes._load_dsr_queue", return_value=[overdue]):
            html = client_as(app, "admin").get("/admin/gdpr").get_data(as_text=True)
            self.assertIn("Anmodninger fra brugere", html)
            self.assertIn("Overskredet", html)
            for role in ("employee", "hr_manager", "company_admin", "anon"):
                r = client_as(app, role).get("/admin/gdpr")
                self.assertIn(r.status_code, (301, 302, 401, 403), role)

    def test_executing_an_erasure_closes_the_tickets(self):
        app = get_app()
        closed = []
        fake, p = patch_mysql(app, lambda sql, params: {"id": 5} if "from users where username" in _n(sql).lower() else None)
        with p, mock.patch("dsr_service.open_erasure_ticket_ids", return_value=[3, 4]), \
                mock.patch("dsr_service.set_status", side_effect=lambda conn, tid, status, actor, note=None: closed.append((tid, status))), \
                mock.patch("gdpr_service.erase_user_data", return_value={
                    "ok": True, "deleted": {}, "anonymised": {}, "errors": [], "ai_memory": {}}), \
                mock.patch("gdpr_service.collect_user_data", return_value={"row_counts": {}, "errors": []}), \
                mock.patch("gdpr_routes._load_dsr_queue", return_value=[]):
            client_as(app, "admin").post("/admin/gdpr/erase", data={
                "username": "anna", "confirm_username": "anna", "mode": "execute"})
        self.assertEqual(closed, [(3, "completed"), (4, "completed")])

    def test_ticket_rows_are_anonymised_after_erasure(self):
        specs = [s for s in gdpr_service.EXTRA_SPECS if s["table"] == "dsr_requests"]
        self.assertEqual(len(specs), 1)
        self.assertIn("email=NULL", specs[0]["set"])
        self.assertNotIn("status", specs[0]["set"])       # proof of handling stays


# ---------------------------------------------------------------------------
# S-4.3
# ---------------------------------------------------------------------------
class S43_Retention(unittest.TestCase):
    def setUp(self):
        import retention_service
        self.rs = retention_service

    def _policy(self, name):
        return next(p for p in self.rs.POLICIES if p["name"] == name)

    def test_covers_transcripts_debug_api_and_email_logs(self):
        names = {p["name"] for p in self.rs.POLICIES} | {p["name"] for p in self.rs.SQLITE_POLICIES}
        for must in ("chatbot_interactions", "hr_chatbot_interactions", "conversation_history",
                     "ai_store_debug_logs", "api_request_logs", "email_log", "ai_agent_runs", "ai_tool_runs"):
            self.assertIn(must, names)
        self.assertNotIn("audit_log", names)       # legal record: pseudonymised, never aged out here

    def test_transcripts_are_scrubbed_not_deleted_so_statistics_survive(self):
        p = self._policy("chatbot_interactions")
        sql, params = self.rs.build_statement(p, 730, now=2_000_000_000)
        self.assertTrue(sql.startswith("UPDATE `chatbot_interactions` SET username=NULL, query_text=NULL"))
        self.assertIn("LIMIT", sql)
        self.assertEqual(params, [2_000_000_000 - 730 * 86400])

    def test_logs_are_deleted_in_bounded_batches(self):
        sql, _ = self.rs.build_statement(self._policy("email_log"), 365)
        self.assertTrue(sql.startswith("DELETE FROM `email_log` WHERE `created_at` < FROM_UNIXTIME(%s) LIMIT "))

    def test_env_override_changes_and_disables_a_rule(self):
        p = self._policy("email_log")
        self.assertEqual(self.rs.effective_days(p, {}), 365)
        self.assertEqual(self.rs.effective_days(p, {"RETENTION_EMAIL_LOG_DAYS": "30"}), 30)
        self.assertIsNone(self.rs.effective_days(p, {"RETENTION_EMAIL_LOG_DAYS": "off"}))
        self.assertIsNone(self.rs.effective_days(p, {"RETENTION_EMAIL_LOG_DAYS": "0"}))
        self.assertEqual(self.rs.effective_days(p, {"RETENTION_EMAIL_LOG_DAYS": "banana"}), 365)

    def test_outbox_only_purges_delivered_rows(self):
        sql, _ = self.rs.build_statement(self._policy("event_outbox"), 30)
        self.assertIn("status = 'delivered'", sql)

    def test_run_loops_until_a_short_batch_and_reports_per_table(self):
        self.rs.BATCH, old = 3, self.rs.BATCH
        try:
            counts = iter([3, 3, 1])

            class Cur:
                rowcount = 0

                def execute(s, sql, params=None):
                    s.rowcount = next(counts, 0)

                def close(s):
                    pass

            class Conn:
                commits = 0

                def cursor(s):
                    return Cur()

                def commit(s):
                    Conn.commits += 1

                def rollback(s):
                    pass

            env = {("RETENTION_%s_DAYS" % p["name"].upper()): "off" for p in self.rs.POLICIES if p["name"] != "email_log"}
            env.update({("RETENTION_%s_DAYS" % p["name"].upper()): "off" for p in self.rs.SQLITE_POLICIES})
            summary = self.rs.run_retention(Conn(), environ=env)
        finally:
            self.rs.BATCH = old
        self.assertEqual(summary["email_log"], 7)
        self.assertEqual(summary["api_request_logs"], "off")

    def test_one_failing_table_does_not_stop_the_rest(self):
        class Cur:
            rowcount = 0

            def execute(s, sql, params=None):
                if "api_request_logs" in sql:
                    raise RuntimeError("table missing")

            def close(s):
                pass

        class Conn:
            def cursor(s):
                return Cur()

            def commit(s):
                pass

            def rollback(s):
                pass

        env = {("RETENTION_%s_DAYS" % p["name"].upper()): "off" for p in self.rs.SQLITE_POLICIES}
        summary = self.rs.run_retention(Conn(), environ=env)
        self.assertTrue(str(summary["api_request_logs"]).startswith("error"))
        self.assertEqual(summary["email_log"], 0)

    def test_dry_run_counts_without_deleting(self):
        seen = []

        class Cur:
            def execute(s, sql, params=None):
                seen.append(sql)

            def fetchone(s):
                return (4,)

            def close(s):
                pass

        class Conn:
            def cursor(s):
                return Cur()

            def commit(s):
                seen.append("COMMIT")

        env = {("RETENTION_%s_DAYS" % p["name"].upper()): "off" for p in self.rs.SQLITE_POLICIES}
        summary = self.rs.run_retention(Conn(), dry_run=True, environ=env)
        self.assertEqual(summary["email_log"], 4)
        self.assertTrue(all(s.startswith("SELECT COUNT(*)") for s in seen))

    def test_sqlite_ai_store_is_aged_out(self):
        from app1 import memory_store
        tmp = tempfile.TemporaryDirectory()
        orig = memory_store.DB_PATH
        conn = getattr(memory_store._local, "conn", None)
        if conn is not None:
            conn.close()
        memory_store._local.conn = None
        memory_store.DB_PATH = os.path.join(tmp.name, "t.db")
        try:
            memory_store.init_db()
            c = memory_store._get_conn()
            old, fresh = time.time() - 400 * 86400, time.time()
            c.execute("INSERT INTO analytics (session_id, timestamp, event_type) VALUES ('old', ?, 'x')", (old,))
            c.execute("INSERT INTO analytics (session_id, timestamp, event_type) VALUES ('new', ?, 'x')", (fresh,))
            c.commit()
            out = self.rs._run_sqlite()
            self.assertEqual(out["ai_store_analytics"], 1)
            left = [r[0] for r in c.execute("SELECT session_id FROM analytics").fetchall()]
            self.assertEqual(left, ["new"])
        finally:
            c = getattr(memory_store._local, "conn", None)
            if c is not None:
                c.close()
            memory_store._local.conn = None
            memory_store.DB_PATH = orig
            tmp.cleanup()

    def test_scheduler_runs_it_daily(self):
        import scheduler
        job = next(j for j in scheduler.JOBS if j["name"] == "data_retention")
        self.assertEqual(job["interval_seconds"], 86400)
        self.assertTrue(job["enabled"])

    def test_policy_table_for_admin_overview(self):
        rows = self.rs.policy_table()
        self.assertTrue(all("days" in r and "action" in r for r in rows))


# ---------------------------------------------------------------------------
# S-4.4
# ---------------------------------------------------------------------------
class GoalDb:
    """Emulates employee_goals + company_settings for goal_sharing."""

    def __init__(self):
        self.goals = []
        self.ai_switch = None
        self.ddl = []

    def cursor(self, *a, **k):
        return _GoalCur(self)

    def commit(self):
        pass

    def rollback(self):
        pass


class _GoalCur:
    def __init__(self, db):
        self.db = db
        self._res = []
        self.rowcount = 0
        self.lastrowid = 0

    def execute(self, sql, params=None):
        s = _n(sql).lower()
        self._res, self.rowcount = [], 0
        db = self.db
        if s.startswith("show columns"):
            self._res = [{"Field": "x"}]        # columns "exist" -> no ALTERs
        elif s.startswith("insert into employee_goals"):
            gid = len(db.goals) + 1
            shared = ", 1, now()" in s
            db.goals.append({"id": gid, "employee_id": params[0], "company_id": params[1], "goal_title": params[2],
                             "goal_description": params[3], "target_date": params[4], "status": "active",
                             "progress": 0, "shared_with_employee": params[5], "shared_at": None,
                             "shared_by": None, "share_note": None})
            self.lastrowid = gid
        elif s.startswith("select id, employee_id, goal_title, shared_with_employee"):
            self._res = [g for g in db.goals if g["id"] == params[0] and g["company_id"] == params[1]]
        elif s.startswith("update employee_goals set shared_with_employee = 1"):
            for g in db.goals:
                if g["id"] == params[2] and g["company_id"] == params[3]:
                    g.update(shared_with_employee=1, shared_by=params[0], share_note=params[1])
                    self.rowcount += 1
        elif s.startswith("update employee_goals set shared_with_employee = 0"):
            for g in db.goals:
                if g["id"] == params[0] and g["company_id"] == params[1]:
                    g.update(shared_with_employee=0, shared_by=None, share_note=None)
                    self.rowcount += 1
        elif "from employee_goals where employee_id = %s and company_id = %s and shared_with_employee = 1" in s:
            self._res = [g for g in db.goals if g["employee_id"] == params[0] and g["company_id"] == params[1]
                         and g["shared_with_employee"]]
        elif s.startswith("select id, goal_title") and "from employee_goals where employee_id" in s:
            self._res = [g for g in db.goals if g["employee_id"] == params[0] and g["company_id"] == params[1]]
        elif s.startswith("select ai_learner_hr_goals"):
            self._res = [{"ai_learner_hr_goals": db.ai_switch}] if db.ai_switch is not None else []
        elif s.startswith("update company_settings set ai_learner_hr_goals"):
            db.ai_switch = params[0]
            self.rowcount = 1

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return list(self._res)

    def close(self):
        pass


class S44_GoalSharing(unittest.TestCase):
    def setUp(self):
        import goal_sharing
        goal_sharing._SCHEMA_READY = False
        self.gs = goal_sharing
        self.db = GoalDb()

    def test_new_goals_are_private_by_default(self):
        gid = self.gs.create_goal(self.db, company_id=7, employee_user_id=11, title="Bliv teamleder")
        self.assertEqual(self.db.goals[0]["shared_with_employee"], 0)
        self.assertEqual(self.gs.list_shared_goals_for_learner(self.db, 11, 7), [])
        sections = self.gs.list_goals_for_hr(self.db, 7, 11)
        self.assertEqual(len(sections["hr_only"]), 1)
        self.assertEqual(sections["shared"], [])

    def test_unshared_goal_never_reaches_the_learner_only_shared_does(self):
        a = self.gs.create_goal(self.db, company_id=7, employee_user_id=11, title="HR-notat: performance")
        b = self.gs.create_goal(self.db, company_id=7, employee_user_id=11, title="Lær Excel", shared=True,
                                actor_user_id=13)
        titles = [r["goal_title"] for r in self.gs.list_shared_goals_for_learner(self.db, 11, 7)]
        self.assertEqual(titles, ["Lær Excel"])
        self.assertNotIn("HR-notat: performance", titles)

    def test_goal_moves_between_sections_and_is_audited(self):
        gid = self.gs.create_goal(self.db, company_id=7, employee_user_id=11, title="Mål")
        with mock.patch("security_audit.audit") as audit:
            self.gs.set_shared(self.db, goal_id=gid, company_id=7, shared=True, actor_user_id=13, note="Godt arbejde")
            sections = self.gs.list_goals_for_hr(self.db, 7, 11)
            self.assertEqual(len(sections["shared"]), 1)
            self.gs.set_shared(self.db, goal_id=gid, company_id=7, shared=False, actor_user_id=13)
            self.assertEqual(len(self.gs.list_goals_for_hr(self.db, 7, 11)["hr_only"]), 1)
            actions = [c.args[0] for c in audit.call_args_list]
            self.assertEqual(actions, ["hr.goal.share", "hr.goal.unshare"])

    def test_no_audit_noise_when_nothing_changes(self):
        gid = self.gs.create_goal(self.db, company_id=7, employee_user_id=11, title="Mål")
        with mock.patch("security_audit.audit") as audit:
            self.gs.set_shared(self.db, goal_id=gid, company_id=7, shared=False, actor_user_id=13)
            audit.assert_not_called()

    def test_sharing_is_tenant_scoped(self):
        gid = self.gs.create_goal(self.db, company_id=7, employee_user_id=11, title="Mål")
        self.assertIsNone(self.gs.set_shared(self.db, goal_id=gid, company_id=8, shared=True, actor_user_id=99))
        self.assertEqual(self.db.goals[0]["shared_with_employee"], 0)

    def test_company_switch_defaults_on_and_can_be_turned_off(self):
        self.assertTrue(self.gs.company_shares_with_ai(self.db, 7))
        self.gs.set_company_ai_sharing(self.db, 7, False)
        self.assertFalse(self.gs.company_shares_with_ai(self.db, 7))
        self.gs.set_company_ai_sharing(self.db, 7, True)
        self.assertTrue(self.gs.company_shares_with_ai(self.db, 7))

    def test_every_learner_facing_goal_query_uses_the_shared_predicate(self):
        """Structural guard: any SELECT on employee_goals outside HR/analytics
        code must carry the sharing predicate."""
        allowed = {"goal_sharing.py", "enterprise_analytics", "gdpr_service.py", "enterprise_tables.py"}
        offenders = []
        for root, dirs, files in os.walk(secapp._REPO_ROOT):
            dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".claude", "tests", "sandbox"}]
            for fn in files:
                if not fn.endswith(".py") or fn in allowed or os.path.basename(root) in allowed:
                    continue
                text = open(os.path.join(root, fn), encoding="utf-8", errors="ignore").read()
                for m in re.finditer(r"FROM\s+employee_goals\b(.{0,400})", text, re.S | re.I):
                    if "shared_with_employee" not in m.group(1):
                        offenders.append(fn)
        self.assertEqual(offenders, [], "Læser employee_goals uden delt-filter: %s" % offenders)

    def test_learner_export_excludes_unshared_goals_but_admin_export_includes_them(self):
        app = get_app()
        def responder(sql, params):
            s = _n(sql).lower()
            if s.startswith("select id, username, email from users"):
                return {"id": 5, "username": "anna", "email": "a@x.dk"}
            if s.startswith("select id, company_id, email from company_users"):
                return [{"id": 71, "company_id": 7, "email": "a@x.dk"}]
            return None

        fake, p = patch_mysql(app, responder)
        with p, app.app_context():
            gdpr_service.collect_user_data("anna", learner_view=True)
            learner_sql = [_n(q[0]) for q in fake.log if "FROM `employee_goals`" in q[0]]
            fake.log.clear()
            gdpr_service.collect_user_data("anna", learner_view=False)
            admin_sql = [_n(q[0]) for q in fake.log if "FROM `employee_goals`" in q[0]]
        self.assertTrue(learner_sql and all("shared_with_employee = 1" in s for s in learner_sql))
        self.assertTrue(admin_sql and not any("shared_with_employee" in s for s in admin_sql))

    def test_self_service_download_is_the_learner_view(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p, mock.patch("gdpr_service.export_user_data", return_value="{}") as export:
            client_as(app, "employee").get("/mine-data")
            self.assertTrue(export.call_args.kwargs.get("learner_view"))

    def test_learner_api_returns_only_shared_goals(self):
        app = get_app()
        rows = [{"id": 1, "goal_title": "Lær Excel", "goal_description": "", "target_date": None,
                 "status": "active", "progress": 10, "share_note": "Godt"}]
        fake, p = patch_mysql(app)
        with p, mock.patch("goal_sharing.list_shared_goals_for_learner", return_value=rows) as lst:
            r = client_as(app, "employee").get("/api/my/manager-goals")
        self.assertEqual(r.get_json()["goals"][0]["title"], "Lær Excel")
        self.assertEqual(lst.call_args.args[1:], (11, 7))        # the caller's OWN user + company
        self.assertEqual(client_as(app, "anon").get("/api/my/manager-goals").status_code, 401)

    # -- HR endpoints: permission boundary --------------------------------
    def _hr_responder(self, dept="Salg"):
        def r(sql, params):
            s = _n(sql).lower()
            if "from companies c" in s:
                return {"id": 7, "company_name": "Acme", "user_role": "hr_manager", "department": None, "permissions": None}
            if s.startswith("select user_id, department from company_users"):
                return {"user_id": params[1], "department": dept}
            return None
        return r

    def test_only_hr_can_change_sharing_and_the_boundary_holds(self):
        app = get_app()
        fake, p = patch_mysql(app, self._hr_responder())
        with p, mock.patch("goal_sharing.set_shared", return_value={"id": 1}) as setter, \
                mock.patch("goal_sharing.create_goal", return_value=4) as creator, \
                mock.patch("goal_sharing.ensure_schema"):
            for role in ("anon", "employee", "dept_head"):
                c = client_as(app, role)
                r = c.post("/hr/goals/1/share", json={"shared": True})
                self.assertIn(r.status_code, (401, 403), role)
                r = c.post("/hr/employee/11/goals", json={"title": "x"})
                self.assertIn(r.status_code, (401, 403), role)
            setter.assert_not_called()
            creator.assert_not_called()
            r = client_as(app, "hr_manager").post("/hr/goals/1/share", json={"shared": True, "note": "Flot"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(setter.call_args.kwargs["shared"])
            r = client_as(app, "hr_manager").post("/hr/employee/11/goals", json={"title": "Nyt mål"})
            self.assertEqual(r.status_code, 201)
            self.assertFalse(creator.call_args.kwargs["shared"])            # private unless HR says so

    def test_hr_list_returns_the_two_named_sections(self):
        app = get_app()
        fake, p = patch_mysql(app, self._hr_responder())
        with p, mock.patch("goal_sharing.list_goals_for_hr", return_value={"shared": [{"id": 1}], "hr_only": [{"id": 2}]}):
            r = client_as(app, "hr_manager").get("/hr/employee/11/goals")
        body = r.get_json()
        self.assertEqual(body["delt_med_medarbejderen"], [{"id": 1}])
        self.assertEqual(body["kun_synligt_for_hr"], [{"id": 2}])

    def test_employee_cannot_list_hr_goals(self):
        app = get_app()
        fake, p = patch_mysql(app, self._hr_responder())
        with p, mock.patch("goal_sharing.list_goals_for_hr") as lst:
            r = client_as(app, "employee").get("/hr/employee/11/goals")
        self.assertIn(r.status_code, (401, 403))
        lst.assert_not_called()

    def test_company_switch_endpoint_is_hr_only_and_audited(self):
        app = get_app()
        fake, p = patch_mysql(app, self._hr_responder())
        with p, mock.patch("goal_sharing.set_company_ai_sharing") as setter, mock.patch("goal_sharing.ensure_schema"):
            r = client_as(app, "employee").post("/hr/settings/ai-hr-goals", json={"enabled": False})
            self.assertIn(r.status_code, (401, 403))
            setter.assert_not_called()
            r = client_as(app, "hr_manager").post("/hr/settings/ai-hr-goals", json={"enabled": False})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(setter.call_args.args[2], False)
            self.assertTrue(fake.queries("insert into audit_log"))


# ---------------------------------------------------------------------------
# S-4.5 (Part A side)
# ---------------------------------------------------------------------------
class S45_AiStoreResidency(unittest.TestCase):
    def test_sqlite_store_tables_are_registered_for_export_erasure_and_retention(self):
        for table in ("sessions", "analytics", "debug_logs", "latency_logs", "anonymous_profiles"):
            self.assertIn(table, gdpr_service.COVERAGE)
            self.assertEqual(gdpr_service.COVERAGE[table][0], "delete")
        import retention_service
        aged = {p["table"] for p in retention_service.SQLITE_POLICIES}
        self.assertTrue({"analytics", "debug_logs", "latency_logs", "sessions", "anonymous_profiles"} <= aged)

    def test_erasure_report_includes_the_ai_store(self):
        app = get_app()
        fake, p = patch_mysql(app)
        with p, app.app_context():
            report = gdpr_service.erase_user_data("ghost", actor="root", dry_run=True)
        self.assertIn("ai_store", report)


if __name__ == "__main__":
    unittest.main()
