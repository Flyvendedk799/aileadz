"""N-3.2: one notification system with per-user read state."""

import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

import notification_service as ns  # noqa: E402


class FakeDB:
    """Tiny in-memory stand-in for the three tables the service touches."""

    def __init__(self):
        self.users = {1: "ada", 2: "bo", 3: "cy"}
        self.members = [  # company_users
            {"company_id": 7, "user_id": 1, "role": "hr_manager", "status": "active"},
            {"company_id": 7, "user_id": 2, "role": "employee", "status": "active"},
            {"company_id": 7, "user_id": 3, "role": "company_admin", "status": "inactive"},
            {"company_id": 8, "user_id": 3, "role": "hr_manager", "status": "active"},
        ]
        self.rows = []  # notifications
        self._last = []
        self.rowcount = 0
        self.lastrowid = 0

    # cursor protocol ------------------------------------------------------
    def cursor(self, *a, **k):
        return self

    def close(self):
        pass

    def commit(self):
        pass

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self._last = []
        if s.startswith("SELECT username FROM users WHERE id"):
            u = self.users.get(params[0])
            self._last = [{"username": u}] if u else []
        elif s.startswith("SELECT id FROM users WHERE username"):
            for i, u in self.users.items():
                if u == params[0]:
                    self._last = [{"id": i}]
        elif "FROM company_users cu" in s:
            cid = params[0]
            roles = set(params[1:]) if len(params) > 1 else None
            for m in self.members:
                if m["company_id"] == cid and m["status"] == "active" and (roles is None or m["role"] in roles):
                    self._last.append({"user_id": m["user_id"], "username": self.users[m["user_id"]]})
        elif s.startswith("SELECT 1 FROM notifications WHERE user_id"):
            u, key = params[0], params[1]
            if any(r["user_id"] == u and r["dedupe_key"] == key for r in self.rows):
                self._last = [{"1": 1}]
        elif s.startswith("INSERT INTO notifications"):
            (user, cid, uid, sender, kind, title, msg, img, action, urgent, key) = params
            self.lastrowid += 1
            self.rows.append(dict(id=self.lastrowid, user_id=user, company_id=cid, recipient_user_id=uid,
                                  kind=kind, title=title, message=msg, action_url=action,
                                  is_urgent=urgent, dedupe_key=key, read=0))
        elif s.startswith("SELECT COUNT(*) AS c FROM notifications"):
            self._last = [{"c": sum(1 for r in self.rows if r["user_id"] == params[0] and not r["read"])}]
        elif s.startswith("UPDATE notifications SET `read` = 1"):
            n = 0
            for r in self.rows:
                if "WHERE id = %s" in s:
                    hit = r["id"] == params[0] and r["user_id"] == params[1]
                else:
                    hit = r["user_id"] == params[0] and not r["read"]
                if hit and not r["read"]:
                    r["read"] = 1
                    n += 1
            self.rowcount = n
        else:  # pragma: no cover
            raise AssertionError("unexpected SQL: " + s)

    def fetchone(self):
        return self._last[0] if self._last else None

    def fetchall(self):
        return list(self._last)


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()

    def test_notify_user_creates_one_row_with_action_url(self):
        ns.notify_user(self.db, title="Hej", message="m", user_id=2, company_id=7,
                       action_url="/min-laering")
        self.assertEqual(len(self.db.rows), 1)
        r = self.db.rows[0]
        self.assertEqual((r["user_id"], r["recipient_user_id"], r["action_url"]),
                         ("bo", 2, "/min-laering"))

    def test_role_fanout_only_hits_matching_active_members(self):
        n = ns.insert_company_notification(self.db, 7, target_roles=["hr_manager", "company_admin"],
                                           title="t", message="m")
        self.assertEqual(n, 1)  # ada only; cy is inactive, bo is an employee
        self.assertEqual([r["user_id"] for r in self.db.rows], ["ada"])

    def test_company_broadcast_reaches_all_active_members_and_not_other_tenants(self):
        n = ns.notify_company(self.db, 7, title="Nyhed", message="m", dedupe_key=None)
        self.assertEqual(n, 2)
        self.assertEqual({r["user_id"] for r in self.db.rows}, {"ada", "bo"})

    def test_read_state_is_per_user(self):
        ns.notify_company(self.db, 7, title="Nyhed", message="m", dedupe_key=None)
        ada = next(r for r in self.db.rows if r["user_id"] == "ada")
        ns.mark_read(self.db, "ada", ada["id"])
        self.assertEqual(ns.unread_count(self.db, "ada"), 0)
        self.assertEqual(ns.unread_count(self.db, "bo"), 1)  # broadcast NOT read for everyone

    def test_cannot_mark_someone_elses_notification(self):
        ns.notify_user(self.db, title="x", username="bo", user_id=2, company_id=7)
        nid = self.db.rows[0]["id"]
        ns.mark_read(self.db, "ada", nid)
        self.assertEqual(ns.unread_count(self.db, "bo"), 1)

    def test_dedupe_key_blocks_repeat_per_recipient(self):
        for _ in range(3):
            ns.insert_company_notification(self.db, 7, target_roles=["hr_manager"], title="t",
                                           dedupe_key="compliance:req-1")
        self.assertEqual(len(self.db.rows), 1)

    def test_mark_all_read(self):
        for i in range(3):
            ns.notify_user(self.db, title=str(i), username="bo", user_id=2, company_id=7)
        self.assertEqual(ns.unread_count(self.db, "bo"), 3)
        ns.mark_read(self.db, "bo", None)
        self.assertEqual(ns.unread_count(self.db, "bo"), 0)

    def test_failure_never_raises(self):
        class Boom:
            def execute(self, *a, **k):
                raise RuntimeError("db down")
        self.assertIsNone(ns.notify_user(Boom(), title="t", username="x"))
        self.assertEqual(ns.role_recipients(Boom(), 7, ["hr_manager"]), [])


class EmailPreferenceTests(unittest.TestCase):
    def test_non_transactional_mail_respects_opt_out(self):
        import email_service
        from flask import Flask
        app = Flask(__name__)
        app.config.update(MAIL_SERVER="smtp.x", MAIL_DEFAULT_SENDER="a@b.dk")
        with app.app_context(), \
                mock.patch.object(email_service, "recipient_opted_out", return_value=True), \
                mock.patch.object(email_service, "_record_email_attempt") as rec:
            ok = email_service.send_branded_email("u@x.dk", "s", "manager_weekly_digest", {},
                                                  pending_approvals=1, budget_utilization="1%",
                                                  inactive_employees=0, skill_gaps=0,
                                                  critical_skill_gaps=0)
        self.assertFalse(ok)
        self.assertEqual(rec.call_args.args[2], "skipped_opt_out")

    def test_transactional_mail_ignores_opt_out(self):
        import email_service
        from flask import Flask
        app = Flask(__name__)
        with app.app_context(), \
                mock.patch.object(email_service, "recipient_opted_out", return_value=True) as opt, \
                mock.patch.object(email_service, "_record_email_attempt") as rec:
            email_service.send_branded_email("u@x.dk", "s", "order_approved", {},
                                             product_title="K", order_id="1")
        opt.assert_not_called()
        self.assertEqual(rec.call_args.args[2], "skipped_no_backend")  # no SMTP in test


if __name__ == "__main__":
    unittest.main()
