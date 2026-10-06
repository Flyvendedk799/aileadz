"""Notifications name people, resolve when decided, reach the manager and skip the actor."""

import unittest

import notification_service as ns
import order_service as svc
from tests.sqlite_platform import PlatformDB, make_app, client_as, render_patches


class NotificationPeopleTests(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.app = make_app(self.db)
        p = render_patches()
        p.start()
        self.addCleanup(p.stop)
        d = self.db
        d.execute("INSERT INTO companies (id,company_name) VALUES (7,'Firma')")
        d.execute("INSERT INTO users (id,username,email) VALUES (1,'anna','a@f.dk'),(2,'lise','l@f.dk'),(3,'mads','m@f.dk'),(4,'bo','b@f.dk')")
        d.execute("INSERT INTO company_users (company_id,user_id,username,full_name,email,role,status,department,manager_user_id) VALUES "
                  "(7,1,'anna','Anna Berg','a@f.dk','employee','active','Salg',3),"
                  "(7,2,'lise','Lise Hr','l@f.dk','hr_manager','active','HR',NULL),"
                  "(7,3,'mads','Mads Chef','m@f.dk','department_head','active','Salg',NULL),"
                  "(7,4,'bo','Bo Jensen','b@f.dk','employee','active','Drift',NULL)")
        self.addCleanup(d.raw.close)
        self.hr = svc.OrderContext(company_id=7, user_id=2, username="lise", company_role="hr_manager", department="HR")

    def order(self, user_id=1, username="anna", dept="Salg"):
        ctx = svc.OrderContext(company_id=7, user_id=user_id, username=username, company_role="employee", department=dept)
        with self.app.app_context():
            return svc.create_order(ctx, product_handle="x", product_title="Excel", price=100)

    def cards(self, user):
        return self.db.query("SELECT title, message, is_urgent, \"read\" AS r FROM notifications WHERE user_id = %s ORDER BY id", (user,))

    def test_approval_card_names_the_requester_by_full_name(self):
        self.order()
        msg = self.cards("lise")[0]["message"]
        self.assertIn("bestilt af Anna Berg", msg)
        self.assertNotIn("anna", msg)

    def test_the_requesters_manager_and_department_head_get_the_card_once(self):
        self.order()                               # mads is both anna's manager and head of Salg
        self.assertEqual(len(self.cards("mads")), 1)
        self.assertEqual(len(self.cards("lise")), 1)
        self.assertEqual(self.cards("anna"), [])    # never about your own order

    def test_a_department_head_of_the_department_is_told_without_being_the_manager(self):
        self.db.execute("UPDATE company_users SET manager_user_id = NULL WHERE user_id = 1")
        self.order()
        self.assertEqual(len(self.cards("mads")), 1)
        self.order(user_id=4, username="bo", dept="Drift")      # other department: mads is not told
        self.assertEqual(len(self.cards("mads")), 1)

    def test_deciding_the_approval_closes_the_card_for_everyone(self):
        self.order()
        before = [self.cards(u)[0] for u in ("lise", "mads")]
        self.assertTrue(all(c["is_urgent"] == 1 and c["r"] == 0 for c in before))
        with self.app.app_context():
            aid = self.db.one("SELECT id FROM order_approvals")["id"]
            self.assertTrue(svc.decide_approval(self.hr, aid, "approved")["success"])
        for u in ("lise", "mads"):
            card = [c for c in self.cards(u) if c["title"] == "Ny bestilling afventer godkendelse"][0]
            self.assertEqual((card["is_urgent"], card["r"]), (0, 1))

    def test_actor_user_id_skips_that_recipient(self):
        cur = self.db.connection.cursor()
        self.assertIsNone(ns.notify_user(cur, title="T", user_id=2, company_id=7, actor_user_id=2))
        self.assertTrue(ns.notify_user(cur, title="T", user_id=2, company_id=7, actor_user_id=1))
        n = ns.notify_roles(cur, 7, ns.HR_ROLES, title="Alle", actor_user_id=2)
        self.assertEqual(n, 0)                      # lise is the only HR person and the actor

    def test_hr_order_page_shows_the_full_name(self):
        out = self.order()
        hr = client_as(self.app, user="lise", user_id=2, company_id=7, company_role="hr_manager")
        html = hr.get("/hr/order/%s/details" % out["order_id"]).get_data(as_text=True)
        self.assertIn("Anna Berg", html)

    def test_hr_floating_button_does_not_cover_table_columns_css(self):
        css = open("static/futurematch/assets/fm-pages.css", encoding="utf-8").read()
        self.assertIn(".fm-wrap:has(.fm-aip) .fm-table td:last-child{padding-right:76px}", css)


if __name__ == "__main__":
    unittest.main()
