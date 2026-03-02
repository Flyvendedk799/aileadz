import logging
import os
from flask import Flask, redirect, url_for
from flask_mysqldb import MySQL

# Import blueprints from your modules
from dashboard import dashboard_bp
from app1 import app1_bp
from app2 import app2_bp
from app3 import app3_bp
from auth import auth_bp
from pages import pages_bp
from api import api_bp  # Import the API blueprint

logging.basicConfig(level=logging.INFO)

def create_app():
    app = Flask(__name__, template_folder='templates')
    app.secret_key = 'your_secret_key_here'
    
    app.config.update({
        'MYSQL_HOST': os.getenv('MYSQL_HOST', 'TobiasMastek.mysql.pythonanywhere-services.com'),
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
    app.register_blueprint(auth_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp)  # Register the API blueprint
    
    @app.route('/')
    def home():
        return redirect(url_for('dashboard.dashboard'))

    @app.errorhandler(404)
    def not_found(error):
        return redirect(url_for('dashboard.dashboard')), 404

    return app

if __name__ == '__main__':
    app = create_app()
    app.run(host='0.0.0.0', port=5000, debug=False)
