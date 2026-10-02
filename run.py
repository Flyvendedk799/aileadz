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
    # Keyed on the DDL fingerprint: a changed table definition invalidates the
    # stamp, so new columns apply on the next boot instead of after the TTL (N-3.4).
    try:
        from enterprise_tables import ddl_fingerprint
        suffix = "_" + ddl_fingerprint()
    except Exception:
        suffix = ""
    return os.path.join(tempfile.gettempdir(), "futurematch_enterprise_tables_ensured" + suffix)


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


def _mysql_settings_from_database_url(url):
    """Split a mysql://user:pass@host[:port]/db URL into MYSQL_* pieces.

    ServerHoster (and most PaaS hosts) hand the app a single DATABASE_URL for
    the linked managed database; this app has always been configured with
    separate MYSQL_* keys. Returns {} for an empty / non-mysql URL so the caller
    can fall through to the other sources. Never raises.
    """
    if not url:
        return {}
    try:
        from urllib.parse import unquote, urlsplit
        parts = urlsplit(url)
        if not parts.scheme.startswith('mysql'):
            return {}
        return {
            'host': parts.hostname,
            'port': parts.port,
            'user': unquote(parts.username) if parts.username else None,
            'password': unquote(parts.password) if parts.password else None,
            'db': unquote(parts.path.lstrip('/')) or None,
        }
    except Exception:  # pragma: no cover - defensive, a bad URL must not break boot
        return {}


INSECURE_SECRET_KEYS = frozenset({
    '', 'your_secret_key_here', 'supersecretkey', 'secret', 'changeme',
    'change-me', 'dev', 'development', 'test', 'password',
})
_SANDBOX_SECRET_KEY = 'REDACTED'


def resolve_secret_key(env):
    """Return the Flask SECRET_KEY from ``env`` or refuse to boot (S-1.4).

    Outside SANDBOX=1 a missing or well-known placeholder key raises
    RuntimeError so a misconfigured deploy fails loudly instead of running with
    forgeable sessions.
    """
    key = (env.get('SECRET_KEY') or '').strip()
    sandbox = env.get('SANDBOX') == '1'
    if key and key.lower() not in INSECURE_SECRET_KEYS:
        if len(key) < 32:
            logging.warning("SECRET_KEY is shorter than 32 characters; generate a longer one "
                            "(python -c \"import secrets; print(secrets.token_hex(32))\").")
        return key
    if sandbox:
        return key or _SANDBOX_SECRET_KEY
    raise RuntimeError(
        "SECRET_KEY is not set (or is a known placeholder). Refusing to start: "
        "sessions could be forged. Set a long random SECRET_KEY in the environment "
        "(see docs/runbooks/SECRET_ROTATION.md), or SANDBOX=1 for local dev/tests."
    )


