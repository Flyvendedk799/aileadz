#!/usr/bin/env python3
"""Run / test the Futurematch app against the local Dockerized MySQL sandbox.

Usage:
  python run_sandbox.py init    # create every table + seed a test company
  python run_sandbox.py demo    # init + a full demo tenant (people, catalogue, orders, talent)
  python run_sandbox.py run     # run the dev server (http://127.0.0.1:5001)
  python run_sandbox.py smoke   # boot, log in as 'test', exercise profile + chat

This never opens the production SSH tunnel — it talks directly to the sandbox DB
configured in .env.sandbox.
"""
import os
import sys
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
os.chdir(ROOT)


def load_env(path):
    """Minimal .env loader. Does NOT override values already in the environment."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if key and os.environ.get(key) in (None, ""):
            os.environ[key] = val


# Shell exports win, then .env.sandbox, then a root .env if present.
load_env(HERE / ".env.sandbox")
load_env(ROOT / ".env")

import run  # noqa: E402  (imported after env is set)


def _dict_cursor(app):
    import MySQLdb
    return app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)


def ensure_schema(app):
    """Create every table the app manages (enterprise + branding + profile)."""
    with app.app_context():
        try:
            from enterprise_tables import ensure_enterprise_tables
            ensure_enterprise_tables(app)
        except Exception as e:
            print("  [enterprise tables]", e)
        try:
            from branding_service import ensure_branding_schema, migrate_legacy_branding_data
            ensure_branding_schema(app)
            migrate_legacy_branding_data(app)
        except Exception as e:
            print("  [branding schema]", e)
        try:
            from app1.user_profile_db import ensure_tables
            ensure_tables()
        except Exception as e:
            print("  [profile tables]", e)
    print("✓ Schema ensured.")


def seed(app):
    """Best-effort: hash the seeded passwords and link 'test' to a sandbox company as company_admin."""
    with app.app_context():
        # init.sql seeds plaintext passwords and only hashed ones authenticate. The boot-time
        # rehash is skipped under TESTING (smoke, ai_eval), so do it here or every login fails.
        try:
            from auth import rehash_plaintext_passwords
            n = rehash_plaintext_passwords(app.mysql.connection)
            if n:
                print("✓ Hashed %d seeded password(s)." % n)
        except Exception as e:
            print("  [seed] password hashing skipped:", e)
        try:
            cur = _dict_cursor(app)
            cur.execute("SELECT id FROM users WHERE username=%s", ("test",))
            u = cur.fetchone()
            if not u:
                print("  [seed] no 'test' user — is the DB initialised?")
                return
            uid = u["id"]
            cur.execute("SELECT id FROM companies WHERE company_slug=%s", ("sandbox",))
            row = cur.fetchone()
            if row:
                cid = row["id"]
            else:
                cur.execute(
                    "INSERT INTO companies (company_name, company_slug, country) VALUES (%s,%s,%s)",
                    ("Sandbox A/S", "sandbox", "Denmark"),
                )
                app.mysql.connection.commit()
                cid = cur.lastrowid
            cur.execute("SELECT id FROM company_users WHERE company_id=%s AND user_id=%s", (cid, uid))
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO company_users (company_id, user_id, role, status) VALUES (%s,%s,'company_admin','active')",
                    (cid, uid),
                )
                app.mysql.connection.commit()
            # The talent modules are opt-in per company; the eval's HR talent cases need them.
            try:
                import schema_registry
                import talent_service
                cur.execute(schema_registry.ddl("talent_settings"))
                talent_service.set_flags(cur, cid, {"roles_enabled": 1, "mobility_enabled": 1, "planning_enabled": 1})
                app.mysql.connection.commit()
            except Exception as e:
                print("  [seed] talent modules not enabled:", e)
            cur.close()
            print("✓ Seeded company 'Sandbox A/S' (slug=sandbox); 'test' is company_admin.")
        except Exception as e:
            print("  [seed] skipped:", e)


def smoke(app):
    """Boot, log in as the test user, and exercise the AI profile + chat flow."""
    app.config.update(TESTING=True)
    c = app.test_client()

    r = c.post("/login", data={"username": "test", "password": "test"}, follow_redirects=False)
    print(f"  login              -> {r.status_code}  ({'OK' if r.status_code in (302, 200) else 'FAIL'})")

    r = c.get("/api/profile/full")
    print(f"  GET /api/profile/full -> {r.status_code}")

    r = c.post("/api/profile/skills", json={"skill_name": "Python", "skill_level": "avanceret"})
    print(f"  POST add skill     -> {r.status_code}  {r.get_json() if r.is_json else ''}")

    r = c.post("/app1/confirm_profile_update",
               json={"action": "add_education", "data": {"degree": "HA", "institution": "CBS", "year_completed": "2020"}})
    print(f"  add education      -> {r.status_code}  {r.get_json() if r.is_json else ''}")

    if os.environ.get("OPENAI_API_KEY"):
        print("  chat (/app1/ask)   -> calling the real AI engine (streaming)…")
        r = c.post("/app1/ask", json={"query": "Anbefal 3 kurser indenfor kommunikation"})
        body = r.get_data(as_text=True)
        has_cards = "course_cards" in body or "premium-course" in body or '"chunk"' in body
        print(f"                        {r.status_code}  bytes={len(body)}  got_ai_output={has_cards}")
    else:
        print("  chat (/app1/ask)   -> SKIPPED (no OPENAI_API_KEY set)")
    print("✓ Smoke test complete.")


def run_server(app):
    port = int(os.environ.get("SANDBOX_PORT", "5001"))
    print(f"\n  Futurematch sandbox running at http://127.0.0.1:{port}")
    print("  Log in at /login as  test / test  (admin)  or  medarbejder / test  (employee)\n")
    app.run(host="127.0.0.1", port=port, debug=True, use_reloader=False)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    print(f"Sandbox DB: {os.environ.get('MYSQL_USER')}@{os.environ.get('MYSQL_HOST')}:{os.environ.get('MYSQL_PORT')}/{os.environ.get('MYSQL_DB')}")
    app = run.create_app()
    if cmd == "init":
        ensure_schema(app)
        seed(app)
    elif cmd == "demo":
        ensure_schema(app)
        seed(app)
        import demo_seed
        if not demo_seed.seed_demo(app):
            sys.exit(1)
    elif cmd == "smoke":
        ensure_schema(app)
        seed(app)
        smoke(app)
    elif cmd == "run":
        ensure_schema(app)
        run_server(app)
    else:
        print(f"Unknown command: {cmd}. Use: init | demo | run | smoke")
        sys.exit(2)


if __name__ == "__main__":
    main()
