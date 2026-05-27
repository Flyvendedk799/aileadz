"""Small compatibility shim for environments without mysqlclient.

The app normally imports ``MySQLdb`` in route/tool modules, while ``run.py``
installs PyMySQL as a drop-in replacement at application startup. Tests and
direct module imports do not always pass through ``run.py``, so import this
module before importing ``MySQLdb`` in standalone-friendly code paths.
"""


def install_mysql_compat():
    try:
        import MySQLdb  # noqa: F401
        return
    except ImportError:
        import pymysql

        pymysql.install_as_MySQLdb()


install_mysql_compat()


def close_flask_mysql_connection():
    """Close and detach Flask-MySQLdb/PyMySQL request connections if present."""
    try:
        from flask import g, has_app_context
    except Exception:
        return
    if not has_app_context():
        return

    for attr in ("mysql_db", "_futurematch_mysql_connection"):
        conn = getattr(g, attr, None)
        if conn is None:
            continue
        try:
            conn.close()
        except Exception:
            pass
        try:
            delattr(g, attr)
        except Exception:
            pass


def refresh_flask_mysql_connection(mysql):
    """Ping the current request connection or reopen it after stale-connection errors."""
    if not mysql:
        return
    try:
        mysql.connection.ping(True)
        return
    except TypeError:
        try:
            mysql.connection.ping(reconnect=True)
            return
        except Exception:
            pass
    except Exception:
        pass

    close_flask_mysql_connection()
    try:
        mysql.connection.ping(True)
    except TypeError:
        mysql.connection.ping(reconnect=True)
