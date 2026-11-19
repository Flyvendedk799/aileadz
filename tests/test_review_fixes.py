"""Regressions found by the pre-merge review of the tie-together branch."""
import json

import pytest

import company_documents
import competency
import grounding
import rate_limit
import talent_service


class _Rows:
    def __init__(self, rows):
        self._rows, self.sql = rows, []

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def test_newest_role_version_is_by_number_not_by_title_order():
    # roles() orders by title, so a retitled v2 arrives after v1.
    rows = [{"id": "a1", "role_id": "r1", "version": 1, "title": "Analyst", "requirements_json": "[]"},
            {"id": "a2", "role_id": "r1", "version": 2, "title": "Senior Analyst", "requirements_json": "[]"}]
    latest = talent_service.latest_roles(_Rows(rows), 7)
    assert [(r["id"], r["title"]) for r in latest] == [("a2", "Senior Analyst")]


@pytest.mark.parametrize("skill,title,hit", [
    ("Java", "JavaScript for begyndere", False),
    ("Excel", "Excellent kundeservice", False),
    ("Java", "Java for begyndere", True),
    ("Ledelse", "Ledelsen i praksis", True),
    ("Excel", "Avanceret Excel: pivot", True),
])
def test_a_gap_matches_a_course_only_on_the_whole_word(skill, title, hit):
    assert bool(competency.gaps_matched_by([{"skill": skill}], title)) is hit


def test_a_write_that_succeeded_is_not_called_unfinished(monkeypatch):
    pending = json.dumps({"needs_confirmation": True})
    done = json.dumps({"success": True, "learner_reminders": 3})
    monkeypatch.setattr(grounding, "_writes", lambda name: name == "send_deadline_reminders")
    answer = "Påmindelserne er nu sendt."
    assert grounding.pending_confirmation_note(answer, [done, pending], ["send_deadline_reminders", "assign_course"]) == ""
    # Only a read succeeded next to the card: the claim is still unfounded.
    assert grounding.pending_confirmation_note(answer, [done, pending], ["get_team_training_status", "assign_course"])
    assert grounding.pending_confirmation_note(answer, [pending])


def test_a_document_that_search_could_not_reach_is_refused(monkeypatch):
    class Cur(_Rows):
        def __init__(self):
            super().__init__([])
            self.answers = [{"n": 1}, {"n": company_documents.MAX_CHUNKS_SCANNED - 1}]

        def fetchone(self):
            return self.answers.pop(0)

    cur = Cur()
    monkeypatch.setattr(company_documents, "extract_text", lambda name, data: "tekst")
    monkeypatch.setattr(company_documents, "split_passages", lambda text: ["a", "b"])
    with pytest.raises(company_documents.DocumentError):
        company_documents.add_document(cur, 7, 1, "Håndbog", "h.txt", b"x")
    assert not any("INSERT" in q for q in cur.sql)


def test_anonymous_visitors_behind_one_address_get_a_wider_budget(monkeypatch):
    monkeypatch.setenv("AI_RATE_TURNS_5MIN", "2")
    monkeypatch.setenv("AI_RATE_TURNS_HOUR", "0")
    scale = rate_limit.ANON_SHARED_ADDRESS_SCALE
    assert scale > 1
    allowed = sum(rate_limit.ai_turn_allowed("anon:review-test", now=1000.0, scale=scale) for _ in range(2 * scale + 3))
    assert allowed == 2 * scale
    assert sum(rate_limit.ai_turn_allowed("user:review-test", now=1000.0) for _ in range(5)) == 2


def test_gunicorn_lets_an_open_stream_finish():
    cfg = {}
    exec(open("deploy/gunicorn.conf.py", encoding="utf-8").read(), cfg)
    assert cfg["graceful_timeout"] >= cfg["timeout"]
    assert cfg["max_requests"] == 0, "no recycling by default with one worker"
