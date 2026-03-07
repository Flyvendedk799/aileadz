import logging
try:
    import sshtunnel
except ImportError:
    sshtunnel = None
from flask import Flask, redirect, url_for
from flask_mysqldb import MySQL

# Import blueprints from your modules
from dashboard import dashboard_bp
from app1 import app1_bp
from app2 import app2_bp
from app3 import app3_bp
from app4 import app4_bp  # Added for app4

from auth import auth_bp
from pages import pages_bp
from api import api_bp  # Import the API blueprint
from admin_notifications import admin_notifications_bp
from admin_dashboard import admin_dashboard_bp
from reports import reports_bp
from admin_reports import admin_reports_bp

# Enterprise / B2B modules
from companies import companies_bp
from hr_dashboard import hr_dashboard_bp
from enterprise_analytics import analytics_bp
from enterprise_api import api_enterprise_bp
from enterprise_sso import sso_bp
from enterprise_company_settings import enterprise_settings_bp
from multitenant_reports import multitenant_reports_bp

logging.basicConfig(level=logging.INFO)

def create_app():
    app = Flask(__name__, template_folder='templates')
    app.secret_key = 'your_secret_key_here'
    
    app.config.update({
        'MYSQL_HOST': 'TobiasMastek.mysql.pythonanywhere-services.com',
        'MYSQL_USER': 'TobiasMastek',
        'MYSQL_PASSWORD': 'Jht89ryu1!!',
        'MYSQL_DB': 'TobiasMastek$AiLead',
        'MYSQL_CURSORCLASS': 'DictCursor'
    })
    
    mysql = MySQL(app)
    app.mysql = mysql

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(app1_bp, url_prefix='/app1')
    app.register_blueprint(app2_bp, url_prefix='/app2')
    app.register_blueprint(app3_bp, url_prefix='/app3')
    app.register_blueprint(app4_bp, url_prefix='/app4')  # Register app4 blueprint
    app.register_blueprint(auth_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp)  # Register the API blueprint
    app.register_blueprint(admin_notifications_bp, url_prefix='/admin')  # Register admin notifications
    app.register_blueprint(admin_dashboard_bp, url_prefix='/admin')
    app.register_blueprint(reports_bp, url_prefix='/reports')
    app.register_blueprint(admin_reports_bp)

    # Enterprise / B2B blueprints
    app.register_blueprint(companies_bp, url_prefix='/companies')
    app.register_blueprint(hr_dashboard_bp, url_prefix='/hr')
    app.register_blueprint(analytics_bp)
    app.register_blueprint(api_enterprise_bp)
    app.register_blueprint(sso_bp)
    app.register_blueprint(enterprise_settings_bp, url_prefix='/enterprise')
    app.register_blueprint(multitenant_reports_bp, url_prefix='/multitenant-reports')

    # Initialize white-label context processor
    try:
        from white_label_global_integration import register_white_label_context_processor
        register_white_label_context_processor(app)
    except Exception as e:
        logging.warning("White-label integration skipped: %s", e)

    # Create enterprise tables on first request
    @app.before_request
    def _ensure_enterprise_tables_once():
        if not getattr(app, '_enterprise_tables_created', False):
            try:
                from enterprise_tables import ensure_enterprise_tables
                ensure_enterprise_tables(app)
                app._enterprise_tables_created = True
            except Exception as e:
                logging.warning("Enterprise table init: %s", e)
                app._enterprise_tables_created = True  # don't retry

    @app.route('/')
    def home():
        return redirect(url_for('dashboard.dashboard'))

    @app.errorhandler(404)
    def not_found(error):
        return redirect(url_for('dashboard.dashboard')), 404

    return app

def main():
    if sshtunnel is None:
        logging.error("sshtunnel is not installed. It is required for local development.")
        return

    tunnel = None
    try:
        tunnel = sshtunnel.SSHTunnelForwarder(
            ('ssh.pythonanywhere.com', 22),
            ssh_username='TobiasMastek',
            ssh_password='Jht89ryu1!',
            remote_bind_address=('TobiasMastek.mysql.pythonanywhere-services.com', 3306)
        )
        tunnel.start()
        logging.info("SSH tunnel established on local port: %s", tunnel.local_bind_port)
        
        app = create_app()
        app.config['MYSQL_HOST'] = '127.0.0.1'
        app.config['MYSQL_PORT'] = tunnel.local_bind_port
        app.run(host='0.0.0.0', port=5000, debug=False)
    except Exception as e:
        logging.error("Application failed to start: %s", e)
    finally:
        if tunnel:
            tunnel.stop()
            logging.info("SSH tunnel closed.")

if __name__ == '__main__':
    main()
