"""Demo tenant for the sandbox: one company with enough real data to walk every journey.

``python run_sandbox.py demo`` (or ``./sandbox.sh demo``) runs ``seed_demo(app)``
after the schema step. It is idempotent: a second run changes nothing.

What it creates, all through the same services the app uses (so budgets, order
history, notifications and queued mail are what a real customer would have):

* a small course catalogue in ``sandbox/.demo/catalog.json`` (``.env.sandbox`` points
  ``CATALOG_SOURCE_FILE`` at it), including one course that has already taken
  place, so attendance and outcome can be exercised;
* people in "Sandbox A/S": ``hr`` (HR manager), ``leder`` (department head, Salg),
  ``ada`` and ``bo`` (employees, Salg) and ``medarbejder`` (employee, Udvikling),
  all with password ``test``; ``test`` stays platform admin and company admin;
* departments with budgets, one internal course, one learning path;
* the course provider "Demo Akademi" with a vendor-portal login
  (``udbyder@sandbox.example`` / ``test`` at ``/vendor/login``);
* one order in each state: awaiting approval, approved, booked, completed, rejected;
* talent: modules on, role frameworks, an assigned role, an open internal
  opportunity, assessments, one private and one shared development goal, and one
  HR-written goal shared with the employee.

Nothing here is used by the production app or the offline test suite.
"""
import datetime
import json
import os
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
CATALOG_FILE = HERE / ".demo" / "catalog.json"
COMPANY_SLUG = "sandbox"
PASSWORD = "test"
VENDOR = "Demo Akademi"

# (username, full name, company role, department, job title, manager username)
PEOPLE = (
    ("hr", "Hanne Rask", "hr_manager", "HR", "HR-chef", None),
    ("leder", "Lars Edelberg", "department_head", "Salg", "Salgschef", None),
    ("ada", "Ada Mikkelsen", "employee", "Salg", "Sælger", "leder"),
    ("bo", "Bo Thomsen", "employee", "Salg", "Sælger", "leder"),
    ("medarbejder", "Mette Dam", "employee", "Udvikling", "Udvikler", None),
)
DEPARTMENTS = (("Salg", 60000), ("Udvikling", 80000), ("HR", 20000))


def _date_label(day):
    months = ("januar", "februar", "marts", "april", "maj", "juni", "juli", "august",
              "september", "oktober", "november", "december")
    return "%d. %s %d" % (day.day, months[day.month - 1], day.year)


def catalog():
    """Five courses. Session dates are relative to today so the tenant never goes stale."""
    today = datetime.date.today()
    soon, later, held = today + datetime.timedelta(days=45), today + datetime.timedelta(days=120), today - datetime.timedelta(days=30)

    def course(pid, handle, title, tags, price, sessions, body):
        return {"id": pid, "handle": handle, "title": title, "vendor": VENDOR, "product_type": "Kursus",
                "tags": tags, "body_html": "<p>%s</p>" % body,
                "variants": [{"id": pid * 10 + i, "price": str(price), "option1": place, "option2": _date_label(day)}
                             for i, (place, day) in enumerate(sessions, start=1)]}

    return [
        course(101, "demo-salgstraening", "Salgstræning i praksis", "Salg, Forhandling, Kommunikation", 8500,
               [("København", soon), ("Aarhus", later)], "To dage med forhandling, behovsafdækning og opfølgning."),
        course(102, "demo-ledelse-grundforloeb", "Ledelse: grundforløb for nye ledere", "Ledelse, Kommunikation", 14500,
               [("København", soon), ("Online", later)], "Fra kollega til leder: samtaler, feedback og prioritering."),
        course(103, "demo-python-dataanalyse", "Python til dataanalyse", "Python, Dataanalyse, IT", 12000,
               [("Online", soon)], "Pandas, visualisering og automatisering af rapporter."),
        course(104, "demo-projektledelse", "Projektledelse med PRINCE2 Foundation", "Projektledelse, Certificering", 16500,
               [("Odense", later)], "Metode, roller og eksamen i PRINCE2 Foundation."),
        course(105, "demo-praesentationsteknik", "Præsentationsteknik", "Kommunikation, Salg", 6500,
               [("København", held), ("København", later)], "Struktur, stemme og gennemslagskraft foran en forsamling."),
    ]