def create_app():
    app = Flask(__name__, template_folder='templates')
    # S-1.4: SECRET_KEY is mandatory. Sessions, the AI-key encryption key and the
    # SSO-secret encryption key all derive from it, so an unset or well-known
    # value would let anyone forge a session (role='admin'). Only SANDBOX=1
    # (tests/dev) may fall back to a throwaway value.
    app.secret_key = resolve_secret_key(os.environ)

    # S-5.5: no request body larger than this is ever read (the biggest legitimate
    # upload is a 25 MB voice clip). Oversize requests get a 413 before any handler runs.
    app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_CONTENT_LENGTH_MB', '26')) * 1024 * 1024

    @app.errorhandler(413)
    def _too_large(_err):
        from flask import jsonify as _jsonify, request
        msg = "Filen eller forespørgslen er for stor."
        if request.path.startswith(('/api', '/app1')) or request.is_json:
            return _jsonify({"success": False, "ok": False, "error": msg}), 413
        return msg, 413

    # Session cookie hardening. These are additive and don't invalidate existing
    # sessions. SESSION_COOKIE_SECURE must stay False under SANDBOX=1 so the test
    # harness (plain HTTP via Werkzeug test client + session_transaction) keeps
    # working; in real production (no SANDBOX) it becomes True so the session
    # cookie is only sent over HTTPS.
    app.config.update({
        'SESSION_COOKIE_HTTPONLY': True,
        'SESSION_COOKIE_SAMESITE': 'Lax',
        'SESSION_COOKIE_SECURE': (os.environ.get('SANDBOX') != '1'),
    })

    # Long-lived caching for worker-served static assets. Safe because every
    # asset URL is cache-busted by content hash (asset_version). See
    # docs/runbooks/STATIC_AND_PERF.md.
    try:
        app.config['SEND_FILE_MAX_AGE_DEFAULT'] = int(
            os.environ.get('STATIC_MAX_AGE_SECONDS', str(60 * 60 * 24 * 365))
        )
    except (TypeError, ValueError):
        app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 60 * 60 * 24 * 365

    # DB config comes from the environment. Precedence: explicit MYSQL_* env vars
    # > a DATABASE_URL (mysql://user:pass@host:port/db, which is what ServerHoster
    # injects for its linked managed MySQL). There is deliberately NO production
    # fallback any more: the old (PythonAnywhere-era) database is gone, so a missing
    # config must fail loudly (localhost/empty password) rather than silently
    # point at a dead host.
    db_url = _mysql_settings_from_database_url(os.environ.get('DATABASE_URL'))
    if not (os.environ.get('MYSQL_HOST') or db_url.get('host')):
        logging.warning(
            "No MYSQL_HOST or DATABASE_URL set; falling back to localhost. "
            "Set DATABASE_URL (or MYSQL_*) — see docs/runbooks/DEPLOY.md."
        )
    app.config.update({
        'MYSQL_HOST': os.environ.get('MYSQL_HOST') or db_url.get('host') or 'localhost',
        'MYSQL_USER': os.environ.get('MYSQL_USER') or db_url.get('user') or 'root',
        'MYSQL_PASSWORD': os.environ.get('MYSQL_PASSWORD') or db_url.get('password') or '',
        'MYSQL_DB': os.environ.get('MYSQL_DB') or db_url.get('db') or 'aileadz',
        'MYSQL_CURSORCLASS': 'DictCursor',
        # utf8mb4 so 4-byte chars (emoji etc.) don't 1366 on insert. flask_mysqldb
        # otherwise defaults the connection charset to 3-byte utf8.
        'MYSQL_CHARSET': 'utf8mb4',
    })
    if os.environ.get('MYSQL_PORT'):
        app.config['MYSQL_PORT'] = int(os.environ['MYSQL_PORT'])
    elif db_url.get('port'):
        app.config['MYSQL_PORT'] = int(db_url['port'])

    mysql = MySQL(app)
    app.mysql = mysql
    try:  # the AI analytics store (N-3.3) lives in MySQL; threads outside a request need the handle
        from app1 import memory_store as _ai_store
        _ai_store.bind_mysql(mysql)
    except Exception as e:
        logging.warning("AI store not bound: %s", e)

    # Request ids, structured logs, optional Sentry (N-8.2).
    try:
        from observability import register_observability
        register_observability(app)
    except Exception as e:
        logging.warning("Observability skipped: %s", e)

    # MAIL_* env -> app.config so Flask-Mail can actually send (N-0.2).
    try:
        from email_service import load_mail_config
        load_mail_config(app)
    except Exception as e:
        logging.warning("Mail config skipped: %s", e)

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(app1_bp, url_prefix='/app1')
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
    try:  # "Avanceret" link on Læringsanalyse (R-5); optional
        import enterprise_analytics
        enterprise_analytics.register_jinja(app)
    except Exception as e:
        logging.warning("Advanced analytics link skipped: %s", e)
    app.register_blueprint(api_enterprise_bp)
    app.register_blueprint(sso_bp)
    # Company settings hub + SSO email-domain discovery (N-4.6, N-7.1)
    from settings_hub import settings_hub_bp, sso_discovery_bp
    app.register_blueprint(settings_hub_bp)
    app.register_blueprint(sso_discovery_bp)
    app.register_blueprint(enterprise_settings_bp, url_prefix='/enterprise')
    app.register_blueprint(multitenant_reports_bp, url_prefix='/multitenant-reports')
    app.register_blueprint(futurematch_bp)

    from bulk_invite import bulk_invite_bp
    app.register_blueprint(bulk_invite_bp)

    # Dashboard-upgrade blueprints: new HR feature pages (Pillar B) sharing the
    # /hr prefix, and the global ⌘K search API. Guarded so a failure here can
    # never crash create_app().
    try:
        from hr_ext import hr_ext_bp
        app.register_blueprint(hr_ext_bp, url_prefix='/hr')
    except Exception as e:
        logging.warning("HR feature pages (hr_ext) skipped: %s", e)
    try:
        from search_api import search_api_bp
        app.register_blueprint(search_api_bp)
    except Exception as e:
        logging.warning("Global search API skipped: %s", e)

    # SCIM 2.0 provisioning/deprovisioning (enterprise SSO/HRIS). Guarded so a
    # failure here can never crash create_app().
    try:
        from scim_api import scim_bp
        app.register_blueprint(scim_bp)
    except Exception as e:
        logging.warning("SCIM provisioning integration skipped: %s", e)

    # GDPR data-subject toolkit (export + erasure). Guarded so a failure here
    # can never crash create_app(). Erase is platform-admin-only and gated by a
    # dry-run preview + typed-username confirmation inside the blueprint.
    try:
        from gdpr_routes import gdpr_bp
        app.register_blueprint(gdpr_bp)
    except Exception as e:
        logging.warning("GDPR data-subject toolkit skipped: %s", e)

    # Vendor self-service portal (/vendor). Isolated vendor sessions (never sets
    # session['user'], so a vendor can never reach the main app). Guarded so a
    # missing/broken vendor_portal or vendor_auth can never crash create_app().
    try:
        from vendor_portal import vendor_bp
        app.register_blueprint(vendor_bp)
    except Exception as e:
        logging.warning("Vendor portal integration skipped: %s", e)

    # Liveness/readiness probes (/healthz, /readyz)
    from health import health_bp
    app.register_blueprint(health_bp)

    try:
        from hr_course_assign import course_assign_bp
        app.register_blueprint(course_assign_bp)
    except Exception as e:
        logging.warning("HR course assign skipped: %s", e)

    # Team-order policy (N-5.2): save route + settings partial state.
    try:
        import team_order_policy
        app.register_blueprint(team_order_policy.team_policy_bp)
        team_order_policy.register_jinja(app)
    except Exception as e:
        logging.warning("Team order policy skipped: %s", e)

    # Admin product browser + search-index controls (N-3.1).
    try:
        from catalog_admin_routes import catalog_admin_bp
        app.register_blueprint(catalog_admin_bp)
    except Exception as e:
        logging.warning("Catalog admin routes skipped: %s", e)

    # AI-usage credit screens (N-6.4).
    try:
        from credit_routes import credit_bp
        app.register_blueprint(credit_bp)
    except Exception as e:
        logging.warning("Credit routes skipped: %s", e)

    # Defensive HTTP response headers (nosniff, frame options, report-only CSP).
    # Guarded so a failure here can never crash create_app().
    try:
        from security_headers import register_security_headers
        register_security_headers(app)
    except Exception as e:
        logging.warning("Security headers integration skipped: %s", e)

    # Gzip dynamic responses (HTML/JSON/…). The reverse proxy does not
    # gzip worker-proxied dynamic responses; this covers those.
    # Guarded so a failure here can never crash create_app().
    try:
        from response_compression import register_response_compression
        register_response_compression(app)
    except Exception as e:
        logging.warning("Response compression integration skipped: %s", e)

    # Initialize white-label context processor
    try:
        from white_label_global_integration import register_white_label_context_processor
        register_white_label_context_processor(app)
    except Exception as e:
        logging.warning("White-label integration skipped: %s", e)

    # Content-hash cache-busting: ?v={{ asset_version('path/under/static') }}.
    # Assets are cached for a year, so a forgotten hand-bumped ?v=N ships the
    # old file to every returning visitor.
    try:
        from asset_version import register_asset_version
        register_asset_version(app)
    except Exception as e:
        logging.warning("Asset versioning skipped: %s", e)

    # S-1.10: deactivated / SCIM-removed users lose access on the next request
    # (membership status re-checked, cached ~60 s), on every route.
    from auth_decorators import register_session_liveness, register_capability_context
    register_session_liveness(app)
    register_capability_context(app)

    # Render-time sanitising filters (safe_html / safe_css) replace bare |safe (S-1.9).
    from html_sanitize import register_html_filters
    register_html_filters(app)

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
            if os.environ.get("ENTERPRISE_TABLE_SYNC_SKIP") == "1":
                return  # test suites build the schema explicitly (tests/test_schema_baseline.py)
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

    # Part A (security/privacy) schema: goal-sharing columns, DSR tickets, 2FA,
    # reset tokens. Runs once per worker, fully guarded, never blocks a request.
    @app.before_request
    def _ensure_security_schema_once():
        if getattr(app, '_security_schema_ensured', False) or app.config.get('TESTING'):
            return
        app._security_schema_ensured = True
        try:
            conn = app.mysql.connection
            import goal_sharing, dsr_service, two_factor, password_tokens
            goal_sharing.ensure_schema(conn)
            dsr_service.ensure_table(conn)
            two_factor.ensure_table(conn)
            password_tokens.ensure_table(conn)
        except Exception as e:
            logging.warning("Security schema init: %s", e)

    # Hot-path performance indexes. Runs once per worker process (its own flag,
    # deliberately NOT gated by the enterprise-sync TTL stamp) so a `git pull` +
    # web-app reload applies the indexes on the next request without a manual
    # MySQL console step. ensure_performance_indexes is idempotent + never raises.
    @app.before_request
    def _ensure_performance_indexes_once():
        if getattr(app, '_perf_indexes_ensured', False):
            return
        app._perf_indexes_ensured = True
        try:
            from performance_indexes import ensure_performance_indexes
            ensure_performance_indexes(app)
        except Exception as e:
            logging.warning("Performance index init: %s", e)

    # Opportunistic scheduler driver (no cron/Celery on the host). Mirrors
    # event_bus.opportunistic_drain: runs at most once per ~60s PER WORKER from a
    # request hook so DUE scheduled jobs (outbox drain, daily insights, agreement
    # alerts, compliance recheck) still fire even with no scheduled task. Attached
    # as after_request and fully guarded so it can NEVER affect the response or
    # raise. Toggle off with SCHEDULER_OPPORTUNISTIC=0 (e.g. under tests). The
    # reliable driver is still drain_worker.py as a Scheduled/Always-on Task.
    _SCHED_OPPORTUNISTIC_MIN_INTERVAL = 60  # seconds between passes per worker

    @app.after_request
    def _opportunistic_scheduler(response):
        try:
            if os.getenv("SCHEDULER_OPPORTUNISTIC", "1").lower() in {"0", "false", "no", "off"}:
                return response
            now = time.time()
            last = getattr(app, '_last_scheduler_pass', 0)
            if (now - last) < _SCHED_OPPORTUNISTIC_MIN_INTERVAL:
                return response
            # Stamp BEFORE running so concurrent requests on this worker don't pile up.
            app._last_scheduler_pass = now
            import scheduler
            scheduler.run_due_jobs_safe(app)
        except Exception as e:
            # Opportunistic only — must never affect the response.
            logging.warning("Opportunistic scheduler skipped: %s", e)
        return response

    @app.route('/')
    def home():
        return redirect(url_for('dashboard.dashboard'))

    # One status vocabulary for every template (N-1.1).
    import order_lifecycle
    order_lifecycle.register_jinja(app)
    # Capability-aware navigation helpers (can(), has_endpoint()).
    import capabilities
    capabilities.register_jinja(app)
    import credit_service
    credit_service.register_jinja(app)

    # Danish 404/500 pages, JSON for API callers (N-0.3).
    from error_pages import register_error_handlers
    register_error_handlers(app)

    # S-2.1: CSRF protection (token on every unsafe request; key-authenticated
    # API/SCIM and the anonymous widget are exempt). Registered last so its
    # HTML-injection after_request runs before response compression.
    from csrf_protect import init_csrf
    init_csrf(app)

    return app

