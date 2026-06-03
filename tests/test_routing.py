import os, importlib

def _fresh():
    import ai_runtime
    importlib.reload(ai_runtime)
    return ai_runtime

def _mode(m):
    os.environ['AI_MODEL_ROUTING'] = m

def test_quality_mode_keeps_4o_for_substantive_turns():
    _mode('quality'); R = _fresh()
    # a substantive search turn with tools should stay on the main (4o) model
    assert R.choose_turn_model(intent='search', tool_count=1, token_estimate=2000) == R.main_model()

def test_balanced_routes_simple_to_mini():
    _mode('balanced'); R = _fresh()
    assert R.choose_turn_model(intent='search', tool_count=1, token_estimate=2000) == R.fast_model()
    assert R.choose_turn_model(intent='greeting', tool_count=0, token_estimate=200) == R.fast_model()

def test_balanced_keeps_4o_for_synthesis():
    _mode('balanced'); R = _fresh()
    assert R.choose_turn_model(intent='compare', tool_count=0, token_estimate=4000) == R.main_model()
    assert R.choose_turn_model(intent='learning_path', tool_count=0, token_estimate=4000) == R.main_model()

def test_cost_mode_minis_almost_everything():
    _mode('cost'); R = _fresh()
    assert R.choose_turn_model(intent='recommend_for_profile', tool_count=1, token_estimate=3000) == R.fast_model()
    # but the must-be-4o allowlist still escalates
    assert R.choose_turn_model(intent='compare', tool_count=0, token_estimate=3000) == R.main_model()

def test_prefer_quality_forces_4o():
    _mode('balanced'); R = _fresh()
    assert R.choose_turn_model(intent='search', tool_count=1, token_estimate=2000, prefer_quality=True) == R.main_model()


def test_profile_mutations_stay_on_4o_in_all_cost_modes():
    for mode in ('balanced', 'cost'):
        _mode(mode); R = _fresh()
        assert R.choose_turn_model(intent='profile_update', tool_count=1, token_estimate=2000) == R.main_model()
        assert R.choose_turn_model(intent='profile_add', tool_count=1, token_estimate=2000) == R.main_model()

def teardown_module(_m):
    os.environ.pop('AI_MODEL_ROUTING', None)
