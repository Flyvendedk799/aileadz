import logging
import sshtunnel
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

logging.basicConfig(level=logging.INFO)

def create_app():
    app = Flask(__name__, template_folder='templates')
    app.secret_key = 'your_secret_key_here'
    
    app.config.update({
        'MYSQL_HOST': '127.0.0.1',
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
    
    @app.route('/')
    def home():
        return redirect(url_for('dashboard.dashboard'))

    @app.errorhandler(404)
    def not_found(error):
        return redirect(url_for('dashboard.dashboard')), 404

    return app

def main():
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
