"""Tests for the k-anon-safe compliance at-risk visual (plan #6).

The compliance route already computes the donut from company-wide totals (safe,
already aggregated). This initiative adds a PER-REQUIREMENT "most-at-risk
requirements" bar. A per-requirement breakdown can re-identify an individual when
a requirement applies to a tiny group, so the bar must suppress any requirement
applicable to fewer than k employees BEFORE it reaches the chart.

These tests assert two layers without an app boot or a DB:

  1. The suppression contract on the exact candidate shape the route builds:
     sub-k requirements are dropped, k-safe ones kept, sorting is most-at-risk
     first, and the chart is capped — i.e. the hard GDPR/k-anon invariant that
     no sub-k cohort ever reaches the visual.
  2. The template renders the at-risk card + canvas + anon note from the data
     the route passes, and emits only the k-safe rows into the chart payload.
"""
import json
import re

import jinja2
import pytest

import kanon

TEMPLATES = "templates"
K = kanon.K_DEFAULT  # env-resolved active floor


def _build_candidates(matrix):
    """Mirror the route's at-risk candidate construction (hr_dashboard route).

    Keep this in lockstep with compliance_matrix(): a requirement is a candidate
    only when it has at-risk employees, and its k-anon cohort is `applicable`.
    """
    candidates = []
    for row in matrix:
        ex = int(row.get("expiring") or 0)
        ov = int(row.get("overdue") or 0)
        if (ex + ov) <= 0:
            continue
        req = row.get("requirement") or {}
        candidates.append({
            "title": req.get("title") or "Uden titel",
            "is_statutory": bool(req.get("is_statutory")),
            "expiring": ex,
            "overdue": ov,
            "at_risk": ex + ov,
            "_cohort": int(row.get("applicable") or 0),
        })
    return candidates


def _suppress_and_sort(candidates):
    """Mirror the route's suppression + sort + cap."""
    kept, note = kanon.suppress_small_groups(candidates, "_cohort")
    kept = [r for r in kept if isinstance(r, dict) and "_cohort" in r]
    kept.sort(key=lambda r: (r.get("at_risk", 0), r.get("overdue", 0)), reverse=True)
    return kept[:8], note


# ── 1. Suppression contract ──────────────────────────────────────────────────

def test_sub_k_requirement_is_suppressed_from_chart():
    """A requirement applicable to < k employees must never reach the chart,
    even if every applicable employee is overdue (worst at-risk case)."""
    matrix = [
        {"requirement": {"title": "Tiny team GDPR"}, "applicable": 1,
         "compliant": 0, "expiring": 0, "overdue": 1},
        {"requirement": {"title": "Big team brandsikkerhed"}, "applicable": K + 10,
         "compliant": 2, "expiring": 3, "overdue": 4},
    ]
    kept, note = _suppress_and_sort(_build_candidates(matrix))
    titles = {r["title"] for r in kept}
    assert "Tiny team GDPR" not in titles, "sub-k requirement leaked into the visual"
    assert "Big team brandsikkerhed" in titles
    assert note["suppressed"] >= 1


def test_k_safe_requirements_are_kept():
    """Requirements applicable to >= k employees are retained."""
    matrix = [
        {"requirement": {"title": "A"}, "applicable": K,
         "compliant": 1, "expiring": 1, "overdue": 1},
        {"requirement": {"title": "B"}, "applicable": K + 5,
         "compliant": 1, "expiring": 0, "overdue": 2},
    ]
    kept, note = _suppress_and_sort(_build_candidates(matrix))
    assert {r["title"] for r in kept} == {"A", "B"}
    assert note["suppressed"] == 0


def test_fully_compliant_requirement_is_not_at_risk():
    """A requirement with zero expiring+overdue is not a candidate at all."""
    matrix = [
        {"requirement": {"title": "All good"}, "applicable": K + 3,
         "compliant": K + 3, "expiring": 0, "overdue": 0},
    ]
    candidates = _build_candidates(matrix)
    assert candidates == []


