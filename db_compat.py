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