def write_catalog():
    CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_FILE.write_text(json.dumps(catalog(), ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        import catalog_service
        catalog_service._SIG_CACHE["value"] = None
        catalog_service.clear_catalog_cache()
    except Exception as e:
        print("  [demo] catalogue cache not cleared:", e)


def _one(cur, sql, args=()):
    cur.execute(sql, args)
    return cur.fetchone()


def _people(cur, conn, company_id):
    from werkzeug.security import generate_password_hash
    ids = {}
    for username, full_name, role, department, title, _manager in PEOPLE:
        row = _one(cur, "SELECT id FROM users WHERE username=%s", (username,))
        if not row:
            cur.execute("INSERT INTO users (username,password,email,credits,role) VALUES (%s,%s,%s,200,'user')",
                        (username, generate_password_hash(PASSWORD), "%s@sandbox.example" % username))
            row = {"id": cur.lastrowid}
        ids[username] = row["id"]
        if not _one(cur, "SELECT id FROM company_users WHERE company_id=%s AND user_id=%s", (company_id, row["id"])):
            cur.execute("INSERT INTO company_users (company_id,user_id,username,full_name,email,role,department,job_title,status) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active')",
                        (company_id, row["id"], username, full_name, "%s@sandbox.example" % username, role, department, title))
    for username, _n, _r, _d, _t, manager in PEOPLE:
        if manager:
            cur.execute("UPDATE company_users SET manager_user_id=%s WHERE company_id=%s AND user_id=%s AND manager_user_id IS NULL",
                        (ids[manager], company_id, ids[username]))
    ids["test"] = _one(cur, "SELECT id FROM users WHERE username='test'")["id"]
    cur.execute("UPDATE company_users SET username='test', full_name=COALESCE(full_name,'Test Admin'), "
                "department=COALESCE(department,'HR') WHERE company_id=%s AND user_id=%s", (company_id, ids["test"]))
    # The two bootstrap logins are stored in plaintext by init.sql; hash them so login works at once.
    for username in ("test", "medarbejder"):
        row = _one(cur, "SELECT password FROM users WHERE username=%s", (username,))
        if row and row["password"] == PASSWORD:
            cur.execute("UPDATE users SET password=%s WHERE username=%s", (generate_password_hash(PASSWORD), username))
    conn.commit()
    return ids


def _departments(cur, conn, company_id):
    year = datetime.date.today().year
    for name, budget in DEPARTMENTS:
        if not _one(cur, "SELECT id FROM company_departments WHERE company_id=%s AND department_name=%s", (company_id, name)):
            cur.execute("INSERT INTO company_departments (company_id,department_name) VALUES (%s,%s)", (company_id, name))
        if not _one(cur, "SELECT id FROM department_budgets WHERE company_id=%s AND department=%s AND fiscal_year=%s",
                    (company_id, name, year)):
            cur.execute("INSERT INTO department_budgets (company_id,department,annual_budget,spent,fiscal_year) VALUES (%s,%s,%s,0,%s)",
                        (company_id, name, budget, year))
    conn.commit()


def _vendor(cur, conn):
    """The course provider gets a portal login, so its orders can be handled from the vendor side."""
    import vendor_auth
    if not _one(cur, "SELECT id FROM vendors WHERE vendor_name=%s", (VENDOR,)):
        cur.execute("INSERT INTO vendors (vendor_name,slug,contact_email,password_hash,status,description) "
                    "VALUES (%s,'demo-akademi','udbyder@sandbox.example',%s,'active','Kurser i salg, ledelse og data.')",
                    (VENDOR, vendor_auth.hash_vendor_password(PASSWORD)))
    conn.commit()


def _ctx(company_id, ids, username):
    from order_service import OrderContext
    person = next(p for p in PEOPLE if p[0] == username)
    return OrderContext(company_id=company_id, user_id=ids[username], username=username,
                        company_role=person[2], department=person[3], source="web")


def _orders(cur, conn, company_id, ids):
    """One order per lifecycle state, created and moved by the real services."""
    import enrollment_service
    import order_fulfillment
    import order_service

    if _one(cur, "SELECT COUNT(*) AS n FROM course_orders WHERE company_id=%s", (company_id,))["n"]:
        return
    hr = _ctx(company_id, ids, "hr")
    products = {p["handle"]: p for p in catalog()}

    def order(username, handle, session=0):
        variant = products[handle]["variants"][session]
        result = enrollment_service.create_order(
            _ctx(company_id, ids, username), product_handle=handle, product_title=products[handle]["title"],
            variant_date=variant["option2"], variant_location=variant["option1"],
            user_email="%s@sandbox.example" % username, user_name=next(p[1] for p in PEOPLE if p[0] == username),
            extra={"session_id": str(variant["id"])})
        if not result.get("success"):
            raise RuntimeError("demo order %s/%s: %s" % (username, handle, result))
        return result["order_id"]

    def must(result, what):
        if not (result or {}).get("success"):
            raise RuntimeError("demo %s: %s" % (what, result))

    order("ada", "demo-ledelse-grundforloeb")                                    # awaiting approval
    approved = order("bo", "demo-salgstraening")
    must(order_service.set_status(hr, approved, "approved", note="Godkendt i demo-data"), "approve")
    booked = order("ada", "demo-salgstraening")
    must(order_service.set_status(hr, booked, "approved"), "approve")
    must(order_fulfillment.book(hr, booked, {"reference": "DEMO-2041", "instructions": "Mød 8.45 i receptionen."}), "book")
    # A held course: a session that has taken place cannot be ordered (by design), so
    # the order is for the coming session and HR books the earlier date as an agreed
    # deviation, exactly as the booking form allows. The order history says so.
    held = order("bo", "demo-praesentationsteknik", session=1)
    must(order_service.set_status(hr, held, "approved"), "approve")
    must(order_fulfillment.book(hr, held, {
        "reference": "DEMO-1987", "confirm_date_change": "yes",
        "start_at": products["demo-praesentationsteknik"]["variants"][0]["option2"]}), "book")
    must(order_fulfillment.report_completion(_ctx(company_id, ids, "bo"), held, note="Deltog begge dage."), "attendance")
    must(order_service.complete_order(hr, held, note="Deltagelse bekræftet"), "complete")
    rejected = order("medarbejder", "demo-projektledelse")
    must(order_service.set_status(hr, rejected, "rejected", reason="Afventer næste budgetår"), "reject")


def _learning(cur, conn, company_id, ids):
    import learning_path_service
    if not _one(cur, "SELECT id FROM company_courses WHERE company_id=%s AND title=%s", (company_id, "Velkommen til Sandbox A/S")):
        cur.execute("INSERT INTO company_courses (company_id,title,description,category,course_type,format,duration_hours,price,"
                    "is_mandatory,is_active,created_by) VALUES (%s,%s,%s,'Onboarding','internal','online',2,0,1,1,%s)",
                    (company_id, "Velkommen til Sandbox A/S", "Introduktion til virksomheden, systemer og sikkerhed.", ids["hr"]))
    if not _one(cur, "SELECT id FROM learning_paths WHERE company_id=%s AND path_name=%s", (company_id, "Ny i salg")):
        cur.execute("INSERT INTO learning_paths (company_id,path_name,path_category,difficulty_level,is_active) "
                    "VALUES (%s,'Ny i salg','Salg','begynder',1)", (company_id,))
        path_id = cur.lastrowid
        result = learning_path_service.save_steps(cur, company_id, path_id, [
            {"step_type": "info", "title": "Tal med din leder om dine første kunder"},
            {"step_type": "catalog", "course_handle": "demo-salgstraening", "title": "Salgstræning i praksis"},
            {"step_type": "catalog", "course_handle": "demo-praesentationsteknik", "title": "Præsentationsteknik"},
        ], actor_user_id=ids["hr"], ordering_mode="sequential")
        if not result.get("success"):
            raise RuntimeError("demo learning path: %s" % result)
    conn.commit()


def _talent(cur, conn, company_id, ids):
    import goal_service
    import talent_service as talent
    from app1.user_profile_db import ensure_tables
    ensure_tables()
    if _one(cur, "SELECT COUNT(*) AS n FROM talent_role_versions WHERE company_id=%s", (company_id,))["n"]:
        return
    talent.set_flags(cur, company_id, dict(roles_enabled=1, mobility_enabled=1, planning_enabled=1))

    def role(title, requirements):
        version = talent.save_role(cur, company_id, title, requirements)
        cur.execute("UPDATE talent_role_versions SET published=1 WHERE company_id=%s AND id=%s", (company_id, version))
        return version

    seller = role("Sælger", "Salg | 3\nForhandling | 3\nKommunikation | 3")
    lead = role("Teamleder, Salg", "Salg | 4\nLedelse | 3\nKommunikation | 4\nForhandling | 4")
    for person in ("ada", "bo"):
        talent.assign_role(cur, company_id, ids[person], seller)
    deadline = datetime.date.today() + datetime.timedelta(days=40)
    opportunity = talent.save_opportunity(cur, company_id, ids["hr"], dict(
        role_version_id=lead, kind="role", title="Teamleder til Salg Øst", department="Salg", location="København",
        description="Du får ansvaret for seks sælgere og den daglige prioritering.", deadline=str(deadline),
        team=[ids["leder"]]))
    cur.execute("UPDATE talent_opportunities SET status='published' WHERE company_id=%s AND id=%s", (company_id, opportunity))
    talent.assess(cur, company_id, ids["ada"], "Salg", 3, "self", ids["ada"], "demo-self-salg")
    talent.assess(cur, company_id, ids["ada"], "Kommunikation", 4, "self", ids["ada"], "demo-self-kom")
    talent.assess(cur, company_id, ids["ada"], "Salg", 4, "manager", ids["leder"], "demo-mgr-salg", "Stærk på nye kunder.")
    talent.assess(cur, company_id, ids["ada"], "Forhandling", 2, "manager", ids["leder"], "demo-mgr-forh")
    for title in ("Blive teamleder inden for to år", "Prøve kræfter med en anden branche"):
        cur.execute("INSERT INTO user_learning_goals (username,title,status) VALUES ('ada',%s,'aktiv')", (title,))
    shared = _one(cur, "SELECT id FROM user_learning_goals WHERE username='ada' AND title=%s", ("Blive teamleder inden for to år",))["id"]
    talent.link_goal(cur, company_id, ids["ada"], shared, "Ledelse | 3\nForhandling | 4",
                     str(datetime.date.today() + datetime.timedelta(days=90)),
                     [dict(kind="mentoring", title="Følg Lars til to kundemøder"),
                      dict(kind="course", title="Tag et lederkursus")], True)
    goal_service.share_own_goal(cur, company_id, ids["ada"], shared, True)
    cur.execute("INSERT INTO employee_goals (employee_id,company_id,goal_title,goal_description,status,shared_with_employee,shared_at,shared_by) "
                "VALUES (%s,%s,'Overtag to nøglekunder','Aftalt på udviklingssamtalen.','active',1,NOW(),%s)",
                (ids["ada"], company_id, ids["hr"]))
    conn.commit()


def seed_demo(app):
    """Fill the sandbox company. Safe to run again."""
    import MySQLdb.cursors
    write_catalog()
    if not os.environ.get("CATALOG_SOURCE_FILE"):
        print("  [demo] CATALOG_SOURCE_FILE is not set: the app will not read sandbox/.demo/catalog.json")
    with app.test_request_context():
        conn = app.mysql.connection
        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        try:
            company = _one(cur, "SELECT id FROM companies WHERE company_slug=%s", (COMPANY_SLUG,))
            if not company:
                print("  [demo] run `init` first: the sandbox company does not exist")
                return False
            company_id = company["id"]
            ids = _people(cur, conn, company_id)
            _departments(cur, conn, company_id)
            _vendor(cur, conn)
            _learning(cur, conn, company_id, ids)
            _orders(cur, conn, company_id, ids)
            _talent(cur, conn, company_id, ids)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    print("✓ Demo tenant ready: hr / leder / ada / bo / medarbejder (password: %s), admin: test, "
          "vendor: udbyder@sandbox.example at /vendor/login" % PASSWORD)
    return True
