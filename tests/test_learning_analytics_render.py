"""Tests for the learning_analytics dead-data recovery (plan #8).

The learning_analytics() route already computes six expensive datasets but only
rendered three. This initiative renders the three discarded ones with the
token-bound FMChart layer:

  * hourly_engagement        -> "when do people learn" 24-bucket bar
  * learning_path_effectiveness -> per-path progress / completion-rate bars
  * skills_gap_analysis      -> course-category ORDER-vs-COMPLETION throughput
                                (NOT skill demand/supply — risk-register #320)

Every one is a per-user breakdown, so each must apply a k-anon floor BEFORE it
reaches the chart (suppress sub-k cohorts), and fail closed if the k-anon helper
is unavailable. These tests assert two layers without an app boot or a DB:

  1. The suppression / relabelling contract on the exact candidate shapes the
     route builds.
  2. The template renders the three new chart cards + canvases + anon notes
     from the data the route passes, and never emits a sub-k cohort or a
     mislabelled "skill demand" axis.
"""
import json
import re

import jinja2

import kanon

TEMPLATES = "templates"
K = kanon.K_DEFAULT  # env-resolved active floor


# ── 1a. Hourly-engagement suppression contract ───────────────────────────────

def _build_hourly_buckets(rows):
    """Mirror the route's hourly-engagement fill + k-anon zeroing.

    Keep in lockstep with learning_analytics(): an hour's interaction count
    reaches the chart only when its distinct-user cohort is >= k; otherwise the
    bucket is zeroed (suppressed) so a single-learner hour can't be profiled.
    """
    interactions = [0] * 24
    suppressed = 0
    for row in rows:
        hod = row.get("hour_of_day")
        if hod is None or not (0 <= int(hod) < 24):
            continue
        cohort = int(row.get("unique_users") or 0)
        if kanon.is_cohort_safe(cohort):
            interactions[int(hod)] = int(row.get("interactions") or 0)
        elif int(row.get("interactions") or 0) > 0:
            suppressed += 1
    return interactions, suppressed


def test_single_learner_hour_is_suppressed():
    """An hour where only one learner was active must not surface its count."""
    rows = [
        {"hour_of_day": 9, "interactions": 40, "unique_users": K + 3},
        {"hour_of_day": 3, "interactions": 5, "unique_users": 1},  # sub-k
    ]
    interactions, suppressed = _build_hourly_buckets(rows)
    assert interactions[9] == 40
    assert interactions[3] == 0, "single-learner hour leaked its activity pattern"
    assert suppressed == 1


def test_k_safe_hours_are_kept():
    rows = [
        {"hour_of_day": 10, "interactions": 12, "unique_users": K},
        {"hour_of_day": 14, "interactions": 30, "unique_users": K + 9},
    ]
    interactions, suppressed = _build_hourly_buckets(rows)
    assert interactions[10] == 12 and interactions[14] == 30
    assert suppressed == 0


def test_out_of_range_hour_is_ignored():
    rows = [{"hour_of_day": 99, "interactions": 99, "unique_users": K + 5}]
    interactions, suppressed = _build_hourly_buckets(rows)
    assert interactions == [0] * 24
    assert suppressed == 0


# ── 1b. Learning-path effectiveness suppression contract ─────────────────────

def _build_paths(rows):
    candidates = []
    for row in rows:
        enrolled = int(row.get("enrolled_users") or 0)
        if enrolled <= 0:
            continue
        completed = int(row.get("completed_users") or 0)
        candidates.append({
            "path_name": row.get("path_name") or "Uden navn",
            "enrolled_users": enrolled,
            "completed_users": completed,
            "completion_rate": round(completed * 100.0 / enrolled, 1),
            "avg_progress": round(float(row.get("avg_progress") or 0), 1),
            "avg_completion_days": round(float(row.get("avg_completion_days") or 0), 1),
        })
    kept, note = kanon.suppress_small_groups(candidates, "enrolled_users")
    kept = [r for r in kept if isinstance(r, dict) and "enrolled_users" in r]
    kept.sort(key=lambda r: r.get("enrolled_users", 0), reverse=True)
    return kept[:8], note


