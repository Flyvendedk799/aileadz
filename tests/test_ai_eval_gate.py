"""T3-8: the eval gate reads a committed per-provider baseline and fails on a drop."""
import json

from ai_eval import run_eval


def test_committed_provider_baseline_is_preferred(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "BASELINES_DIR", tmp_path)
    (tmp_path / "openai.json").write_text("{}")
    assert run_eval.resolve_baseline("", "openai") == tmp_path / "openai.json"
    assert run_eval.resolve_baseline("", "anthropic") == run_eval.BASELINE_PATH
    assert str(run_eval.resolve_baseline("x.json", "openai")) == "x.json"


def test_gate_fails_on_a_drop_beyond_threshold(tmp_path):
    base = tmp_path / "b.json"
    base.write_text(json.dumps({"metrics": {"overall_pass_pct": 80.0, "tool_selection_pct": 90.0}}))
    ok, msgs = run_eval.run_gate({"metrics": {"overall_pass_pct": 70.0, "tool_selection_pct": 90.0}}, 5.0, base)
    assert not ok and any("REGRESSION overall_pass_pct" in m for m in msgs)
    ok, _ = run_eval.run_gate({"metrics": {"overall_pass_pct": 77.0, "tool_selection_pct": 92.0}}, 5.0, base)
    assert ok


def test_committed_baselines_are_valid_json():
    for path in run_eval.BASELINES_DIR.glob("*.json"):
        data = json.loads(path.read_text())
        assert "overall_pass_pct" in data["metrics"], path
