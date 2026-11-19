"""T3-9: a single assistant run has a cost and token ceiling by default."""
import ai_runtime


def test_defaults_are_on(monkeypatch):
    monkeypatch.delenv("AI_MAX_RUN_COST", raising=False)
    monkeypatch.delenv("AI_MAX_RUN_TOKENS", raising=False)
    assert ai_runtime.max_run_cost_usd() == ai_runtime.DEFAULT_MAX_RUN_COST_USD > 0
    assert ai_runtime.max_run_tokens() == ai_runtime.DEFAULT_MAX_RUN_TOKENS > 0


def test_zero_still_disables(monkeypatch):
    monkeypatch.setenv("AI_MAX_RUN_COST", "0")
    monkeypatch.setenv("AI_MAX_RUN_TOKENS", "0")
    assert not ai_runtime._run_cost_exceeded({"input_tokens": 10_000_000, "output_tokens": 1})


def test_bad_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("AI_MAX_RUN_COST", "lots")
    monkeypatch.setenv("AI_MAX_RUN_TOKENS", "many")
    assert ai_runtime.max_run_cost_usd() == ai_runtime.DEFAULT_MAX_RUN_COST_USD
    assert ai_runtime.max_run_tokens() == ai_runtime.DEFAULT_MAX_RUN_TOKENS


def test_a_normal_long_run_is_not_cut(monkeypatch):
    monkeypatch.delenv("AI_MAX_RUN_COST", raising=False)
    monkeypatch.delenv("AI_MAX_RUN_TOKENS", raising=False)
    # Four model calls at the production per-call input cap (20k) plus a long answer.
    usage = {"input_tokens": 80_000, "output_tokens": 2_000}
    assert not ai_runtime._run_cost_exceeded(usage, model="gpt-6.1-terra")
    assert not ai_runtime._run_cost_exceeded(usage, model="claude-sonnet-5")


def test_a_runaway_run_is_stopped(monkeypatch):
    monkeypatch.delenv("AI_MAX_RUN_COST", raising=False)
    monkeypatch.delenv("AI_MAX_RUN_TOKENS", raising=False)
    assert ai_runtime._run_cost_exceeded({"input_tokens": 250_000, "output_tokens": 0}, model="gpt-6-luna")
    assert ai_runtime._run_cost_exceeded({"input_tokens": 150_000, "output_tokens": 5_000}, model="claude-opus-5")