def main():
    """Local dev entrypoint: reach the production MySQL over an SSH tunnel.

    Production now runs on ServerHoster (the VPS); the tunnel target and
    credentials come from env — SSH_HOST, SSH_USER, SSH_PASSWORD (or SSH_PKEY),
    REMOTE_MYSQL_HOST/REMOTE_MYSQL_PORT — nothing is hardcoded here any more.
    """
    if sshtunnel is None:
        logging.error("sshtunnel is not installed. It is required for local development.")
        return
    ssh_host = os.environ.get('SSH_HOST')
    ssh_user = os.environ.get('SSH_USER')
    if not ssh_host or not ssh_user or not (os.environ.get('SSH_PASSWORD') or os.environ.get('SSH_PKEY')):
        logging.error("Set SSH_HOST, SSH_USER and SSH_PASSWORD (or SSH_PKEY) to open the dev tunnel.")
        return

    tunnel = None
    try:
        tunnel = sshtunnel.SSHTunnelForwarder(
            (ssh_host, int(os.environ.get('SSH_PORT', '22'))),
            ssh_username=ssh_user,
            ssh_password=os.environ.get('SSH_PASSWORD'),
            ssh_pkey=os.environ.get('SSH_PKEY'),
            remote_bind_address=(
                os.environ.get('REMOTE_MYSQL_HOST', '127.0.0.1'),
                int(os.environ.get('REMOTE_MYSQL_PORT', '3306')),
            ),
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
