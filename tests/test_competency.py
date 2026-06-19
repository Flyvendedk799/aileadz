"""competency.py — canonicalization, categories, the 1-5 scale bridge, and the
per-learner skill-gap engine (the missing 'B' in the A→C journey).

Pure-function / injected-profile tests: no Flask boot, no DB. compute_skill_gaps
is exercised via the role/goal sources (the company-targets SELECT degrades to
[] with no app context), which is exactly the offline-safe contract.
"""
import competency as C


# ── canonical_skill ──

def test_canonical_aliases_collapse_variants():
    assert C.canonical_skill("js") == "JavaScript"
    assert C.canonical_skill("JavaScript") == "JavaScript"
    assert C.canonical_skill("python3") == "Python"
    assert C.canonical_skill("PYTHON") == "Python"
    assert C.canonical_skill("reactjs") == "React"
    assert C.canonical_skill("k8s") == "Kubernetes"


def test_canonical_acronyms_uppercased():
    assert C.canonical_skill("sql") == "SQL"
    assert C.canonical_skill("gdpr") == "GDPR"
    assert C.canonical_skill("seo") == "SEO"


def test_canonical_preserves_deliberate_mixedcase():
    # iOS / DevOps typed correctly must not be mangled.
    assert C.canonical_skill("iOS") == "iOS"
    assert C.canonical_skill("DevOps") == "DevOps"


def test_canonical_titlecases_plain_words_and_trims():
    assert C.canonical_skill("  projektledelse ") == "Projektledelse"
    assert C.canonical_skill("machine   learning") == "Machine Learning"
    assert C.canonical_skill("") == ""
    assert C.canonical_skill(None) == ""


def test_skill_key_dedupes_case_and_alias():
    assert C.skill_key("python") == C.skill_key("Python3")
    assert C.skill_key("JS") == C.skill_key("javascript")


# ── categories ──

def test_skill_category_buckets():
    assert C.skill_category("Python") == "Programmering"
    assert C.skill_category("Power BI") == "Data & AI"
    assert C.skill_category("Kubernetes") == "Cloud & DevOps"
    assert C.skill_category("Projektledelse") == "Projektledelse"
    assert C.skill_category("Figma") == "Design & UX"
    assert C.skill_category("Salg") == "Forretning & Salg"
    assert C.skill_category("Zzz Unknown Skill") == "Andet"


# ── the canonical 1-5 scale bridge (reuses hr_tools) ──

def test_level_to_score_matches_hr_scale():
    assert C.level_to_score("begynder") == 1
    assert C.level_to_score("mellem") == 2
    assert C.level_to_score("avanceret") == 4
    assert C.level_to_score("ekspert") == 5     # reaches the HR ceiling
    assert C.level_to_score(None) == 0


def test_level_to_score_idempotent_on_ints():
    # The historical gap bug: re-mapping an already-int level collapsed it.
    assert C.level_to_score(3) == 3
    assert C.level_to_score(5) == 5
    assert C.level_to_score(9) == 5   # clamp, never widen


def test_score_to_level_and_label():
    assert C.score_to_level(5) == "ekspert"
    assert C.score_to_level(4) == "avanceret"
    assert C.score_to_level(2) == "mellem"
    assert C.score_to_level(1) == "begynder"
    assert C.score_label_da(0) == "ingen"
    assert C.score_label_da(5) == "ekspert"


# ── the gap engine ──

def _profile(skills=None, target_role="", goals="", learning_goals=None):
    return {
        "skills": skills or [],
        "target_role": target_role,
        "goals": goals,
        "learning_goals": learning_goals or [],
    }


def test_gap_engine_role_based_gaps():
    # Target role "data analyst" wants SQL/Python/Statistik/...; the learner has
    # SQL at begynder (1) and nothing else → SQL gap 4-1=3, others 4-0=4.
    prof = _profile(skills=[{"name": "sql", "level": "begynder"}],
                    target_role="Data Analyst")
    gaps = C.compute_skill_gaps("u", profile=prof)
    by = {g["skill"]: g for g in gaps}
    assert "SQL" in by and by["SQL"]["gap"] == 3
    assert by["SQL"]["current_level"] == 1 and by["SQL"]["target_level"] == 4
    assert "Python" in by and by["Python"]["gap"] == 4
    # every gap is on the 1-5 scale and carries a category + source
    for g in gaps:
        assert 1 <= g["target_level"] <= 5
        assert g["source"] in ("company", "role", "goal")
        assert g["category"]


def test_gap_engine_skips_covered_skills():
    # An ekspert (5) in a role skill has no gap (target 4) and is omitted.
    prof = _profile(skills=[{"name": "Python", "level": "ekspert"}],
                    target_role="data scientist")
    gaps = C.compute_skill_gaps("u", profile=prof)
    assert "Python" not in {g["skill"] for g in gaps}


def test_gap_engine_goal_tokens():
    prof = _profile(goals="Jeg vil gerne blive bedre til Excel og Power BI")
    gaps = {g["skill"] for g in C.compute_skill_gaps("u", profile=prof)}
    assert "Excel" in gaps and "Power BI" in gaps


def test_gap_engine_empty_profile_is_safe():
    assert C.compute_skill_gaps("u", profile=_profile()) == []
    # never raises, even with a totally malformed profile
    assert isinstance(C.compute_skill_gaps("u", profile={"skills": [None, "x"]}), list)


def test_gaps_to_query():
    prof = _profile(skills=[{"name": "sql", "level": "begynder"}],
                    target_role="Data Analyst")
    gaps = C.compute_skill_gaps("u", profile=prof)
    q = C.gaps_to_query(gaps, limit=3)
    assert q and len(q.split()) <= 6  # multi-word skills allowed
