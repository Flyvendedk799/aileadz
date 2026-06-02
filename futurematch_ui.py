"""Futurematch redesign blueprint.

Serves the new Futurematch UI: the AI chat surface, the employee learning home,
and a design showcase that can render any converted page under templates/fm/.
The shared shell lives in templates/fm_base.html; individual pages extend it.
"""
import os
from flask import (Blueprint, render_template, session, abort, redirect,
                   url_for, request, flash, current_app)

futurematch_bp = Blueprint('futurematch', __name__, template_folder='templates')

_FM_DIR = os.path.join(os.path.dirname(__file__), 'templates', 'fm')


def _fm_pages():
    try:
        return {f[:-5] for f in os.listdir(_FM_DIR) if f.endswith('.html')}
    except OSError:
        return set()


@futurematch_bp.route('/chat')
def chat():
    """AI assistant chat surface (standalone shell with chat.js)."""
    return render_template('fm/chat.html')


@futurematch_bp.route('/min-laering')
def employee_home():
    """Employee learning home."""
    return render_template('fm/employee_home.html')


@futurematch_bp.route('/mine-maal')
def learning_goals():
    """Development-goals dashboard for the logged-in user."""
    if not session.get('user'):
        flash('Log ind for at se dine udviklingsmål.', 'danger')
        return redirect(url_for('auth.login'))
    goals = []
    try:
        from app1.user_profile_db import get_learning_goals, ensure_tables
        ensure_tables()
        goals = get_learning_goals(session['user'])
    except Exception as e:
        current_app.logger.warning("learning goals load: %s", e)
    return render_template('fm/learning_goals.html', goals=goals)


@futurematch_bp.route('/mine-maal/add', methods=['POST'])
def learning_goal_add():
    if not session.get('user'):
        return redirect(url_for('auth.login'))
    title = (request.form.get('title') or '').strip()
    if title:
        try:
            from app1.user_profile_db import add_learning_goal, ensure_tables
            ensure_tables()
            add_learning_goal(session['user'], title, request.form.get('description', ''), request.form.get('target_date'))
            flash('Udviklingsmål oprettet.', 'success')
        except Exception as e:
            current_app.logger.warning("goal add: %s", e)
            flash('Kunne ikke oprette målet.', 'danger')
    return redirect(url_for('futurematch.learning_goals'))


@futurematch_bp.route('/mine-maal/<int:goal_id>/status', methods=['POST'])
def learning_goal_status(goal_id):
    if not session.get('user'):
        return redirect(url_for('auth.login'))
    action = request.form.get('action')
    try:
        from app1.user_profile_db import update_learning_goal, delete_learning_goal, ensure_tables
        ensure_tables()
        if action == 'slet':
            delete_learning_goal(session['user'], goal_id)
        elif action in ('aktiv', 'fuldfoert', 'paa_pause'):
            update_learning_goal(session['user'], goal_id, status=action)
    except Exception as e:
        current_app.logger.warning("goal status: %s", e)
    return redirect(url_for('futurematch.learning_goals'))


@futurematch_bp.route('/ui')
def showcase_index():
    """Gallery of every Futurematch design page (for review / navigation)."""
    pages = sorted(_fm_pages())
    return render_template('fm/_showcase_index.html', pages=pages)


@futurematch_bp.route('/ui/<page>')
def showcase(page):
    """Render any converted Futurematch page by name."""
    if page not in _fm_pages() or page.startswith('_'):
        abort(404)
    return render_template(f'fm/{page}.html')
