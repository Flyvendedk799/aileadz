"""T0-3: Systemstatus shows the nightly AI eval and flags a regression or a stopped job."""
import json
from datetime import datetime, timezone

import eval_status

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
TS = int(datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc).timestamp())


def _write(root, provider, **extra):
    d = {"timestamp": TS, "cases_total": 89, "cases_passed": 60, "metrics": {"overall_pass_pct": 67.4},
         "commit": "abcdef123", "exit_code": 0, "gate": {"ok": True, "messages": ["gate PASSED"]}}
    d.update(extra)
    (root / ("latest-%s.json" % provider)).write_text(json.dumps(d))


def test_unconfigured(monkeypatch):
    monkeypatch.delenv("AI_EVAL_DIR", raising=False)
    assert eval_status.status(NOW) == []


def test_ok_regression_and_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_EVAL_DIR", str(tmp_path))
    _write(tmp_path, "openai")
    _write(tmp_path, "anthropic", exit_code=1,
           gate={"ok": False, "messages": ["REGRESSION overall_pass_pct: 70 -> 50"]})
    (tmp_path / "latest-broken.json").write_text("{not json")
    rows = {r["provider"]: r for r in eval_status.status(NOW)}
    assert set(rows) == {"openai", "anthropic"}
    assert rows["openai"]["state"] == "ok" and rows["openai"]["passed"] == 60 and rows["openai"]["commit"] == "abcdef1"
    assert rows["anthropic"]["state"] == "regression" and rows["anthropic"]["messages"]
    later = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    assert {r["state"] for r in eval_status.status(later)} == {"stale"}


def test_status_cards_render_with_data(tmp_path, monkeypatch):
    """Both Systemstatus cards render with real values, not only the empty state."""
    from flask import render_template
    from tests.sqlite_platform import PlatformDB, make_app

    import backup_status
    bdir = tmp_path / "b"
    bdir.mkdir()
    (bdir / "20261120T013000Z").mkdir()
    (bdir / "20261120T013000Z" / "MANIFEST").write_text("db_bytes=3649310\n")
    monkeypatch.setenv("BACKUP_DIR", str(bdir))
    monkeypatch.setenv("AI_EVAL_DIR", str(tmp_path))
    _write(tmp_path, "openai", exit_code=1, gate={"ok": False, "messages": ["REGRESSION overall_pass_pct: 70 -> 50"]})
    app = make_app(PlatformDB())
    with app.test_request_context("/admin/system-health"):
        from flask import session
        session.update(user="admin", role="admin")
        html = render_template("fm/admin_system_health.html", features={}, outbox={}, snapshot={}, mail={},
                               jobs=[], backup=backup_status.status(NOW), ai_eval=eval_status.status(NOW))
    assert "20.11.2026 01:30 UTC" in html and "3.5 MB" in html
    assert "60/89 testsamtaler" in html and "REGRESSION overall_pass_pct" in html and "Kræver opmærksomhed" in html
