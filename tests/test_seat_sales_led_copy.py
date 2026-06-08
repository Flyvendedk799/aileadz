"""Sales-led seat/trial copy reframe (plan #2).

aileadz is a sales-led B2B product: there is no self-serve upgrade flow. When a
trial expires or seats run out the only correct next step is to contact the
account manager ("kundeansvarlige"), NEVER an "Opgradér dit abonnement"
self-serve affordance.

These tests lock in three invariants:
  1. seat_governance.can_add_employee block messages route to the account
     manager and carry no self-serve upgrade framing.
  2. The add_employee.html seat callout copy is sales-led, not "opgradér".
  3. The hr_trial_and_seat_status AI tool returns a sales-led next_step and no
     self-serve upgrade affordance, so nothing upsell-y leaks via the advisor.

They run without a Flask boot or a live DB by stubbing the cursor/session the
two backend helpers reach for.
"""
import json
import sys
import types
import importlib

import pytest


# A token that must never appear in seat/trial user-facing copy on the HR
# surface — it is the dead self-serve affordance we removed.
_FORBIDDEN = ("opgradér dit abonnement", "opgradér for at", "opgrader dit abonnement")
_SALES_LED = "kundeansvarlige"


# ---------------------------------------------------------------------------
# 1) seat_governance.can_add_employee block messages
# ---------------------------------------------------------------------------

class _FakeCursor:
    """Minimal dict-cursor returning one canned company row."""

    def __init__(self, row):
        self._row = row

    def execute(self, *a, **k):
        return None

    def fetchone(self):
        return self._row

    def close(self):
        return None


def _seat_governance_with_row(monkeypatch, row):
    import seat_governance
    importlib.reload(seat_governance)
    monkeypatch.setattr(seat_governance, "_get_cursor", lambda: _FakeCursor(None))
    monkeypatch.setattr(seat_governance, "_load_company_seat_row", lambda cur, cid: row)
    return seat_governance


def test_trial_expired_message_is_sales_led(monkeypatch):
    import datetime
    sg = _seat_governance_with_row(
        monkeypatch,
        {
            "plan": "trial",
            "trial_ends_at": datetime.datetime(2000, 1, 1),
            "max_employees": 50,
            "seats_used": 1,
        },
    )
    ok, reason = sg.can_add_employee(123)
    assert ok is False
    low = reason.lower()
    assert _SALES_LED in low, reason
    for bad in _FORBIDDEN:
        assert bad not in low, f"trial-expired copy still self-serve: {reason!r}"


def test_seat_limit_message_is_sales_led(monkeypatch):
    sg = _seat_governance_with_row(
        monkeypatch,
        {
            "plan": "active",
            "trial_ends_at": None,
            "max_employees": 10,
            "seats_used": 10,
        },
    )
    ok, reason = sg.can_add_employee(123)
    assert ok is False
    low = reason.lower()
    assert _SALES_LED in low, reason
    for bad in _FORBIDDEN:
        assert bad not in low, f"seat-limit copy still self-serve: {reason!r}"


# ---------------------------------------------------------------------------
# 2) add_employee.html seat callout copy
# ---------------------------------------------------------------------------

def test_add_employee_template_copy_is_sales_led():
    src = open("templates/fm/add_employee.html", encoding="utf-8").read().lower()
    assert _SALES_LED in src, "seat callout no longer routes to the account manager"
    for bad in _FORBIDDEN:
        assert bad not in src, f"add_employee.html still has self-serve copy: {bad!r}"


# ---------------------------------------------------------------------------
# 3) hr_trial_and_seat_status AI tool output
# ---------------------------------------------------------------------------

class _ToolCursor:
    """Cursor that answers the company SELECT then the live-count SELECT."""

    def __init__(self, company_row, live_count):
        self._company_row = company_row
        self._live_count = live_count
        self._calls = 0

    def execute(self, *a, **k):
        self._calls += 1

    def fetchone(self):
        if self._calls <= 1:
            return self._company_row
        return {"cnt": self._live_count}

    def close(self):
        return None


def _run_trial_tool(monkeypatch, company_row, live_count):
    import hr_tools
    monkeypatch.setattr(hr_tools, "session", {"company_id": 1})
    monkeypatch.setattr(hr_tools, "_get_cursor", lambda: _ToolCursor(company_row, live_count))
    out = hr_tools._execute_hr_trial_and_seat_status({})
    return json.loads(out)


def test_trial_tool_seats_full_routes_to_account_manager(monkeypatch):
    data = _run_trial_tool(
        monkeypatch,
        {
            "company_name": "Acme",
            "subscription_plan": "active",
            "trial_ends_at": None,
            "max_employees": 5,
            "current_employee_count": 5,
            "status": "active",
        },
        live_count=5,
    )
    assert data["seats_full"] is True
    blob = json.dumps(data, ensure_ascii=False).lower()
    assert _SALES_LED in blob, data
    for bad in _FORBIDDEN:
        assert bad not in blob, f"trial tool leaks self-serve affordance: {bad!r}"


def test_trial_tool_has_room_still_sales_led(monkeypatch):
    data = _run_trial_tool(
        monkeypatch,
        {
            "company_name": "Acme",
            "subscription_plan": "active",
            "trial_ends_at": None,
            "max_employees": 50,
            "current_employee_count": 3,
            "status": "active",
        },
        live_count=3,
    )
    assert data["seats_full"] is False
    blob = json.dumps(data, ensure_ascii=False).lower()
    # next_step is present and sales-led even when there's room (no upsell).
    assert _SALES_LED in blob, data
    for bad in _FORBIDDEN:
        assert bad not in blob, f"trial tool leaks self-serve affordance: {bad!r}"
