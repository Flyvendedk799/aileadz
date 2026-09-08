"""Completion contracts for the learner AI workspace.

Offline: all database and model seams are mocked.
"""
from unittest import mock

import pytest
from flask import Flask

import api


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(api.api_bp)
    app.config.update(TESTING=True)
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user"] = "alice"
            sess["sid"] = "sid-alice"
        yield c


def _empty_profile():
    return {
        "headline": "", "bio": "", "goals": "", "target_role": "Data Analyst",
        "skills": [], "experience": [], "education": [], "certifications": [],
        "languages": [], "portfolio_links": [], "completed_courses": [],
        "learning_paths": [], "learning_goals": [],
    }


def test_mindmap_api_includes_complete_profile_graph(client):
    profile = _empty_profile()
    profile.update({
        "skills": [{"id": 11, "name": "Python", "level": "mellem", "category": "Teknologi"}],
        "portfolio_links": [{"id": 12, "label": "GitHub", "url": "https://github.com/a", "kind": "github"}],
        "completed_courses": [{"title": "Python 101", "handle": "python-101", "vendor": "Acme"}],
        "learning_paths": [{"id": 13, "title": "Data-sti", "goal": "Analytiker", "steps": [{}, {}], "status": "aktiv", "source": "ai"}],
    })
    memories = [{"id": 14, "label": "Foretrækker praksis", "category": "praeference",
                 "source": "user", "confidence": .9, "used_count": 1}]
    with mock.patch("app1.user_profile_db.ensure_tables"), \
         mock.patch("app1.user_profile_db.get_full_profile", return_value=profile), \
         mock.patch("app1.user_profile_db.get_memories", return_value=memories), \
         mock.patch("app1.user_profile_db.profile_completeness", return_value={"pct": 75}), \
         mock.patch("app1.user_profile_db.load_conversation_summary", return_value=""), \
         mock.patch("competency.compute_skill_gaps", return_value=[]):
        response = client.get("/api/profile/mindmap")

    assert response.status_code == 200
    data = response.get_json()
    branches = {n["id"] for n in data["nodes"] if n["type"] == "branch"}
    assert {"kompetencer", "portfolio", "kurser", "laeringsstier", "hukommelse"} <= branches
    skill = next(n for n in data["nodes"] if n["id"] == "skill:11")
    assert skill["meta"]["entity_type"] == "skill"
    assert skill["meta"]["profile_section"] == "skills"
    assert any(n["id"] == "path:13" and n["meta"]["step_count"] == 2 for n in data["nodes"])


def test_cv_job_ids_are_bound_to_authenticated_user():
    assert api._cv_job_key("alice", "same-browser-id") != api._cv_job_key("bob", "same-browser-id")
    assert api._cv_job_key("alice", "same-browser-id") == api._cv_job_key("alice", "same-browser-id")


def test_cv_apply_preserves_summary_and_experience_fields(client):
    before = _empty_profile()
    after = _empty_profile()
    after["bio"] = "Grounded summary"
    after["skills"] = [{"id": 1, "name": "Python", "level": "mellem"}]
    add_experience = mock.Mock(return_value=22)
    update_summary = mock.Mock(return_value=True)
    with mock.patch("app1.user_profile_db.ensure_tables"), \
         mock.patch("app1.user_profile_db.get_full_profile", side_effect=[before, after]), \
         mock.patch("app1.user_profile_db.add_experience", add_experience), \
         mock.patch("app1.user_profile_db.update_profile_summary", update_summary), \
         mock.patch("app1.user_profile_db.add_skill"), \
         mock.patch("app1.user_profile_db.add_education"), \
         mock.patch("app1.user_profile_db.add_certification"), \
         mock.patch("app1.user_profile_db.add_language"), \
         mock.patch("app1.user_profile_db.update_skill_level"), \
         mock.patch("app1.user_profile_db.update_experience"), \
         mock.patch("app1.user_profile_db.update_education"), \
         mock.patch("app1.user_profile_db.update_certification"), \
         mock.patch("app1.user_profile_db.update_language_level"), \
         mock.patch("competency.compute_skill_gaps", return_value=[]):
        response = client.post("/api/cv/apply", json={
            "summary": "Grounded summary",
            "conflict_mode": "merge",
            "accepted": [{
                "type": "experience", "title": "Analyst", "company": "Acme",
                "start_year": 2020, "end_year": 2024, "is_current": False,
                "description": "Built verified dashboards.",
            }],
        })

    assert response.status_code == 200
    update_summary.assert_called_once_with("alice", bio="Grounded summary")
    kwargs = add_experience.call_args.kwargs
    assert kwargs["start_year"] == 2020
    assert kwargs["end_year"] == 2024
    assert kwargs["description"] == "Built verified dashboards."
    assert response.get_json()["outcomes"]["created"] == 2


def test_cv_coach_is_non_destructive_and_returns_diff_data(client):
    profile = _empty_profile()
    suggestion = {
        "suggestion": "Dataanalytiker med dokumenteret dashboard-erfaring.",
        "rationale": "Mere konkret.",
        "missing_evidence": ["Hvor mange dashboards?"],
        "inferred": False,
    }
    with mock.patch("app1.user_profile_db.ensure_tables"), \
         mock.patch("app1.user_profile_db.get_full_profile", return_value=profile), \
         mock.patch("competency.compute_skill_gaps", return_value=[]), \
         mock.patch("cv_ingest.improve_cv_section", return_value=suggestion) as improve:
        response = client.post("/api/cv/improve", json={
            "section": "summary", "content": "Dataanalytiker med erfaring.",
        })

    assert response.status_code == 200
    assert response.get_json()["suggestion"] == suggestion["suggestion"]
    improve.assert_called_once()


def test_cv_template_has_honest_accessible_fallback_and_no_sample_profile():
    source = open("templates/fm/cv_upload.html", encoding="utf-8").read()
    assert "runMock" not in source
    assert "const SAMPLE" not in source
    assert "cv-server-review" in source
    assert 'id="reviewList"' in source
    assert 'aria-live="polite"' in source
    assert "prefers-reduced-motion" in source
    assert "/api/cv/improve" in source


def test_cross_surface_contract_is_intent_preserving():
    events = open("app1/sse_events.py", encoding="utf-8").read()
    tools = open("app1/tools.py", encoding="utf-8").read()
    chat = open("static/futurematch/assets/chat.js", encoding="utf-8").read()
    mindmap = open("templates/fm/mind_map.html", encoding="utf-8").read()
    assert '"open_advisor"' in events and '"open_advisor"' in tools
    assert 'params.get("intent")' in chat
    assert "node_id" in tools and "data-node" in chat
    assert "_goAdvisor" in mindmap and "mindmap_gap_cta" in mindmap


def test_learner_telemetry_rejects_unknown_events(client):
    assert client.post("/api/learner/events", json={"event": "raw_arbitrary_event"}).status_code == 400
    with mock.patch("app1.memory_store.log_event") as log:
        response = client.post("/api/learner/events", json={
            "event": "cv_apply", "meta": {"created": 4, "too_long": "x" * 500},
        })
    assert response.status_code == 200
    assert "too_long" not in log.call_args.kwargs["extra"]
