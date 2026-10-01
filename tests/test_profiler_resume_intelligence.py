"""Tests for smart profiler resume — verifying all 6 intelligence changes.

Offline: all database and model seams are mocked.
"""



# ── 1. Resume-aware profiler prompt ──

def test_profiler_playbook_contains_resume_awareness():
    """SYSTEM_PLAYBOOK_PROFILER treats the profile, memories and earlier
    conversations as established knowledge to build on — phrased as
    consultant guidance, not shouted prohibitions (which read as a checklist
    and, before the context assembler, never reached the model anyway)."""
    from app1.agent import SYSTEM_PLAYBOOK_PROFILER
    assert "DET DU ALLEREDE VED" in SYSTEM_PLAYBOOK_PROFILER
    assert "tidligere samtaler" in SYSTEM_PLAYBOOK_PROFILER
    assert "NÅR SAMTALEN ÅBNER ELLER GENOPTAGES" in SYSTEM_PLAYBOOK_PROFILER
    assert "SPØRG ALDRIG" not in SYSTEM_PLAYBOOK_PROFILER
    # Must also retain the career strategy section
    assert "SAMTALESTRATEGI" in SYSTEM_PLAYBOOK_PROFILER
    assert "target_role" in SYSTEM_PLAYBOOK_PROFILER


def test_profiler_playbook_retains_tool_usage_instructions():
    """The profiler prompt must still instruct the AI to save data immediately."""
    from app1.agent import SYSTEM_PLAYBOOK_PROFILER
    assert "update_user_profile" in SYSTEM_PLAYBOOK_PROFILER
    assert "remember_about_user" in SYSTEM_PLAYBOOK_PROFILER
    assert "request_user_input" in SYSTEM_PLAYBOOK_PROFILER


# ── 2. format_profile_for_ai enrichments ──

def test_format_profile_includes_experience_descriptions():
    """Experience descriptions should appear in the AI profile text."""
    from app1.user_profile_db import format_profile_for_ai
    profile = {
        "experience": [{
            "title": "Lager Team Lead",
            "company": "Nemlig.com",
            "start_year": 2019,
            "end_year": None,
            "is_current": True,
            "description": "Ansvarlig for daglig drift og teamledelse af 12 medarbejdere",
        }],
    }
    text = format_profile_for_ai(profile)
    assert "Lager Team Lead @ Nemlig.com" in text
    assert "Ansvarlig for daglig drift" in text


def test_format_profile_includes_education_descriptions():
    """Education descriptions should appear in the AI profile text."""
    from app1.user_profile_db import format_profile_for_ai
    profile = {
        "education": [{
            "degree": "BSc Datalogi",
            "institution": "Københavns Universitet",
            "year_completed": "2018",
            "description": "Specialiseret i machine learning og dataanalyse",
        }],
    }
    text = format_profile_for_ai(profile)
    assert "BSc Datalogi" in text
    assert "machine learning" in text


def test_format_profile_includes_active_learning_paths():
    """Active learning paths should appear in the AI profile text."""
    from app1.user_profile_db import format_profile_for_ai
    profile = {
        "learning_paths": [
            {"title": "Data Analyst path", "status": "aktiv"},
            {"title": "Old path", "status": "arkiveret"},  # should be excluded
        ],
    }
    text = format_profile_for_ai(profile)
    assert "Data Analyst path" in text
    assert "Læringsstier" in text
    assert "Old path" not in text  # archived should be excluded


def test_format_profile_truncates_long_descriptions():
    """Descriptions should be truncated to prevent context bloat."""
    from app1.user_profile_db import format_profile_for_ai
    long_desc = "A" * 300
    profile = {
        "experience": [{
            "title": "Dev", "company": "Co", "start_year": 2020,
            "end_year": None, "is_current": True, "description": long_desc,
        }],
    }
    text = format_profile_for_ai(profile)
    # Should be truncated to 120 chars
    assert "A" * 120 in text
    assert "A" * 121 not in text


def test_format_profile_handles_missing_description_gracefully():
    """Experience without descriptions should still work (no crash, no dash)."""
    from app1.user_profile_db import format_profile_for_ai
    profile = {
        "experience": [{
            "title": "Dev", "company": "Co", "start_year": 2020,
            "end_year": None, "is_current": True,
        }],
    }
    text = format_profile_for_ai(profile)
    assert "Dev @ Co" in text
    assert " — " not in text  # no dangling dash


# ── 3. Tool registry: profiler mode gets full toolset ──

