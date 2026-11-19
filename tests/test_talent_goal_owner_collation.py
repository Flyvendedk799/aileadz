"""Goal ownership in talent joins is compared in Python, not with a cross-table SQL `=`.

MySQL raises error 1267 when `users.username` and `user_learning_goals.username` carry
different utf8mb4 collations (seen on the sandbox: /udvikling and /hr/talent answered 500).
"""
import talent_service


class FakeCursor:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def execute(self, sql, params=()):
        self.sql.append(sql)

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


def test_no_cross_table_username_equality_in_sql(monkeypatch):
    monkeypatch.setattr(talent_service, "_mark_course_steps", lambda *a: None)
    cur = FakeCursor([
        {"id": 1, "goal_id": 5, "title": "Mit mål", "status": "aktiv", "actions_json": "[]",
         "goal_username": "Ada", "link_username": "ada"},
        {"id": 2, "goal_id": 6, "title": "Ikke mit", "status": "aktiv", "actions_json": "[]",
         "goal_username": "bob", "link_username": "ada"},
    ])
    links = talent_service.goals(cur, 7, 3)
    assert [link["id"] for link in links] == [1]
    assert "goal_username" not in links[0] and "link_username" not in links[0]
    assert "g.username=u.username" not in cur.sql[0].replace(" ", "")


def test_complete_course_steps_skips_a_goal_of_someone_else():
    cur = FakeCursor([
        {"id": 1, "title": "Mit mål", "actions_json": '[{"order_id": "o1"}]', "goal_username": "ada", "link_username": "ada"},
        {"id": 2, "title": "Fremmed", "actions_json": '[{"order_id": "o1"}]', "goal_username": "bob", "link_username": "ada"},
    ])
    assert talent_service.complete_course_steps(cur, 7, 3, "o1") == ["Mit mål"]
