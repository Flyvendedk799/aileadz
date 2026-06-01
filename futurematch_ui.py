"""Futurematch redesign blueprint.

Serves the new Futurematch UI: the AI chat surface, the employee learning home,
and a design showcase that can render any converted page under templates/fm/.
The shared shell lives in templates/fm_base.html; individual pages extend it.
"""
import os
from flask import Blueprint, render_template, session, abort

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
