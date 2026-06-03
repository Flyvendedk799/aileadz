#!/usr/bin/env python3
"""Exercise the AI harness end-to-end against the sandbox DB.

Drives /app1/ask with natural-language queries, classifies the streamed SSE
events, and reads the agent's tool-call debug log to surface tool errors.

Requires OPENAI_API_KEY (export it or put it in .env.sandbox) and a running
sandbox DB (./sandbox.sh up && ./sandbox.sh init).

  python test_ai.py            # run the full suite
  python test_ai.py "a query"  # run a single ad-hoc query
"""
import os
import sys
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def load_env(path):
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and os.environ.get(k) in (None, ""):
            os.environ[k] = v


load_env(HERE / ".env.sandbox")
load_env(ROOT / ".env")
os.environ["AI_WARMUP_ON_IMPORT"] = "0"

import run  # noqa: E402

if not os.environ.get("OPENAI_API_KEY"):
    print("ERROR: OPENAI_API_KEY not set (export it or add it to .env.sandbox).")
    sys.exit(2)

app = run.create_app()


def parse_sse(body):
    events = []
    for part in body.split("\n\n"):
        line = next((l for l in part.split("\n") if l.startswith("data: ")), None)
        if not line:
            continue
        raw = line[6:].strip()
        if raw == "[DONE]":
            continue
        try:
            events.append(json.loads(raw))
        except Exception:
            pass
    return events


def reset_test_profile():
    """Make the suite IDEMPOTENT: the 'add skill' flow asserts a confirm-card
    (profile_confirm_request), which the agent only proposes when the skill is
    NOT already on the profile. Without this reset, the first run adds Python to
    the 'test' user and every later run sees a duplicate and answers differently
    (a profile_update, not a confirm card) -> a false failure. Clear what the
    suite adds so each run starts from a known state."""
    try:
        with app.app_context():
            from db_compat import refresh_flask_mysql_connection
            refresh_flask_mysql_connection(app.mysql)
            cur = app.mysql.connection.cursor()
            cur.execute("DELETE FROM user_skills WHERE username='test' AND LOWER(skill_name)='python'")
            app.mysql.connection.commit()
            cur.close()
    except Exception as e:
        print(f"[reset_test_profile] skipped: {e}")


def fresh_client():
    c = app.test_client()
    c.post("/login", data={"username": "test", "password": "test"})
    return c


def ask(client, query):
    r = client.post("/app1/ask", json={"query": query})
    body = r.get_data(as_text=True)
    evs = parse_sse(body)
    types = {}
    text = ""
    err = None
    for e in evs:
        t = e.get("type")
        types[t] = types.get(t, 0) + 1
        if t == "chunk":
            text += e.get("content", "")
        if t == "error":
            err = e.get("content")
    return {"http": r.status_code, "types": types, "text": text.strip(), "error": err}


CASES = [
    ("course search",          "Anbefal 3 kurser indenfor kommunikation",            ["course_cards", "product"]),
    ("course search (projekt)","Hvilke kurser har I om projektledelse?",             ["course_cards", "product"]),
    ("course details",         "Fortæl mig mere om det første kursus",               ["chunk", "course_cards", "product"]),
    ("category",               "Hvilke kategorier af kurser har I?",                 ["chunk"]),
    ("vendor",                 "Har I kurser fra Mannaz?",                           ["chunk", "course_cards", "product"]),
    ("add skill",              "Tilføj kompetencen Python paa avanceret niveau til min profil", ["profile_confirm_request"]),
    ("add education (form)",   "Jeg har en uddannelse fra CBS",                      ["ui_card", "profile_confirm_request"]),
    ("add experience",         "Jeg har erfaring som projektleder hos Nordi",        ["profile_confirm_request", "ui_card"]),
    ("get profile",            "Hvad staar der paa min profil lige nu?",             ["chunk"]),
    ("recommend for profile",  "Anbefal kurser ud fra min profil",                   ["course_cards", "product", "chunk"]),
    ("learning path",          "Lav en laeringssti til mig indenfor ledelse",        ["chunk", "course_cards", "product"]),
    ("skill gaps",             "Hvad skal jeg laere for at blive projektleder?",     ["chunk", "course_cards", "product"]),
]


def recent_tool_log(limit=200):
    rows = []
    try:
        with app.app_context():
            import MySQLdb
            cur = app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("SELECT event_type, details FROM debug_logs WHERE event_type IN ('tool_call','tool_error') ORDER BY id DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
            cur.close()
    except Exception as e:
        print("  (debug_logs unavailable:", e, ")")
    return rows


def run_query(label, query, expect_any):
    c = fresh_client()
    res = ask(c, query)
    ok = (res["http"] == 200) and (res["error"] is None) and any(t in res["types"] for t in expect_any)
    flag = "PASS" if ok else "FAIL"
    ev = ", ".join(f"{k}×{v}" for k, v in res["types"].items() if k)
    print(f"[{flag}] {label}")
    print(f"       q: {query}")
    print(f"       events: {ev or '(none)'}")
    if res["error"]:
        print(f"       ERROR EVENT: {res['error']}")
    if res["text"]:
        print(f"       text: {res['text'][:160]}")
    return ok, label


def main():
    print("Warming AI subsystems (RAG)…")
    with app.app_context():
        try:
            from ai_context import warm_ai_subsystems
            warm_ai_subsystems()
        except Exception as e:
            print("  warmup:", e)

    if len(sys.argv) > 1:
        c = fresh_client()
        res = ask(c, sys.argv[1])
        print(json.dumps(res, ensure_ascii=False, indent=2)[:3000])
        return

    reset_test_profile()  # idempotency: start each run from a known profile state
    print(f"\nRunning {len(CASES)} AI flows against the sandbox…\n")
    results = []
    for label, query, expect in CASES:
        try:
            results.append(run_query(label, query, expect))
        except Exception as e:
            print(f"[FAIL] {label}  -> exception: {e}")
            results.append((False, label))
        print()

    # Tool-call summary from the agent's own debug log.
    print("=" * 60)
    rows = recent_tool_log()
    tools_used, tool_errors = {}, []
    for r in rows:
        try:
            d = json.loads(r["details"]) if r.get("details") else {}
        except Exception:
            d = {}
        name = d.get("tool", "?")
        if r["event_type"] == "tool_call":
            tools_used[name] = tools_used.get(name, 0) + 1
        else:
            tool_errors.append((name, d.get("error", "")))
    print("Tools exercised:", ", ".join(f"{k}×{v}" for k, v in sorted(tools_used.items())) or "(none logged)")
    if tool_errors:
        print("\nTOOL ERRORS:")
        for name, err in tool_errors:
            print(f"  - {name}: {str(err)[:200]}")
    else:
        print("Tool errors: none")

    passed = sum(1 for ok, _ in results if ok)
    print("\n" + "=" * 60)
    print(f"RESULT: {passed}/{len(results)} flows passed")
    for ok, label in results:
        if not ok:
            print(f"  FAIL: {label}")


if __name__ == "__main__":
    main()
