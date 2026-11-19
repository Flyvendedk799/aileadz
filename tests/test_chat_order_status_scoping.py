"""The chat order-status lookup must never read course_orders unscoped.

``OrderHandler.get_order_status`` used a bare ``WHERE order_id = %s``; it now
delegates to ``order_service.get_order``, which returns a row only when the
caller plays a role on the order.
"""
import inspect

from flask import Flask

import order_service
from app1 import order_handler


def _app():
    app = Flask(__name__)
    app.secret_key = "test"
    return app


def test_lookup_delegates_to_the_scoped_order_service(monkeypatch):
    seen = {}

    def fake_get_order(ctx, order_id):
        seen["ctx"], seen["order_id"] = ctx, order_id
        return None  # the service refuses: not the caller's order

    monkeypatch.setattr(order_service, "get_order", fake_get_order)
    app = _app()
    with app.test_request_context():
        from flask import session
        session["company_id"] = 7
        session["user_id"] = 42
        assert order_handler.order_handler.get_order_status("abc123") is None
    assert seen["order_id"] == "abc123"
    assert seen["ctx"].company_id == 7 and seen["ctx"].user_id == 42


def test_handler_has_no_raw_course_orders_select():
    src = inspect.getsource(order_handler.OrderHandler.get_order_status)
    assert "FROM course_orders" not in src