def test_profiler_mode_seeds_full_toolset():
    """In profiler mode, all profile-related tools must be on the menu
    regardless of keyword matching."""
    from ai_tool_registry import get_employee_tool_selection
    _, meta = get_employee_tool_selection(
        logged_in=True,
        company_id=None,
        intent="chit_chat",  # would normally strip all tools
        user_query="hej",    # no profile keywords at all
        mode="profiler",
    )
    names = set(meta["tool_names"])
    # All of these should be present even with zero keyword matches
    # Note: analyze_skill_gaps requires company_id and is excluded here
    expected = {
        "get_user_profile", "update_user_profile", "request_user_input",
        "remember_about_user", "recommend_for_profile",
        "suggest_learning_path", "save_learning_path", "get_learning_path",
        "update_learning_path", "show_skill_gaps", "show_cv_summary",
        "set_learning_goal", "get_learning_goals", "update_learning_goal",
        "catalog_search",
    }
    missing = expected - names
    assert not missing, f"Profiler mode missing tools: {missing}"


def test_default_mode_does_not_seed_profiler_tools():
    """In default (chat) mode, a bare greeting should NOT seed profiler tools."""
    from ai_tool_registry import get_employee_tool_selection
    tools, meta = get_employee_tool_selection(
        logged_in=True,
        company_id=None,
        intent="chit_chat",
        user_query="hej",
        mode="default",
    )
    # chit_chat with just "hej" should return empty tools (fast-path)
    assert tools == []


def test_tool_selection_mode_parameter_is_optional():
    """The mode parameter should default to 'default' for backward compatibility."""
    from ai_tool_registry import get_employee_tool_selection
    # Should not raise — mode defaults to "default"
    _, meta = get_employee_tool_selection(
        logged_in=True,
        company_id=None,
        intent="discovery",
        user_query="find et kursus",
    )
    assert meta["tool_names"]


# ── 4. Session init no longer duplicates profile into CHAT_MEMORY ──

def test_session_init_does_not_duplicate_profile():
    """After session init, CHAT_MEMORY should NOT contain a system message
    with 'BRUGERENS NUVÆRENDE PROFIL' — the per-turn ephemeral injection
    handles profile context instead."""
    import app1.agent as agent_mod
    # Read the source to verify the old pattern is gone
    import inspect
    source = inspect.getsource(agent_mod)
    # The old injection text should no longer be present
    assert "BRUGERENS NUVÆRENDE PROFIL" not in source, \
        "Static profile injection into CHAT_MEMORY should be removed (ephemeral handles it)"


# ── 5. Profiler context includes "ALLEREDE AFDÆKKET" block ──

def test_profiler_prompt_uses_need_driven_state_not_covered_checklist():
    """The duplicated "ALLEREDE AFDÆKKET — spørg IKKE om dette igen" checklist
    (and the contradictory "DIT NÆSTE SPØRGSMÅL SKAL VÆRE OM …" line) are gone:
    the profile layer carries what is known, _build_profiler_state says which
    unknowns would sharpen the advice and why."""
    import inspect
    import app1.agent as agent_mod
    source = inspect.getsource(agent_mod)
    assert "spørg IKKE om dette igen" not in source
    assert "DIT NÆSTE SPØRGSMÅL SKAL VÆRE OM" not in source
    assert "_build_profiler_state(_profiler_completeness" in source


# ── 6. Chat.js mode-awareness contracts ──

def test_chatjs_has_mode_aware_boot():
    """Opening a surface starts a NEW conversation on that surface — the server
    session is reset too (/new_session with the surface's mode), so a blank
    thread can never be secretly continuing an old one. Only an explicit
    ?c=<id> reopens a past conversation."""
    source = open("static/futurematch/assets/chat.js", encoding="utf-8").read()
    assert "CHAT_MODE" in source
    assert "body: JSON.stringify({ mode: (window.CHAT_MODE" in source
    assert "const mismatch" not in source
    assert "restoreActiveConversation" not in source
    boot = source[source.index("function bootChat()"):source.index("window.fmChatBoot = bootChat();")]
    assert "openConversation(cid)" in boot
    assert "newChat()" in boot


def test_chatjs_intent_link_sends_into_a_fresh_chat():
    """?intent= deep links (profile page, Mind-Map) land in a new conversation
    and the param is stripped first, so a reload never re-sends it."""
    source = open("static/futurematch/assets/chat.js", encoding="utf-8").read()
    boot = source[source.index("function bootChat()"):source.index("window.fmChatBoot = bootChat();")]
    assert boot.index('params.delete("intent")') < boot.index("return newChat().then(")