def test_sorted_most_at_risk_first():
    matrix = [
        {"requirement": {"title": "Low"}, "applicable": K + 2,
         "compliant": 0, "expiring": 1, "overdue": 0},
        {"requirement": {"title": "High"}, "applicable": K + 2,
         "compliant": 0, "expiring": 2, "overdue": 6},
        {"requirement": {"title": "Mid"}, "applicable": K + 2,
         "compliant": 0, "expiring": 1, "overdue": 2},
    ]
    kept, _ = _suppress_and_sort(_build_candidates(matrix))
    assert [r["title"] for r in kept] == ["High", "Mid", "Low"]


def test_chart_is_capped_to_eight():
    matrix = [
        {"requirement": {"title": f"Req {i}"}, "applicable": K + 1,
         "compliant": 0, "expiring": 0, "overdue": i + 1}
        for i in range(20)
    ]
    kept, _ = _suppress_and_sort(_build_candidates(matrix))
    assert len(kept) == 8


# ── 2. Template render ───────────────────────────────────────────────────────

def _render(**ctx):
    # ChainableUndefined lets the shared fm_base.html chrome (session/request/
    # csrf_token globals) resolve to empty instead of raising, so we can render
    # the real template inheritance with no Flask app boot and no DB.
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES),
        undefined=jinja2.ChainableUndefined,
    )
    env.globals["url_for"] = lambda ep, **kw: "/" + ep
    env.globals["csrf_token"] = lambda: ""
    env.globals["session"] = {}
    env.globals["request"] = jinja2.ChainableUndefined()
    env.globals["get_flashed_messages"] = lambda **kw: []
    tmpl = env.get_template("fm/compliance.html")
    base = dict(
        company={"id": 1, "name": "Acme"},
        matrix=[],
        totals={"requirements": 0, "compliant": 0, "expiring": 0, "overdue": 0},
        departments=[],
        roles=[],
        at_risk_chart=[],
        compliance_anon=None,
        active_hr_page="compliance",
    )
    base.update(ctx)
    return tmpl.render(**base)


def test_at_risk_card_renders_with_data():
    at_risk = [
        {"title": "Brandsikkerhed", "is_statutory": True,
         "expiring": 3, "overdue": 4, "at_risk": 7, "_cohort": K + 10},
    ]
    html = _render(
        totals={"requirements": 5, "compliant": 10, "expiring": 3, "overdue": 4},
        at_risk_chart=at_risk,
    )
    assert "Mest udsatte krav" in html
    assert 'id="complianceAtRisk"' in html
    # The chart payload must carry the safe row.
    assert "Brandsikkerhed" in html


def test_at_risk_card_hidden_without_data():
    html = _render(
        totals={"requirements": 5, "compliant": 10, "expiring": 0, "overdue": 0},
        at_risk_chart=[],
    )
    assert "Mest udsatte krav" not in html
    assert 'id="complianceAtRisk"' not in html


def test_anon_note_renders_when_present():
    note = "Grupper under k=5 er skjult af hensyn til anonymitet"
    html = _render(
        totals={"requirements": 5, "compliant": 10, "expiring": 1, "overdue": 1},
        at_risk_chart=[{"title": "X", "is_statutory": False,
                        "expiring": 1, "overdue": 1, "at_risk": 2, "_cohort": K}],
        compliance_anon=note,
    )
    assert note in html


def test_chart_payload_excludes_employee_names():
    """The at-risk chart payload must be aggregate counts only — no per-employee
    identity ever flows into the visual (k-anon / GDPR hard constraint)."""
    at_risk = [
        {"title": "GDPR", "is_statutory": True,
         "expiring": 2, "overdue": 3, "at_risk": 5, "_cohort": K + 4},
    ]
    html = _render(
        totals={"requirements": 1, "compliant": 1, "expiring": 2, "overdue": 3},
        at_risk_chart=at_risk,
    )
    # Pull the JSON array the template emits into the chart.
    m = re.search(r"var atRisk = (\[.*?\]);", html, re.S)
    assert m, "could not find the atRisk chart payload"
    payload = json.loads(m.group(1))
    assert payload, "payload should not be empty when data is present"
    for row in payload:
        # Only aggregate / labeling keys are allowed in the chart payload.
        assert set(row.keys()) <= {
            "title", "is_statutory", "expiring", "overdue", "at_risk", "_cohort",
        }
        assert "name" not in row and "user_id" not in row and "employees" not in row
