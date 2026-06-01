import logging
import os
import tempfile
import time
try:
    import sshtunnel
except ImportError:
    sshtunnel = None
try:
    import MySQLdb  # noqa: F401
except ImportError:
    try:
        import pymysql
        pymysql.install_as_MySQLdb()
    except ImportError:
        pass
from flask import Flask, current_app, g, redirect, url_for
try:
    from flask_mysqldb import MySQL
except ImportError:
    import pymysql

    class _PyMySQLConnection:
        def __init__(self, connection):
            self._connection = connection

        def cursor(self, *args, **kwargs):
            if kwargs.pop('dictionary', False):
                args = (pymysql.cursors.DictCursor,)
            return self._connection.cursor(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    class MySQL:
        def __init__(self, app=None):
            self.app = None
            if app is not None:
                self.init_app(app)

        def init_app(self, app):
            self.app = app
            app.teardown_appcontext(self._close_connection)

        @property
        def connection(self):
            conn = getattr(g, '_futurematch_mysql_connection', None)
            if conn is None or not getattr(conn._connection, 'open', True):
                config = current_app.config
                cursorclass = None
                if config.get('MYSQL_CURSORCLASS') == 'DictCursor':
                    cursorclass = pymysql.cursors.DictCursor
                kwargs = {
                    'host': config.get('MYSQL_HOST'),
                    'user': config.get('MYSQL_USER'),
                    'password': config.get('MYSQL_PASSWORD'),
                    'database': config.get('MYSQL_DB'),
                    'charset': config.get('MYSQL_CHARSET', 'utf8mb4'),
                    'autocommit': False,
                }
                if config.get('MYSQL_PORT'):
                    kwargs['port'] = int(config['MYSQL_PORT'])
                if cursorclass:
                    kwargs['cursorclass'] = cursorclass
                conn = _PyMySQLConnection(pymysql.connect(**kwargs))
                g._futurematch_mysql_connection = conn
            return conn

        def _close_connection(self, exception=None):
            conn = getattr(g, '_futurematch_mysql_connection', None)
            if conn is not None:
                conn.close()

# Import blueprints from your modules
from dashboard import dashboard_bp
from app1 import app1_bp
from app2 import app2_bp
from app3 import app3_bp
from app4 import app4_bp  # Added for app4

from auth import auth_bp
from pages import pages_bp
from catalog_routes import catalog_bp
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
from futurematch_ui import futurematch_bp

logging.basicConfig(level=logging.INFO)


def _enterprise_sync_stamp_path():
    return os.path.join(tempfile.gettempdir(), "futurematch_enterprise_tables_ensured")


def _recent_enterprise_sync_exists():
    if os.environ.get("ENTERPRISE_TABLE_SYNC_FORCE") == "1":
        return False
    try:
        ttl = int(os.environ.get("ENTERPRISE_TABLE_SYNC_TTL_SECONDS", "21600"))
    except ValueError:
        ttl = 21600
    if ttl <= 0:
        return False
    try:
        return time.time() - os.path.getmtime(_enterprise_sync_stamp_path()) < ttl
    except OSError:
        return False


def _mark_enterprise_sync_done():
    try:
        with open(_enterprise_sync_stamp_path(), "w", encoding="utf-8") as stamp:
            stamp.write(str(int(time.time())))
    except OSError:
        pass

def create_app():
    app = Flask(__name__, template_folder='templates')
    app.secret_key = 'your_secret_key_here'
    
    app.config.update({
        'MYSQL_HOST': 'TobiasMastek.mysql.pythonanywhere-services.com',
        'MYSQL_USER': 'TobiasMastek',
        'MYSQL_PASSWORD': 'Jht89ryu1!',
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
    app.register_blueprint(catalog_bp)
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
    app.register_blueprint(futurematch_bp)

    # Initialize white-label context processor
    try:
        from white_label_global_integration import register_white_label_context_processor
        register_white_label_context_processor(app)
    except Exception as e:
        logging.warning("White-label integration skipped: %s", e)

    # Branding schema migration runs every process start (not gated by enterprise sync TTL)
    @app.before_request
    def _warm_ai_subsystems_once():
        if getattr(app, '_ai_subsystems_warmed', False):
            return
        app._ai_subsystems_warmed = True
        try:
            from ai_context import warm_ai_subsystems
            stats = warm_ai_subsystems()
            logging.info("AI subsystems warmed: %s", stats)
        except Exception as e:
            logging.warning("AI warmup skipped: %s", e)

    if os.getenv("AI_WARMUP_ON_IMPORT", "1").lower() not in {"0", "false", "no", "off"}:
        try:
            from ai_context import warm_ai_subsystems
            stats = warm_ai_subsystems()
            logging.info("AI subsystems warmed at import: %s", stats)
            app._ai_subsystems_warmed = True
        except Exception as e:
            logging.warning("AI import warmup skipped: %s", e)

    @app.before_request
    def _ensure_branding_schema_once():
        if getattr(app, '_branding_schema_ensured', False):
            return
        app._branding_schema_ensured = True
        try:
            from branding_service import ensure_branding_schema, migrate_legacy_branding_data
            ensure_branding_schema(app)
            migrate_legacy_branding_data(app)
        except Exception as e:
            logging.warning("Branding schema init: %s", e)

    # Create enterprise tables on first request
    @app.before_request
    def _ensure_enterprise_tables_once():
        if not getattr(app, '_enterprise_tables_created', False):
            app._enterprise_tables_created = True  # set early to prevent concurrent runs
            if _recent_enterprise_sync_exists():
                return
            try:
                from enterprise_tables import ensure_enterprise_tables
                ensure_enterprise_tables(app)
                _mark_enterprise_sync_done()
                try:
                    from branding_service import ensure_branding_schema, migrate_legacy_branding_data
                    ensure_branding_schema(app)
                    migrate_legacy_branding_data(app)
                except Exception as mig_err:
                    logging.warning("Branding migration: %s", mig_err)
            except Exception as e:
                logging.warning("Enterprise table init: %s", e)

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
