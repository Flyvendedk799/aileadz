"""Cross-module reactions to domain events (subscribers of ``event_bus``).

Contract: each reaction is a function ``(company_id, event_type, payload)``
registered in ``register()``. It runs after the emitting transaction has
committed, opens its own cursor on the request connection, scopes every query
by ``company_id`` and commits its own work. ``event_bus`` isolates failures: a
reaction that raises is logged and rolled back and never reaches the emitter.

Add a reaction here instead of editing the service that emits the event.
"""
import logging

import event_bus

logger = logging.getLogger(__name__)


def _cursor():
    from flask import current_app
    import MySQLdb.cursors
    conn = current_app.mysql.connection
    return conn, conn.cursor(MySQLdb.cursors.DictCursor)


def course_completed_moves_development_goals(company_id, event_type, payload):
    """A completed course ticks the development-goal steps linked to its order
    and tells the learner, with a way back to the development workspace."""
    user_id, order_id = payload.get("user_id"), payload.get("order_id")
    if not user_id or not order_id:
        return
    import talent_service
    from notification_service import notify_user
    conn, cur = _cursor()
    try:
        for goal_title in talent_service.complete_course_steps(cur, company_id, int(user_id), order_id):
            notify_user(
                cur, user_id=int(user_id), company_id=company_id, kind="success",
                title="Et skridt nærmere dit udviklingsmål",
                message='"%s" er gennemført og tæller nu med i dit mål "%s".' % (
                    payload.get("product_title") or "Kurset", goal_title),
                action_url="/udvikling",
                dedupe_key="talent-goal-step:%s" % order_id)
        conn.commit()
    finally:
        cur.close()


def register():
    """Idempotent; called once from ``run.create_app``."""
    event_bus.subscribe("course.completed", course_completed_moves_development_goals)
