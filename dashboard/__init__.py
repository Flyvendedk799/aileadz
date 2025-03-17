from flask import Blueprint, render_template

dashboard_bp = Blueprint('dashboard', __name__, template_folder='templates')

@dashboard_bp.route('/dashboard')
def dashboard():
    # Render your dashboard page
    return render_template('dashboard.html')
