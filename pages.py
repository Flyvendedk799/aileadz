# pages.py
from flask import Blueprint, render_template

pages_bp = Blueprint('pages', __name__, template_folder='templates')

@pages_bp.route('/about')
def about():
    return render_template('about.html')

@pages_bp.route('/contact')
def contact():
    return render_template('contact.html')

@pages_bp.route('/analytics')
def analytics():
    return render_template('analytics.html')

@pages_bp.route('/notifications')
def notifications():
    return render_template('notifications.html')

@pages_bp.route('/indstillinger')  # Changed route from '/settings' to '/indstillinger'
def settings():
    return render_template('indstillinger.html')

@pages_bp.route('/profile')
def profile():
    return render_template('profile.html')
