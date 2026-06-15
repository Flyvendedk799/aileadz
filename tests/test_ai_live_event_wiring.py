"""Guard that every AI assistant surface uses the shared live tool-event path."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relpath):
    return (ROOT / relpath).read_text(encoding="utf-8")


def test_employee_hr_and_vendor_agents_use_shared_live_runner():
    for relpath in ("app1/agent.py", "hr_agent.py", "vendor_portal.py"):
        source = _read(relpath)
        assert "iter_agent_with_live_tool_events" in source
        assert "live_tool_events_enabled" in source


def test_hr_and_vendor_tool_chips_upgrade_live_start_events_in_place():
    for relpath, cls in (
        ("templates/fm/chatbot.html", "data-call-id"),
        ("templates/fm/vendor_dashboard.html", "data-call-id"),
    ):
        source = _read(relpath)
        assert "data.phase === 'start'" in source
        assert cls in source
        assert "running" in source
