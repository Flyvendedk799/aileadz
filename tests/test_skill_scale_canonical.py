"""Canonical 1-5 skill-level scale (plan #5).

The HR skill radar, dashboard skill tiles and the "Gns. niveau" KPI used to
systematically understate competence: the HR matrix/targets and the whole viz
(radar max=5, "af 5 mulige", sliders min=1/max=5) live on a 1-5 scale, but the
chatbot 4-label enum (begynder..ekspert) was mapped onto 1-4. A fully
ekspert-profiled workforce could therefore never exceed 4/5, so the radar "gap"
was partly an artifact and the badge on hr.html even read "/4" beside 5 dots.

The fix normalizes EVERY source onto a single 1-5 scale, in lockstep, so gaps
cannot flip sign (per the HR_VALUE_PLAN risk register). These tests lock in:
  1. hr_tools.SKILL_LEVEL_MAP / _skill_level_to_int are the canonical 1-5 map
     (ekspert reaches the ceiling 5; ints pass through unchanged).
  2. Every chatbot string->int map across the backend (the SQL CASE level_maps
     in insights_engine.py + hr_dashboard, and the dict map in the employee
     detail view) agrees on the SAME 1-5 mapping — a source-level drift guard.
  3. The HR viz denominators are /5 (no stray /4), and the radar max stays 5.

They run with no Flask boot and no live DB: (1)/(2) are pure-function /
source-text checks; (3) reads template files.
"""
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# The single source of truth for the chatbot enum spread onto the canonical
# 1-5 scale. begynder/mellem keep their low anchors; avanceret/ekspert take the
# upper anchors so a self-reported "ekspert" reaches the ceiling.
CANONICAL = {"begynder": 1, "mellem": 2, "avanceret": 4, "ekspert": 5}


# ---------------------------------------------------------------------------
# 1) hr_tools canonical map + idempotent normalizer
# ---------------------------------------------------------------------------

def test_skill_level_map_is_canonical_1_to_5():
    import hr_tools
    assert hr_tools.SKILL_LEVEL_MAP == CANONICAL
    # ekspert must reach the ceiling so it can meet a 5/5 HR target.
    assert hr_tools.SKILL_LEVEL_MAP["ekspert"] == 5
    assert hr_tools.SKILL_LEVEL_MAP["begynder"] == 1


def test_skill_level_to_int_maps_strings_once_on_1_to_5():
    import hr_tools
    f = hr_tools._skill_level_to_int
    assert f("begynder") == 1
    assert f("mellem") == 2
    assert f("avanceret") == 4
    assert f("ekspert") == 5


def test_skill_level_to_int_passes_ints_through_unchanged():
    # The historical gap bug: re-mapping an already-int matrix level through the
    # string table collapsed every gap to the default. Ints must pass through.
    import hr_tools
    f = hr_tools._skill_level_to_int
    assert f(1) == 1
    assert f(3) == 3
    assert f(5) == 5
    assert f("4") == 4          # numeric string is still a number
    # clamp to the canonical 0..5 range, never silently widen the scale
    assert f(9) == 5
    assert f(0) == 0


def test_skill_level_to_int_unknown_falls_back_without_inflating():
    import hr_tools
    f = hr_tools._skill_level_to_int
    assert f(None, default=0) == 0
    assert f("garbage", default=0) == 0
    assert f("garbage", default=2) == 2


# ---------------------------------------------------------------------------
# 2) Source-level lockstep guard — every chatbot map agrees on 1-5
# ---------------------------------------------------------------------------

def _read(path):
    with open(os.path.join(REPO, path), "r", encoding="utf-8") as fh:
        return fh.read()


def _sql_case_maps(text):
    """Extract (begynder,mellem,avanceret,ekspert) ints from every SQL CASE
    level_map of the form ...WHEN 'begynder' THEN 1 ... WHEN 'ekspert' THEN N..."""
    pat = re.compile(
        r"WHEN 'begynder' THEN (\d+) WHEN 'mellem' THEN (\d+) "
        r"WHEN 'avanceret' THEN (\d+) WHEN 'ekspert' THEN (\d+)"
    )
    return [tuple(int(g) for g in m.groups()) for m in pat.finditer(text)]


def test_all_sql_level_maps_are_1_to_5_in_lockstep():
    expected = (CANONICAL["begynder"], CANONICAL["mellem"],
                CANONICAL["avanceret"], CANONICAL["ekspert"])
    found = []
    for path in ("insights_engine.py", "hr_dashboard/__init__.py"):
        maps = _sql_case_maps(_read(path))
        assert maps, f"no SQL level_map found in {path} — did it move?"
        found.extend((path, m) for m in maps)
    for path, m in found:
        assert m == expected, f"SQL level_map in {path} drifted off 1-5: {m}"


def test_employee_detail_dict_map_is_1_to_5():
    # The Python dict map used in the employee-detail view (hr_dashboard) must
    # match the canonical map, or that page disagrees with the dashboard radar.
    text = _read("hr_dashboard/__init__.py")
    m = re.search(
        r"\{'begynder':\s*(\d+),\s*'mellem':\s*(\d+),\s*"
        r"'avanceret':\s*(\d+),\s*'ekspert':\s*(\d+)\}",
        text,
    )
    assert m, "employee-detail dict level_map not found"
    got = tuple(int(g) for g in m.groups())
    assert got == (1, 2, 4, 5), f"employee-detail dict map drifted off 1-5: {got}"


# ---------------------------------------------------------------------------
# 3) Viz denominators are /5 (no stray /4) and radar max stays 5
# ---------------------------------------------------------------------------

def test_hr_dashboard_skill_badge_is_over_5_not_4():
    text = _read("templates/fm/hr.html")
    assert "/5 gns." in text, "hr.html skill badge should read /5"
    assert "/4 gns." not in text, "hr.html still shows the stale /4 skill badge"


def test_skill_gaps_viz_uses_1_to_5_scale():
    text = _read("templates/fm/skill_gaps.html")
    # KPI tile + radar max + sliders are all on 1-5
    assert "af 5 mulige" in text
    assert "max: 5" in text
    assert text.count('max="5"') >= 2  # both target and assign sliders
    assert 'min="1"' in text
    # the critical-gap badges carry the /5 denominator
    assert "}}/5 gns." in text


def test_no_hr_skill_template_advertises_a_4_max():
    # Guard against re-introducing a "/4" or "af 4" competence denominator on
    # any HR skill surface (the trust regression this initiative fixes).
    for path in ("templates/fm/hr.html",
                 "templates/fm/skill_gaps.html",
                 "templates/fm/employee_details.html"):
        text = _read(path)
        assert "/4 gns." not in text, f"{path} reintroduced a /4 skill denominator"
        assert "af 4 mulige" not in text, f"{path} reintroduced 'af 4 mulige'"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