def test_sub_k_path_is_suppressed():
    rows = [
        {"path_name": "Solo onboarding", "enrolled_users": 1,
         "completed_users": 1, "avg_progress": 100},
        {"path_name": "Lederforløb", "enrolled_users": K + 4,
         "completed_users": 3, "avg_progress": 60},
    ]
    kept, note = _build_paths(rows)
    names = {p["path_name"] for p in kept}
    assert "Solo onboarding" not in names, "sub-k path leaked an individual's progress"
    assert "Lederforløb" in names
    assert note["suppressed"] >= 1


def test_never_started_path_excluded():
    rows = [{"path_name": "Empty", "enrolled_users": 0,
             "completed_users": 0, "avg_progress": 0}]
    kept, _ = _build_paths(rows)
    assert kept == []


def test_path_completion_rate_is_derived_not_raw():
    rows = [{"path_name": "P", "enrolled_users": 10,
             "completed_users": 4, "avg_progress": 55}]
    kept, _ = _build_paths(rows)
    assert kept[0]["completion_rate"] == 40.0


def test_paths_sorted_by_enrolment_and_capped():
    rows = [
        {"path_name": f"P{i}", "enrolled_users": K + i,
         "completed_users": 1, "avg_progress": 10}
        for i in range(12)
    ]
    kept, _ = _build_paths(rows)
    assert len(kept) == 8
    enrolments = [p["enrolled_users"] for p in kept]
    assert enrolments == sorted(enrolments, reverse=True)


# ── 1c. Course-category throughput suppression + relabel contract ────────────

def _build_categories(rows):
    candidates = []
    for row in rows:
        ordered = int(row.get("demand") or 0)
        if ordered <= 0:
            continue
        completed = int(row.get("supply") or 0)
        candidates.append({
            "skill_category": row.get("skill_category") or "Andet",
            "ordered": ordered,
            "completed": completed,
            "interested_employees": int(row.get("interested_employees") or 0),
            "fulfillment_rate": float(row.get("fulfillment_rate") or 0),
        })
    kept, note = kanon.suppress_small_groups(candidates, "interested_employees")
    kept = [r for r in kept if isinstance(r, dict) and "interested_employees" in r]
    kept.sort(key=lambda r: r.get("ordered", 0), reverse=True)
    return kept, note


def test_sub_k_category_is_suppressed():
    rows = [
        {"skill_category": "Sales", "demand": 9, "supply": 4,
         "interested_employees": 1},  # one identifiable employee
        {"skill_category": "Leadership", "demand": 20, "supply": 12,
         "interested_employees": K + 6},
    ]
    kept, note = _build_categories(rows)
    cats = {c["skill_category"] for c in kept}
    assert "Sales" not in cats, "sub-k category exposed an individual's orders"
    assert "Leadership" in cats
    assert note["suppressed"] >= 1


def test_category_payload_relabels_to_order_volume():
    """The category dataset is ORDER throughput, not skill demand/supply. The
    route must surface 'ordered'/'completed' volume keys, never 'demand'/'supply'
    framed as a skill signal (risk-register #320)."""
    rows = [{"skill_category": "Tech", "demand": 15, "supply": 9,
             "interested_employees": K + 2}]
    kept, _ = _build_categories(rows)
    row = kept[0]
    assert row["ordered"] == 15 and row["completed"] == 9
    # The raw competency-loaded keys must not survive into the chart shape.
    assert "demand" not in row and "supply" not in row


# ── 2. Template render ───────────────────────────────────────────────────────

def _render(**ctx):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES),
        undefined=jinja2.ChainableUndefined,
    )
    env.globals["url_for"] = lambda ep, **kw: "/" + ep
    env.globals["csrf_token"] = lambda: ""
    env.globals["session"] = {}
    env.globals["request"] = jinja2.ChainableUndefined()
    env.globals["get_flashed_messages"] = lambda **kw: []
    tmpl = env.get_template("fm/learning_analytics.html")
    base = dict(
        company={"id": 1, "name": "Acme"},
        period="30d",
        learning_trends=[],
        popular_courses=[],
        department_comparison=[],
        learning_path_effectiveness=[],
        hourly_engagement=[],
        skills_gap_analysis=[],
        chart_data={
            "learning_trends": {"dates": [], "enrollments": [], "completions": []},
            "hourly_engagement": {"hours": [f"{h:02d}:00" for h in range(24)],
                                  "interactions": [0] * 24},
        },
        engagement_anon=None,
        path_effectiveness=[],
        path_anon=None,
        category_throughput=[],
        category_anon=None,
        active_hr_page="learning_analytics",
    )
    base.update(ctx)
    return tmpl.render(**base)


