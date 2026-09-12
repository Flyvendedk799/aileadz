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


def test_cv_summary_api_endpoint(client):
    profile = _empty_profile()
    profile.update({
        "bio": "Erfaren dataanalytiker",
        "skills": [{"name": "SQL", "level": "avanceret"}, {"name": "Python", "level": "mellem"}],
        "experience": [{"title": "Data Analyst", "company": "Tech Corp", "start_year": 2021, "is_current": True}],
        "education": [{"degree": "BSc Datalogi", "institution": "KU"}],
        "certifications": [{"name": "Azure Data Fundamentals"}],
        "completed_courses": [{"title": "SQL Deep Dive"}],
        "languages": [{"language": "Dansk", "proficiency": "modersmaal"}],
    })
    with mock.patch("app1.user_profile_db.ensure_tables"), \
         mock.patch("app1.user_profile_db.get_full_profile", return_value=profile), \
         mock.patch("app1.user_profile_db.profile_completeness", return_value={"pct": 80, "weighted_pct": 85, "sections": []}), \
         mock.patch("competency.compute_skill_gaps", return_value=[{"skill": "Machine Learning", "gap": 2}]):
        res = client.get("/api/cv/summary")

    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["has_cv"] is True
    s = data["summary"]
    assert s["skills_count"] == 2
    assert s["experience_count"] == 1
    assert s["education_count"] == 1
    assert s["certifications_count"] == 1
    assert s["courses_count"] == 1
    assert s["languages_count"] == 1
    assert len(s["top_skills"]) == 2
    assert s["top_skills"][0]["name"] == "SQL"
    assert len(s["recent_experience"]) == 1
    assert s["recent_experience"][0]["company"] == "Tech Corp"
    assert s["completeness_pct"] == 80
    assert len(s["top_gaps"]) == 1


def test_cv_summary_api_empty_profile(client):
    empty = _empty_profile()
    with mock.patch("app1.user_profile_db.ensure_tables"), \
         mock.patch("app1.user_profile_db.get_full_profile", return_value=empty), \
         mock.patch("app1.user_profile_db.profile_completeness", return_value={"pct": 0, "weighted_pct": 0, "sections": []}), \
         mock.patch("competency.compute_skill_gaps", return_value=[]):
        res = client.get("/api/cv/summary")

    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["has_cv"] is False
    assert data["summary"]["skills_count"] == 0


def test_learning_path_lifecycle_endpoints(client):
    with mock.patch("app1.user_profile_db.ensure_tables"), \
         mock.patch("app1.user_profile_db.update_learning_path_status", return_value=True) as update_mock, \
         mock.patch("app1.user_profile_db.toggle_learning_path_step", return_value={"id": 42, "status": "fuldfoert", "steps": [{"order": 1, "done": True}]}) as toggle_mock, \
         mock.patch("app1.user_profile_db.delete_learning_path", return_value=True) as delete_mock:

        # Test status update
        res = client.post("/api/profile/learning-paths/42/status", json={"status": "fuldfoert"})
        assert res.status_code == 200
        assert res.get_json()["status"] == "fuldfoert"
        update_mock.assert_called_once_with("alice", 42, "fuldfoert")

        # Test invalid status
        res_bad = client.post("/api/profile/learning-paths/42/status", json={"status": "not_a_status"})
        assert res_bad.status_code == 400

        # Test step toggle
        res_step = client.post("/api/profile/learning-paths/42/step", json={"step_order": 1, "done": True})
        assert res_step.status_code == 200
        assert res_step.get_json()["path"]["status"] == "fuldfoert"
        toggle_mock.assert_called_once_with("alice", 42, 1, done=True)

        # Test step toggle missing order
        res_no_order = client.post("/api/profile/learning-paths/42/step", json={"done": True})
        assert res_no_order.status_code == 400

        # Test delete (archive)
        res_del = client.delete("/api/profile/learning-paths/42")
        assert res_del.status_code == 200
        assert res_del.get_json()["deleted"] is True
        delete_mock.assert_called_once_with("alice", 42)


def test_skill_history_valid_sources_and_snapshot_pipeline():
    import skill_history
    for expected in ("assign", "post_course", "profile_manual", "cv_upload", "ai_chat"):
        assert expected in skill_history.VALID_SOURCES

    cur_mock = mock.MagicMock()
    # Test valid source
    ok = skill_history.record_snapshot(cur_mock, 10, 20, "Python", 4, previous_level=2, source="profile_manual")
    assert ok is True
    assert cur_mock.execute.called
    sql = cur_mock.execute.call_args[0][0]
    args = cur_mock.execute.call_args[0][1]
    assert "INSERT INTO employee_skill_history" in sql
    assert args[5] == "profile_manual"


def test_navigation_shell_unification_and_modebar():
    modebar = open("templates/fm/_ai_modebar.html", encoding="utf-8").read()
    assert "futurematch.cv_upload" in modebar
    assert "CV-portal" in modebar
    assert "futurematch.chat" in modebar
    assert "futurematch.ai_profiler" in modebar
    assert "futurematch.mind_map" in modebar

    cv_upload = open("templates/fm/cv_upload.html", encoding="utf-8").read()
    assert "fm/_ai_modebar.html" in cv_upload
    assert ".ai-modebar" in cv_upload

    fm_base = open("templates/fm_base.html", encoding="utf-8").read()
    assert "'cvupload'" in fm_base
    assert "fm/_ai_sidebar.html" in fm_base


def test_profile_page_intent_bridging_and_cv_widget():
    profile_src = open("templates/fm/my_profile.html", encoding="utf-8").read()
    assert "futurematch.ai_profiler" in profile_src
    assert "?intent=" in profile_src
    assert "cvWidgetCard" in profile_src
    assert "loadCvSummary" in profile_src
    assert "togglePathStep" in profile_src
    assert "setPathStatus" in profile_src

    pages_src = open("pages.py", encoding="utf-8").read()
    assert "@pages_bp.route('/profile')" in pages_src
    assert "@pages_bp.route('/profil')" in pages_src


def test_update_learning_path_tool_and_registry():
    from app1.tools import _execute_update_learning_path, PROFILE_TOOLS
    import ai_tool_registry

    # Tool is registered in PROFILE_TOOLS
    tool_names = [t["function"]["name"] for t in PROFILE_TOOLS if t.get("type") == "function"]
    assert "update_learning_path" in tool_names

    # Registry has metadata, display name, and triggers
    meta = ai_tool_registry.get_tool_meta("update_learning_path")
    assert meta is not None
    assert "mutation" in meta.toolset_tags
    assert ai_tool_registry._TOOL_LABELS.get("update_learning_path") == "Opdater læringssti"
    assert "update_learning_path" in ai_tool_registry._TOOL_TRIGGERS

    # Execution with mocks
    with mock.patch("app1.user_profile_db.ensure_tables"), \
         mock.patch("app1.user_profile_db.toggle_learning_path_step", return_value={"id": 5, "status": "fuldfoert", "steps": []}), \
         mock.patch("app1.user_profile_db.update_learning_path_status", return_value=True), \
         mock.patch("app1.user_profile_db.get_learning_path", return_value={"id": 5, "title": "Sti", "status": "fuldfoert", "steps": []}):
        res = _execute_update_learning_path({"path_id": 5, "step_order": 1, "step_done": True, "status": "fuldfoert"}, "alice")
        import json
        out = json.loads(res)
        assert out["status"] == "success"
        assert out["path"]["id"] == 5
