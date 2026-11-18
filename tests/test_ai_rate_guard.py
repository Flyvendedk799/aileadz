"""The assistant endpoints have a per-actor request brake besides credits."""
import rate_limit


def setup_function(_):
    rate_limit.reset()


def test_short_window_blocks_then_recovers(monkeypatch):
    monkeypatch.setenv("AI_RATE_TURNS_5MIN", "3")
    monkeypatch.setenv("AI_RATE_TURNS_HOUR", "0")
    assert [rate_limit.ai_turn_allowed("user:a", now=t) for t in (0, 1, 2, 3)] == [True, True, True, False]
    assert rate_limit.ai_turn_allowed("user:b", now=3), "another actor is unaffected"
    assert rate_limit.ai_turn_allowed("user:a", now=400), "the window slides"


def test_hour_limit_and_off_switch(monkeypatch):
    monkeypatch.setenv("AI_RATE_TURNS_5MIN", "0")
    monkeypatch.setenv("AI_RATE_TURNS_HOUR", "2")
    assert [rate_limit.ai_turn_allowed("user:a", now=t) for t in (0, 600, 1200)] == [True, True, False]
    monkeypatch.setenv("AI_RATE_TURNS_HOUR", "0")
    assert all(rate_limit.ai_turn_allowed("user:c", now=t) for t in range(500))


def test_the_three_endpoints_use_it():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in ("app1/__init__.py", "hr_dashboard/__init__.py", "vendor_portal.py"):
        assert "rate_limit.ai_turn_allowed(" in open(os.path.join(root, path), encoding="utf-8").read(), path
