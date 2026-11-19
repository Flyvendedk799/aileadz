"""One wording for "completed", "in progress" and "still open" in every query.

``course_orders.completion_status`` is NULL unless the order is completed, so the
older tests (``completion_status != 'completed'``, ``= 'in_progress'``,
``NOT IN ('completed', 'cancelled')``) matched nothing and several HR counts were
always zero. Queries now carry the text of ``order_lifecycle.sql_completed`` /
``sql_in_progress`` / ``sql_still_open``; this test keeps it that way.
"""
import glob
import os
import re

import order_lifecycle as lc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The writer and the one-time migration name the column on purpose.
WRITERS = {"order_service.py", "schema_registry.py", "enterprise_tables.py", "order_lifecycle.py"}


def _sources():
    for path in glob.glob(os.path.join(ROOT, "**", "*.py"), recursive=True):
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        if rel.startswith(("tests/", "sandbox/", "migrations/", "scripts/")) or os.path.basename(rel) in WRITERS:
            continue
        yield rel, open(path, encoding="utf-8", errors="ignore").read()


def test_no_query_tests_the_completion_column_on_its_own():
    bad = []
    for rel, text in _sources():
        stripped = re.sub(r"COALESCE\((?:\w+\.)?completion_status, ''\) (?:=|<>) 'completed'", "", text)
        for pattern in (r"completion_status\s*(?:=|!=|<>)\s*'(?:completed|in_progress)'",
                        r"completion_status\s+(?:NOT\s+)?IN\s*\("):
            for m in re.finditer(pattern, stripped):
                bad.append("%s: %s" % (rel, m.group(0)))
    assert bad == [], "Use order_lifecycle.sql_completed / sql_in_progress / sql_still_open: %s" % bad


def test_the_fragments_handle_the_empty_column():
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE course_orders (id INTEGER, status TEXT, completion_status TEXT)")
    rows = [(1, "pending_approval", None), (2, "approved", None), (3, "booked", None), (4, "completed", "completed"),
            (5, "rejected", None), (6, "cancelled", None), (7, "booked", "completed"), (8, "completed", None)]
    db.executemany("INSERT INTO course_orders VALUES (?,?,?)", rows)

    def ids(fragment):
        return [r[0] for r in db.execute("SELECT id FROM course_orders co WHERE " + fragment + " ORDER BY id")]
    assert ids(lc.sql_completed("co")) == [4, 7, 8]
    assert ids(lc.sql_in_progress("co")) == [2, 3]
    assert ids(lc.sql_still_open("co")) == [1, 2, 3]
    assert ids(lc.sql_completed()) == [4, 7, 8]


def test_queries_use_exactly_the_canonical_text():
    canon = {lc.sql_completed(a) for a in ("", "co", "o")}
    seen = 0
    for _rel, text in _sources():
        for m in re.finditer(r"\((?:\w+\.)?status = 'completed' OR COALESCE\((?:\w+\.)?completion_status, ''\) = 'completed'\)", text):
            seen += 1
            assert m.group(0) in canon, m.group(0)
    assert seen > 40


def test_one_reading_of_where_an_order_stands_for_every_role():
    cases = {
        ("pending_approval", ()): ("approver", "lederen eller HR"),
        ("approved", ()): ("booker", "HR eller udbyderen"),
        ("booked", ()): ("course", None),
        ("booked", ("pending_change",)): ("change", "HR eller udbyderen"),
        ("booked", ("taken_place",)): ("learner", "deltageren"),
        ("booked", ("taken_place", "attendance_reported")): ("verifier", "HR eller udbyderen"),
        ("completed", ("outcome_pending",)): ("outcome", "nærmeste leder"),
        ("completed", ()): ("done", None),
        ("rejected", ()): ("rejected", None),
        ("cancelled", ()): ("cancelled", None),
        ("confirmed", ()): ("course", None),   # a legacy status reads as booked
    }
    for (status, flags), (stage, who) in cases.items():
        state = lc.case_state(status, **{f: True for f in flags})
        assert (state["stage"], state["waiting_on_label"]) == (stage, who), (status, flags)
        assert state["next_step"] and state["status_label"]
    # An open change request outranks everything else on a booked order: the booking stands until it is answered.
    assert lc.case_state("booked", pending_change=True, taken_place=True, attendance_reported=True)["stage"] == "change"