def test_three_new_chart_cards_render():
    """All three discarded datasets get a chart card. hourChart is always
    emitted (empty-state via FMChart); pathChart/categoryChart appear once they
    carry k-safe data."""
    html = _render(
        path_effectiveness=[
            {"path_name": "Lederforløb", "enrolled_users": K + 5,
             "completed_users": 4, "completion_rate": 40.0,
             "avg_progress": 62.5, "avg_completion_days": 21.0},
        ],
        category_throughput=[
            {"skill_category": "Leadership", "ordered": 10, "completed": 6,
             "interested_employees": K + 3, "fulfillment_rate": 60.0},
        ],
    )
    assert 'id="hourChart"' in html
    assert 'id="pathChart"' in html
    assert 'id="categoryChart"' in html


def test_hourly_card_always_present():
    """The hourly heatmap card has no {% if %} guard — it shows the empty state
    via FMChart when all buckets are zero, so the canvas is always emitted."""
    html = _render()
    assert "Hvornår på dagen lærer medarbejderne" in html
    assert 'id="hourChart"' in html


def test_category_axis_is_labelled_as_order_volume_not_skill_demand():
    """Risk-register #320: the category chart must read as order-vs-completion
    volume, never 'skill demand vs supply'."""
    html = _render(category_throughput=[
        {"skill_category": "Leadership", "ordered": 10, "completed": 6,
         "interested_employees": K + 3, "fulfillment_rate": 60.0},
    ])
    assert "Bestilte kurser" in html
    assert "Gennemførte kurser" in html
    # The visible card title frames it as bestilte vs. gennemførte kurser and the
    # subtitle explicitly disclaims the skill-demand framing (risk-register #320).
    assert "Bestilte vs. gennemførte kurser pr. kategori" in html
    assert "ikke kompetencebehov" in html
    # And must NOT present this as a competency demand-vs-supply signal as a label.
    assert "Kompetencebehov vs. udbud" not in html
    assert "Skill demand vs supply" not in html


def test_path_chart_payload_is_aggregate_only():
    """No per-employee identity ever flows into the path effectiveness chart."""
    html = _render(path_effectiveness=[
        {"path_name": "Lederforløb", "enrolled_users": K + 5,
         "completed_users": 4, "completion_rate": 40.0,
         "avg_progress": 62.5, "avg_completion_days": 21.0},
    ])
    m = re.search(r"var pe = (\[.*?\]);", html, re.S)
    assert m, "could not find the path-effectiveness chart payload"
    payload = json.loads(m.group(1))
    assert payload
    for row in payload:
        assert "username" not in row and "user_id" not in row and "name" not in row


def test_anon_notes_render_when_present():
    html = _render(
        engagement_anon="Timer skjult for anonymitet",
        path_effectiveness=[{"path_name": "P", "enrolled_users": K,
                             "completed_users": 1, "completion_rate": 20.0,
                             "avg_progress": 30.0, "avg_completion_days": 5.0}],
        path_anon="Forløb skjult for anonymitet",
        category_throughput=[{"skill_category": "Tech", "ordered": 5,
                              "completed": 2, "interested_employees": K,
                              "fulfillment_rate": 40.0}],
        category_anon="Kategorier skjult for anonymitet",
    )
    assert "Timer skjult for anonymitet" in html
    assert "Forløb skjult for anonymitet" in html
    assert "Kategorier skjult for anonymitet" in html


def test_empty_states_when_no_path_or_category_data():
    html = _render(path_effectiveness=[], category_throughput=[])
    # The path / category cards fall back to the empty() macro and never emit
    # their canvases when there is no k-safe data.
    assert 'id="pathChart"' not in html
    assert 'id="categoryChart"' not in html


def test_learning_analytics_in_canonical_subnav():
    """The page must be reachable from the canonical HR subnav (the 'add it to
    the nav' half of the initiative)."""
    subnav = open(f"{TEMPLATES}/fm/_hr_subnav.html").read()
    # The canonical subnav binds the active state via the _hp alias.
    assert "_hp == 'learning_analytics'" in subnav
    assert "hr_dashboard.learning_analytics" in subnav
